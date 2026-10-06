"""Provider adapters and schema-validated proposal generation for grill-v1.

Supports Codex and Antigravity (AGY) model providers, schema-validates all proposals
against PROPOSAL_SCHEMA, and handles malformed output, provider outages, and budget
exhaustion with deterministic fallbacks or session pausing without state corruption.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from galaxy_core.discovery.grill_dag import GrillDAG
from galaxy_core.discovery.grill_models import (
    GrillConflict,
    GrillFact,
    GrillNode,
    GrillQuestion,
    GrillSessionMeta,
    InvalidProposalError,
    PauseReason,
    validate_proposal,
)
from galaxy_core.discovery.grill_privacy import minimize_provider_context, redact_secrets
from galaxy_core.discovery.grill_profiles import get_profile


PROPOSAL_SYSTEM_PROMPT = """You are Sun, the Discovery Interrogator for Galaxy 3.1.
Your role is to propose clarifying questions, verified facts, and potential conflicts for an interview DAG.
You MUST output ONLY a valid JSON object conforming to this exact schema:

{
  "questions": [
    {
      "id": "Q_CATEGORY_01",
      "node_id": "node_category",
      "category": "category",
      "text": "Question text...",
      "why": "Explanation of why this question is crucial...",
      "alternatives": [
        {"id": "A", "text": "First distinct alternative", "description": "Short explanation"},
        {"id": "B", "text": "Second distinct alternative", "description": "Short explanation"}
      ],
      "recommendation": "A",
      "recommendation_reason": "Why alternative A is recommended...",
      "allow_custom": true,
      "dependencies": []
    }
  ],
  "facts": [],
  "conflicts": []
}

Rules:
1. Every question MUST have 2 to 4 distinct alternatives with unique IDs ('A', 'B', etc.).
2. The recommendation MUST match one of the alternative IDs.
3. Every question MUST explain 'why' and allow custom answers ('allow_custom': true).
4. Do NOT hallucinate answers on behalf of the user.
5. Return raw JSON without markdown explanations.
"""


class GrillProviderAdapter:
    """Safely interfaces with Codex, AGY, or deterministic fallback for question proposals."""

    def __init__(
        self,
        root: str | Path,
        provider: str = "codex",
        model: str | None = None,
        timeout_seconds: int = 30,
    ):
        self.root = Path(root)
        self.provider = (provider or "codex").lower()
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate_proposals(
        self,
        meta: GrillSessionMeta,
        dag: GrillDAG,
        frontier_nodes: list[GrillNode],
    ) -> tuple[list[GrillQuestion], list[GrillFact], list[GrillConflict], str | None]:
        """Generate schema-validated proposals for the current frontier.

        Returns (questions, facts, conflicts, pause_reason_or_none).
        On malformed JSON or provider failure, seamlessly falls back to deterministic questions.
        On budget exhaustion, returns ([], [], [], PauseReason.BUDGET_EXHAUSTED).
        """
        # 1. Check budget
        if meta.budget_used >= meta.budget_limit:
            return [], [], [], PauseReason.BUDGET_EXHAUSTED

        # If provider is explicitly deterministic or no frontier nodes, use deterministic branch
        if self.provider == "deterministic" or not frontier_nodes:
            fallback_q = self._deterministic_fallback(meta, dag, frontier_nodes)
            return fallback_q, [], [], None

        # 2. Prepare minimal, privacy-redacted prompt context
        context = minimize_provider_context(meta, dag, frontier_nodes)
        prompt = (
            f"{PROPOSAL_SYSTEM_PROMPT}\n\n"
            f"Current Interview Context:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
            f"Generate questions for these frontier categories: {context['frontier_categories']}."
        )
        prompt = redact_secrets(prompt)

        # 3. Call provider
        raw_output = None
        try:
            raw_output = self._call_provider(prompt)
        except Exception:
            # Provider outage / network / process failure: fall back deterministically
            fallback_q = self._deterministic_fallback(meta, dag, frontier_nodes)
            return fallback_q, [], [], None

        if not raw_output:
            fallback_q = self._deterministic_fallback(meta, dag, frontier_nodes)
            return fallback_q, [], [], None

        # 4. Parse and schema-validate proposal
        try:
            json_text = self._extract_json(raw_output)
            proposal_dict = json.loads(json_text)
            questions, facts, conflicts, proposed_deps = validate_proposal(
                proposal_dict, session_id=meta.session_id
            )

            # Apply proposed dependencies to DAG if valid (ignoring cycles safely)
            for parent_id, child_id in proposed_deps:
                if parent_id in dag.nodes and child_id in dag.nodes:
                    try:
                        dag.add_dependency(parent_id, child_id)
                    except Exception:
                        pass

            # Filter questions to only those matching current frontier or active categories
            valid_questions = []
            for q in questions:
                # Ensure node_id exists in DAG or create corresponding node
                if q.node_id in dag.nodes:
                    valid_questions.append(q)
                elif f"node_{q.category}" in dag.nodes:
                    q.node_id = f"node_{q.category}"
                    valid_questions.append(q)

            if valid_questions:
                return valid_questions, facts, conflicts, None

        except (json.JSONDecodeError, InvalidProposalError, Exception):
            # Malformed output: safe fallback to deterministic branch
            pass

        fallback_q = self._deterministic_fallback(meta, dag, frontier_nodes)
        return fallback_q, [], [], None

    def _call_provider(self, prompt: str) -> str:
        """Invoke model provider via ProviderRegistry, with fallback to CLI."""
        try:
            from galaxy_core.providers.registry import ProviderRegistry
            from galaxy_core.providers.base import GenerationRequest
            backend = ProviderRegistry.get(self.provider)
            req = GenerationRequest(
                prompt=prompt,
                model=self.model,
                timeout_seconds=float(self.timeout_seconds),
            )
            resp = backend.generate(req)
            if resp and resp.content:
                return resp.content
        except Exception:
            pass

        cmd: list[str] = []
        if self.provider == "codex":
            codex_bin = shutil.which("codex")
            if not codex_bin:
                raise RuntimeError("codex binary not found on PATH")
            cmd = [codex_bin, "exec", "--json"]
            if self.model:
                cmd.extend(["--model", self.model])
        elif self.provider in {"antigravity", "agy"}:
            agy_bin = shutil.which("agy") or shutil.which("antigravity")
            if not agy_bin:
                raise RuntimeError("antigravity/agy binary not found on PATH")
            cmd = [agy_bin, "prompt"]
            if self.model:
                cmd.extend(["--model", self.model])
        else:
            raise ValueError(f"Unknown provider: {self.provider}")

        proc = subprocess.run(
            cmd,
            input=prompt,
            text=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            check=True,
        )
        return proc.stdout

    @staticmethod
    def _extract_json(text: str) -> str:
        """Extract JSON block from text, supporting markdown code fences."""
        match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
        if match:
            return match.group(1).strip()
        match_brace = re.search(r"(\{[\s\S]*\})", text)
        if match_brace:
            return match_brace.group(1).strip()
        return text.strip()

    @staticmethod
    def _deterministic_fallback(
        meta: GrillSessionMeta,
        dag: GrillDAG,
        frontier_nodes: list[GrillNode],
    ) -> list[GrillQuestion]:
        """Generate deterministic, compliant questions from profile for frontier nodes."""
        profile = get_profile(meta.profile)
        questions: list[GrillQuestion] = []

        for node in frontier_nodes:
            if node.question:
                questions.append(node.question)
            else:
                q = profile.questions.get(node.category)
                if q:
                    questions.append(q)
        return questions
