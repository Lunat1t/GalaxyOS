"""Deterministic decision-DAG state machine for Galaxy 3.1 grill-v1.

Maintains nodes, dependencies, frontier calculation, cycle detection,
transitive reopening on revision, conflict tracking, and completion gates.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from galaxy_core.discovery.grill_models import (
    ConflictStatus,
    CycleDetectedError,
    GrillAlternative,
    GrillAnswerHistory,
    GrillConflict,
    GrillDependency,
    GrillFact,
    GrillNode,
    GrillQuestion,
    InvalidProposalError,
    NodeStatus,
    utcnow,
)


class GrillDAG:
    """Authoritative decision DAG state machine."""

    def __init__(self) -> None:
        self.nodes: dict[str, GrillNode] = {}
        self.dependencies: list[GrillDependency] = []
        self.conflicts: dict[str, GrillConflict] = {}
        self.facts: dict[str, GrillFact] = {}

    def add_node(self, node: GrillNode) -> None:
        """Add or update a node in the DAG."""
        node.validate()
        self.nodes[node.id] = node

    def get_node(self, node_id: str) -> GrillNode | None:
        return self.nodes.get(node_id)

    def add_dependency(self, parent_id: str, child_id: str) -> GrillDependency:
        """Add a directed dependency: parent_id must be resolved before child_id."""
        if parent_id not in self.nodes:
            raise InvalidProposalError(f"Parent node '{parent_id}' does not exist in DAG")
        if child_id not in self.nodes:
            raise InvalidProposalError(f"Child node '{child_id}' does not exist in DAG")
        if parent_id == child_id:
            raise CycleDetectedError(f"Self-dependency detected for node '{parent_id}'")

        # Check if adding parent_id -> child_id would introduce a cycle
        # A cycle would occur if parent_id is already a descendant of child_id
        if parent_id in self.descendants(child_id):
            raise CycleDetectedError(
                f"Adding dependency {parent_id} -> {child_id} creates a cycle in the decision DAG"
            )

        # Idempotently avoid duplicate dependencies
        for dep in self.dependencies:
            if dep.parent_id == parent_id and dep.child_id == child_id:
                return dep

        dep = GrillDependency(parent_id=parent_id, child_id=child_id)
        dep.validate()
        self.dependencies.append(dep)
        child = self.nodes[child_id]
        if parent_id not in child.dependencies:
            child.dependencies.append(parent_id)
        child.updated_at = utcnow()

        # Recalculate frontier to reflect updated dependencies
        self.recalculate_frontier()
        return dep

    def has_dependency(self, parent_id: str, child_id: str) -> bool:
        """Return True if direct dependency parent_id -> child_id exists."""
        return any(d.parent_id == parent_id and d.child_id == child_id for d in self.dependencies)

    def add_fact(self, fact: GrillFact) -> None:
        """Register a verified fact in the DAG."""
        fact.validate()
        self.facts[fact.id] = fact

    def parents(self, node_id: str) -> list[str]:
        """Return direct prerequisite parent IDs for node_id."""
        return [dep.parent_id for dep in self.dependencies if dep.child_id == node_id]

    def children(self, node_id: str) -> list[str]:
        """Return direct dependent child IDs for node_id."""
        return [dep.child_id for dep in self.dependencies if dep.parent_id == node_id]

    def descendants(self, node_id: str) -> set[str]:
        """Return all transitive descendants reachable from node_id."""
        visited: set[str] = set()
        queue = deque(self.children(node_id))
        while queue:
            curr = queue.popleft()
            if curr not in visited:
                visited.add(curr)
                queue.extend(self.children(curr))
        return visited

    def ancestors(self, node_id: str) -> set[str]:
        """Return all transitive ancestors of node_id."""
        visited: set[str] = set()
        queue = deque(self.parents(node_id))
        while queue:
            curr = queue.popleft()
            if curr not in visited:
                visited.add(curr)
                queue.extend(self.parents(curr))
        return visited

    def check_cycles(self) -> None:
        """Validate that the DAG contains no cycles using Kahn's algorithm."""
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for dep in self.dependencies:
            if dep.child_id in in_degree:
                in_degree[dep.child_id] += 1

        queue = deque([nid for nid, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for child in self.children(curr):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)

        if visited_count != len(self.nodes):
            raise CycleDetectedError("Cycle detected in decision DAG")

    def topological_sort(self) -> list[str]:
        """Return node IDs in topological order."""
        self.check_cycles()
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for dep in self.dependencies:
            if dep.child_id in in_degree:
                in_degree[dep.child_id] += 1

        queue = deque(sorted([nid for nid, deg in in_degree.items() if deg == 0]))
        result: list[str] = []

        while queue:
            curr = queue.popleft()
            result.append(curr)
            for child in sorted(self.children(curr)):
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    queue.append(child)
        return result

    def has_unresolved_conflict(self, node_id: str) -> bool:
        """Return True if node_id is involved in an active, unresolved conflict."""
        for conflict in self.conflicts.values():
            if conflict.status == ConflictStatus.ACTIVE and node_id in conflict.node_ids:
                return True
        return False

    def add_conflict(self, conflict: GrillConflict) -> None:
        """Register a contradiction/conflict between nodes."""
        conflict.validate()
        self.conflicts[conflict.id] = conflict
        for nid in conflict.node_ids:
            if nid in self.nodes:
                node = self.nodes[nid]
                if node.status in {NodeStatus.BLOCKED, NodeStatus.FRONTIER}:
                    node.status = NodeStatus.CONFLICT
                    node.updated_at = utcnow()
        self.recalculate_frontier()

    def resolve_conflict(self, conflict_id: str, resolution: str) -> None:
        """Mark a conflict as resolved with an explanation."""
        if conflict_id not in self.conflicts:
            raise InvalidProposalError(f"Conflict '{conflict_id}' not found")
        if not resolution or not resolution.strip():
            raise InvalidProposalError("Resolution explanation cannot be empty")
        conflict = self.conflicts[conflict_id]
        conflict.status = ConflictStatus.RESOLVED
        conflict.resolution = resolution.strip()
        conflict.resolved_at = utcnow()
        for nid in conflict.node_ids:
            if nid in self.nodes:
                node = self.nodes[nid]
                if node.status == NodeStatus.CONFLICT and not self.has_unresolved_conflict(nid):
                    node.status = NodeStatus.BLOCKED
                    node.updated_at = utcnow()
        self.recalculate_frontier()

    def accept_conflict(self, conflict_id: str, rationale: str) -> None:
        """Explicitly accept a conflict with documented user rationale."""
        if conflict_id not in self.conflicts:
            raise InvalidProposalError(f"Conflict '{conflict_id}' not found")
        if not rationale or not rationale.strip():
            raise InvalidProposalError("Accepted conflict rationale cannot be empty")
        conflict = self.conflicts[conflict_id]
        conflict.status = ConflictStatus.ACCEPTED
        conflict.accepted_rationale = rationale.strip()
        conflict.resolved_at = utcnow()
        for nid in conflict.node_ids:
            if nid in self.nodes:
                node = self.nodes[nid]
                if node.status == NodeStatus.CONFLICT and not self.has_unresolved_conflict(nid):
                    node.status = NodeStatus.BLOCKED
                    node.updated_at = utcnow()
        self.recalculate_frontier()

    def recalculate_frontier(self) -> list[str]:
        """Recalculate frontier according to the exact invariant:

        Frontier(G) = {v in V | status(v) in {blocked, frontier}
                               and for all p in parents(v): status(p) in {answered, assumption, research}
                               and not has_unresolved_conflict(p)
                               and not has_unresolved_conflict(v)}
        """
        frontier_ids: list[str] = []
        for nid, node in self.nodes.items():
            if self.has_unresolved_conflict(nid):
                if node.status in {NodeStatus.BLOCKED, NodeStatus.FRONTIER}:
                    node.status = NodeStatus.CONFLICT
                    node.updated_at = utcnow()
                continue
            elif node.status == NodeStatus.CONFLICT:
                node.status = NodeStatus.BLOCKED
                node.updated_at = utcnow()

            if node.status in {NodeStatus.BLOCKED, NodeStatus.FRONTIER}:
                parents = self.parents(nid)
                all_parents_satisfied = True
                for pid in parents:
                    p = self.nodes.get(pid)
                    if not p:
                        all_parents_satisfied = False
                        break
                    if p.status not in NodeStatus.RESOLVED or self.has_unresolved_conflict(pid):
                        all_parents_satisfied = False
                        break

                if all_parents_satisfied:
                    if node.status != NodeStatus.FRONTIER:
                        node.status = NodeStatus.FRONTIER
                        node.updated_at = utcnow()
                    frontier_ids.append(nid)
                else:
                    if node.status != NodeStatus.BLOCKED:
                        node.status = NodeStatus.BLOCKED
                        node.updated_at = utcnow()

        return sorted(frontier_ids)

    def get_independent_frontier_batch(self, max_batch: int = 5) -> list[GrillNode]:
        """Return at most 5 independent frontier questions, or all when < 3 exist.

        Nodes on the frontier have all parent prerequisites satisfied and have
        no dependencies among each other, ensuring mutual independence.
        """
        frontier_ids = self.recalculate_frontier()
        candidates = [self.nodes[nid] for nid in frontier_ids if self.nodes[nid].question is not None]
        if len(candidates) < 3:
            return candidates
        return candidates[:max_batch]

    def reopen_node(
        self,
        node_id: str,
        session_id: str = "",
        actor: str = "user",
        rationale: str | None = None,
    ) -> list[GrillAnswerHistory]:
        """Atomically reopen node and all transitive descendants upon answer revision.

        Previous answers are preserved in GrillAnswerHistory.
        """
        if node_id not in self.nodes:
            raise InvalidProposalError(f"Node '{node_id}' does not exist in DAG")

        history_records: list[GrillAnswerHistory] = []
        target = self.nodes[node_id]

        # Record history for target node if it had an answer or was resolved
        if target.answer or target.status in NodeStatus.RESOLVED:
            answer_val = target.answer or target.assumption_text or target.research_task or ""
            history_records.append(
                GrillAnswerHistory(
                    session_id=session_id,
                    node_id=target.id,
                    answer=answer_val,
                    quality=target.answer_quality or "",
                    actor=actor,
                    rationale=rationale or "User initiated revision",
                )
            )
        target.answer = None
        target.answer_option_id = None
        target.answer_quality = None
        target.is_custom_answer = False
        target.assumption_text = None
        target.assumption_verification = None
        target.research_task = None
        target.research_result = None
        target.answered_at = None
        target.status = NodeStatus.FRONTIER
        target.updated_at = utcnow()

        # Reopen all transitive descendants
        desc_ids = self.descendants(node_id)
        for did in desc_ids:
            desc = self.nodes[did]
            if desc.answer or desc.status in NodeStatus.RESOLVED:
                history_records.append(
                    GrillAnswerHistory(
                        session_id=session_id,
                        node_id=desc.id,
                        answer=desc.answer or desc.assumption_text or desc.research_task or "",
                        quality=desc.answer_quality or "",
                        actor=actor,
                        rationale=f"Reopened due to revision of upstream prerequisite {node_id}",
                    )
                )
                desc.answer = None
                desc.answer_option_id = None
                desc.answer_quality = None
                desc.is_custom_answer = False
                desc.assumption_text = None
                desc.assumption_verification = None
                desc.research_task = None
                desc.research_result = None
                desc.answered_at = None

            desc.status = NodeStatus.BLOCKED
            desc.updated_at = utcnow()

        # Recalculate frontier across the DAG
        self.recalculate_frontier()
        return history_records

    def is_draft_ready(self, mode: str = "relentless") -> tuple[bool, list[str]]:
        """Validate the 5 completion invariants:

        1. Frontier is empty.
        2. All detected conflicts are resolved or explicitly accepted.
        3. All assumptions have an explicit, testable verification method.
        4. Outcome criteria are concretely testable.
        5. No nodes remain in blocked or conflict states.
        """
        frontier = self.recalculate_frontier()
        reasons: list[str] = []

        if frontier:
            reasons.append(f"Frontier is not empty: {len(frontier)} question(s) remaining ({', '.join(frontier)})")

        unresolved_conflicts = [
            c.id for c in self.conflicts.values() if c.status == ConflictStatus.ACTIVE
        ]
        if unresolved_conflicts:
            reasons.append(f"Unresolved active conflicts exist: {', '.join(unresolved_conflicts)}")

        # Check assumptions for explicit verification methods
        unverified_assumptions: list[str] = []
        for node in self.nodes.values():
            if node.status == NodeStatus.ASSUMPTION:
                if not node.assumption_verification or not node.assumption_verification.strip():
                    unverified_assumptions.append(node.id)
        if unverified_assumptions:
            reasons.append(
                f"Assumptions lack explicit verification methods: {', '.join(unverified_assumptions)}"
            )

        # Check outcome/success testability
        outcome_nodes = [
            n for n in self.nodes.values()
            if n.category in {"outcome", "success", "must_have"} and n.status in NodeStatus.RESOLVED
        ]
        if not outcome_nodes:
            reasons.append("Outcome criteria are not defined or testable")

        # Check for any remaining blocked or conflict nodes
        blocked_nodes = [
            n.id for n in self.nodes.values()
            if n.status in {NodeStatus.BLOCKED, NodeStatus.CONFLICT}
        ]
        if blocked_nodes:
            reasons.append(f"Incomplete nodes remaining: {', '.join(blocked_nodes)}")

        return (len(reasons) == 0, reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": {nid: node.to_dict() for nid, node in self.nodes.items()},
            "dependencies": [dep.to_dict() for dep in self.dependencies],
            "conflicts": {cid: conflict.to_dict() for cid, conflict in self.conflicts.items()},
            "facts": {fid: fact.to_dict() for fid, fact in self.facts.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GrillDAG:
        dag = cls()
        for raw_node in (data.get("nodes") or {}).values():
            dag.add_node(GrillNode.from_dict(raw_node))
        for raw_dep in data.get("dependencies") or []:
            dep = GrillDependency.from_dict(raw_dep)
            dag.dependencies.append(dep)
            if dep.child_id in dag.nodes:
                child = dag.nodes[dep.child_id]
                if dep.parent_id not in child.dependencies:
                    child.dependencies.append(dep.parent_id)
        for raw_conflict in (data.get("conflicts") or {}).values():
            conflict = GrillConflict.from_dict(raw_conflict)
            dag.conflicts[conflict.id] = conflict
        for raw_fact in (data.get("facts") or {}).values():
            fact = GrillFact.from_dict(raw_fact)
            dag.facts[fact.id] = fact
        dag.recalculate_frontier()
        return dag
