#!/usr/bin/env python3
"""Galaxy 3.2.1 headless Context OS command line."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
from typing import Any

from galaxy_core.engine.autonomy import (
    AutonomousEngine, AutonomyStore, BudgetLimits, ExecutionPlan, ModelProfile,
)
from galaxy_core.brain.store import BrainStore
from galaxy_core.brain.reconcile import MemoryReconciler
from galaxy_core.world import ProjectWorldModel
from galaxy_core.context import ContextCompiler
from galaxy_core.attention import AttentionEngine
from galaxy_core.benchmark import export_predictions, evaluate_predictions
from galaxy_core.discovery.interview import (
    QUESTION_BANK,
    GrillInterview,
    SunInterview,
    classify_request,
)
from galaxy_core.discovery.grill_models import GrillRound
from galaxy_core.discovery.grill_privacy import redact_secrets
from galaxy_core.discovery.grill_profiles import detect_profile, list_profiles
from galaxy_core.discovery.i18n import (
    format_brief_markdown,
    format_question,
    format_round,
    format_tree,
    t,
)
from galaxy_core.engine.planning import (
    CLIMoonExecutor, CLIModelExecutor, CLIPlanner, CLITeamPlanner, TeamAwareExecutor,
)
from galaxy_core.engine.storage import VERSION, atomic_json
from galaxy_core.migration import migrate_user_data
from galaxy_core.agents import (
    AgentRegistry, AutomaticTeamBuilder, MoonBudget, MoonResult,
    SubAgentManager, TeamPolicy,
)
from galaxy_core.engine.decisions import (
    Choice,
    DAGDecisionRouter,
    DecisionEngine,
    DecisionRequest,
    DecisionFabric,
    GuardrailVerdict,
    MarsDecisionGuardrail,
    Noul,
    Score,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_MODELS = ROOT / "config" / "model-profiles.json"


def load_models(path: str | Path = DEFAULT_MODELS) -> list[ModelProfile]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    profiles = [ModelProfile.from_dict(item) for item in data.get("profiles", [])]
    if not profiles:
        raise ValueError("model configuration contains no profiles")
    return profiles


def learn_from_run(run_id: str, plan: ExecutionPlan, run: dict, brain: BrainStore | None = None,
                   agents: AgentRegistry | None = None) -> None:
    brain = brain or BrainStore(ROOT)
    agents = agents or AgentRegistry(ROOT)
    for row in run["nodes"]:
        result = row.get("result")
        if not result:
            continue
        brain.remember(
            "verification" if next(x for x in plan.nodes if x.id == row["node_id"]).capability in {"qa", "verification"} else "outcome",
            f"{plan.goal} — {row['node_id']}", result["summary"],
            agent=next(x.role for x in plan.nodes if x.id == row["node_id"]),
            project=plan.project, tags=["galaxy-v2", "autonomous-dag"],
            success=True, qa_pass=True, run_id=run_id, source_type="autonomy_run",
            source_ref=f"data/runs/{run_id}/state.json", confidence=.92,
            importance=.75, confirmed=True, confirmed_by="dag_verification",
            dedupe_key=f"v2:{run_id}:{row['node_id']}",
        )
        agent_id = next(x.role.lower() for x in plan.nodes if x.id == row["node_id"])
        candidates = result.get("memory_candidates") or []
        lesson = candidates[0] if candidates else result["summary"]
        agents.record_experience(agent_id, "success", lesson, project=plan.project,
                                 run_id=run_id, confidence=float(result.get("confidence", .7)))


def engine(models: str | Path = DEFAULT_MODELS, concurrency: int = 4,
           team_builder: bool = True) -> AutonomousEngine:
    brain = BrainStore(ROOT)
    agents = AgentRegistry(ROOT)
    agents.bootstrap_defaults()
    profiles = load_models(models)
    executor = (TeamAwareExecutor(
        ROOT, agents, CLIModelExecutor(), TeamPolicy(max_parallel=min(4, max(1, concurrency))),
        profile_limits={profile.name: profile.max_parallel for profile in profiles})
        if team_builder else CLIModelExecutor())
    compilers: dict[str, ContextCompiler] = {}
    def project_context(node, plan):
        compiler = compilers.setdefault(plan.project, ContextCompiler(ROOT, ROOT, plan.project))
        packet = compiler.compile(node.objective, budget_tokens=0, max_files=16)
        return {"packet": packet.to_dict(), "agent": agents.context(node.role.lower(), project=plan.project)}
    return AutonomousEngine(
        ROOT, profiles, executor, max_parallel=concurrency,
        memory_context=lambda node, plan: brain.context(node.objective, agent=node.role, project=plan.project, limit=10),
        project_context=project_context,
        learning_hook=lambda run_id, plan, run: learn_from_run(run_id, plan, run, brain, agents),
    )


def print_run(run: dict) -> None:
    compact = {
        "run_id": run["id"], "status": run["status"], "reason": run["reason"],
        "goal": run["plan"]["goal"], "project": run["plan"]["project"],
        "usage": run["usage"],
        "nodes": [{
            "id": row["node_id"], "status": row["status"], "attempts": row["attempts"],
            "model": (row.get("route") or {}).get("profile"),
            "summary": (row.get("result") or {}).get("summary"), "error": row.get("error") or "",
        } for row in run["nodes"]],
        "awaiting_approval": [row["node_id"] for row in run["nodes"]
                              if row["status"] == "AWAITING_APPROVAL"],
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))


def interview_view(session: dict) -> dict:
    return {
        "interview_id": session["id"], "status": session["status"],
        "project": session["project"], "idea": session["initial_idea"],
        "depth": session["depth"], "questions_asked": session.get("questions_asked", 0),
        "max_questions": session.get("max_questions", 30), "pending_question": session.get("pending"),
        "answered_categories": [key for key, value in session.get("answers", {}).items()
                                if value.get("status") == "complete"],
        "assumptions": session.get("assumptions", []), "brief_ready": bool(session.get("brief")),
    }


def print_question(question: dict, current: int, maximum: int) -> None:
    print(f"\nSun — вопрос {current}/{maximum} [{question['category']}]")
    print(question["text"])
    print(f"Почему спрашиваю: {question['why']}")
    if question.get("examples"):
        print("Для ориентира: " + "; ".join(question["examples"]))


def save_brief(sun: Any, session_id: str, destination: str | None) -> Path:
    session = sun.require(session_id)
    target = Path(destination) if destination else ROOT / "data" / "briefs" / f"{session_id.lower()}.md"
    if not target.is_absolute():
        target = ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        atomic_json(target, session["brief"])
    else:
        target.write_text(sun.markdown(session_id), encoding="utf-8")
    return target


def create_plan_from_interview(sun: Any, session_id: str, provider: str,
                               model: str | None, out: str | None,
                               extra_constraints: list[str] | None = None) -> tuple[ExecutionPlan, Path]:
    session = sun.require(session_id)
    goal = sun.planning_prompt(session_id, confirmed_only=True)
    memories = BrainStore(ROOT).context(goal, agent="Sun", project=session["project"], limit=12)
    trace = ROOT / "data" / "planning" / session_id.lower()
    constraints: list[str] = []
    if session.get("brief") and session["brief"].get("assumptions"):
        constraints.extend(session["brief"]["assumptions"])
    if extra_constraints:
        constraints.extend(extra_constraints)
    generated = asyncio.run(CLIPlanner(ROOT, provider, model).plan(
        goal, session["project"], constraints=constraints,
        memories=memories, evidence=trace))
    destination = Path(out) if out else ROOT / "data" / "plans" / f"{generated.plan_id.lower()}.json"
    if not destination.is_absolute():
        destination = ROOT / destination
    atomic_json(destination, generated.to_dict())
    return generated, destination


def interactive_legacy_interview(args) -> int:
    sun = SunInterview(ROOT, engine_type="legacy")
    if args.resume:
        session = sun.require(args.resume)
    else:
        if not args.idea:
            raise ValueError("idea is required unless --resume is used")
        session = sun.start(args.idea, args.project, args.depth, args.max_questions)
        print(f"Sun начал интервью (legacy): {session['id']}")
        print("Можно отвечать «не знаю». Команда /pause сохраняет сессию.")
    while True:
        session = sun.require(session["id"])
        if session["status"] in {"DISCOVERY", "PAUSED"}:
            question = sun.next_question(session["id"])
            session = sun.require(session["id"])
            if not question:
                continue
            print_question(question, session["questions_asked"], session["max_questions"])
            try:
                answer = input("Ваш ответ: ").strip()
            except (EOFError, KeyboardInterrupt):
                sun.pause(session["id"])
                print(f"\nИнтервью сохранено. Продолжить: python galaxy.py interview --resume {session['id']}")
                return 0
            if answer.lower() == "/pause":
                sun.pause(session["id"])
                print(f"Интервью сохранено. Продолжить: python galaxy.py interview --resume {session['id']}")
                return 0
            sun.answer(session["id"], answer)
            continue
        if session["status"] == "DRAFT_READY":
            print("\n" + sun.markdown(session["id"]))
            try:
                decision = input("Sun правильно понял задачу? [да/изменить/пауза]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                sun.pause(session["id"]); return 0
            if decision in {"да", "yes", "y"}:
                session = sun.confirm(session["id"])
                path = save_brief(sun, session["id"], args.brief_out)
                print(f"Goal Brief подтверждён и сохранён: {path.relative_to(ROOT)}")
                if args.create_plan:
                    plan, plan_path = create_plan_from_interview(
                        sun, session["id"], args.provider, args.model, args.plan_out,
                        getattr(args, "constraint", []))
                    print(json.dumps({"plan_id": plan.plan_id, "path": str(plan_path.relative_to(ROOT)),
                                      "layers": plan.layers()}, ensure_ascii=False, indent=2))
                return 0
            if decision in {"изменить", "change", "edit"}:
                categories = [q.category for q in QUESTION_BANK
                              if q.category in session["brief"]["requirements"]]
                print("Категории: " + ", ".join(categories))
                category = input("Какой раздел исправить: ").strip()
                session = sun.revise(session["id"], category)
                continue
            sun.pause(session["id"])
            print(f"Черновик сохранён. Продолжить: python galaxy.py interview --resume {session['id']}")
            return 0
        if session["status"] == "CONFIRMED":
            print(sun.markdown(session["id"]))
            return 0


def interactive_interview(args) -> int:
    engine_choice = getattr(args, "engine", "grill-v1")
    if engine_choice == "legacy":
        return interactive_legacy_interview(args)

    grill = GrillInterview(ROOT)
    lang = getattr(args, "lang", "ru")

    if args.resume:
        session = grill.require(args.resume)
        if getattr(args, "tree", False):
            print(format_tree(grill.tree(session["id"]), lang=lang))
            return 0
        print(f"Возобновление интервью Sun (grill-v1): {session['id']} [профиль: {session['profile']}]")
    else:
        if not args.idea:
            raise ValueError("idea is required unless --resume is used")

        profile_choice = getattr(args, "profile", "auto")
        if profile_choice == "auto":
            det_prof, det_conf, det_reason = detect_profile(args.idea)
            print(f"\nSun — Автоматически выбран профиль: '{det_prof}' ({det_reason})")
            print(f"Для ручного выбора используйте: --profile <general|product|engineering|bug|research>\n")
            profile_choice = det_prof

        budget = getattr(args, "budget", None) or getattr(args, "max_questions", None) or 30
        session = grill.start(
            idea=args.idea,
            project=args.project,
            profile=profile_choice,
            mode=getattr(args, "mode", "relentless"),
            depth=args.depth,
            budget_limit=budget,
        )
        print(f"Sun начал интервью (grill-v1): {session['id']} [профиль: {session['profile']}, режим: {session['mode']}]")
        print("Отвечайте вариантами (A, B...), текстом или 'rec' для принятия рекомендаций.")
        print("Команды: /pause (пауза), /tree (дерево решений), /revise <раздел> (исправить).")

    while True:
        session = grill.require(session["id"])

        if getattr(args, "tree", False):
            print(format_tree(grill.tree(session["id"]), lang=lang))
            return 0

        if session["status"] in {"DISCOVERY", "PAUSED"}:
            if session["status"] == "PAUSED":
                if session.get("pause_reason") == "BUDGET_EXHAUSTED":
                    print(t("budget_exhausted_msg", lang, limit=session["budget_limit"]))
                    try:
                        choice = input("Увеличить лимит вопросов на сколько? (число, например 10, или 'выход'): ").strip()
                    except (EOFError, KeyboardInterrupt):
                        return 0
                    if choice.isdigit() and int(choice) > 0:
                        session = grill.resume(session["id"], additional_budget=int(choice))
                    else:
                        return 0
                else:
                    print(t("paused_msg", lang, session_id=session["id"]))
                    try:
                        cmd = input("Возобновить интервью? [продолжить / выход]: ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        return 0
                    if cmd in {"продолжить", "continue", "resume", "да", "yes", "y", "/continue", "/resume"}:
                        session = grill.resume(session["id"])
                    else:
                        return 0

            # Get current active round
            current_rnd = session.get("current_round")
            if not current_rnd or not current_rnd.get("questions"):
                # Advance round
                rnd_obj = grill.engine.next_round(session["id"])
                session = grill.require(session["id"])
                if not rnd_obj:
                    if session["status"] == "DRAFT_READY":
                        continue
                    print("Нет доступных вопросов на фронтире.")
                    return 0
                current_rnd = rnd_obj.to_dict()

            round_obj = GrillRound.from_dict(current_rnd)
            print(format_round(round_obj, lang=lang,
                               budget_used=session.get("budget_used", 0),
                               budget_limit=session.get("budget_limit", 30)))

            try:
                answer = input("\nВаш ответ: ").strip()
            except (EOFError, KeyboardInterrupt):
                grill.pause(session["id"])
                print(f"\n{t('paused_msg', lang, session_id=session['id'])}")
                return 0

            if not answer:
                continue

            ans_lower = answer.lower()
            if ans_lower in {"/pause", "pause"}:
                grill.pause(session["id"])
                print(f"{t('paused_msg', lang, session_id=session['id'])}")
                return 0

            if ans_lower in {"/tree", "tree"}:
                print(format_tree(grill.tree(session["id"]), lang=lang))
                continue

            if ans_lower in {"/finish", "finish", "/завершить", "завершить"}:
                session = grill.finish(session["id"])
                print("Интервью завершено по запросу пользователя. Подготовка черновика Goal Brief...")
                continue

            if ans_lower.startswith("/revise ") or ans_lower.startswith("revise "):
                parts = answer.split(maxsplit=1)
                if len(parts) > 1:
                    cat = parts[1].strip()
                    session = grill.revise(session["id"], cat)
                    print(f"Раздел '{cat}' и зависимые ветви открыты заново.")
                continue

            if ans_lower in {"/help", "help"}:
                print(t("controls_hint", lang=lang))
                continue

            # Submit answer (supports 'rec', 'не знаю', option IDs, or text)
            session = grill.answer(session["id"], answer)
            continue

        if session["status"] == "DRAFT_READY":
            print("\n" + grill.markdown(session["id"]))
            try:
                decision = input(t("confirm_prompt", lang)).strip().lower()
            except (EOFError, KeyboardInterrupt):
                grill.pause(session["id"])
                return 0

            if decision in {"да", "yes", "y"}:
                session = grill.confirm(session["id"])
                path = save_brief(grill, session["id"], args.brief_out)
                print(f"Goal Brief подтверждён и сохранён: {path.relative_to(ROOT)}")
                if args.create_plan:
                    plan, plan_path = create_plan_from_interview(
                        grill, session["id"], args.provider, args.model, args.plan_out,
                        getattr(args, "constraint", []))
                    print(json.dumps({"plan_id": plan.plan_id, "path": str(plan_path.relative_to(ROOT)),
                                      "layers": plan.layers()}, ensure_ascii=False, indent=2))
                return 0

            if decision in {"/tree", "tree", "дерево"}:
                print(format_tree(grill.tree(session["id"]), lang=lang))
                continue

            if (decision in {"изменить", "change", "edit"} or
                    decision.startswith("изменить ") or decision.startswith("edit ")):
                category = ""
                if " " in decision:
                    category = decision.split(maxsplit=1)[1].strip()
                else:
                    cats = list((session.get("brief") or {}).get("requirements", {}).keys())
                    print("Категории: " + ", ".join(cats))
                    category = input("Какой раздел исправить: ").strip()
                if category:
                    session = grill.revise(session["id"], category)
                continue

            grill.pause(session["id"])
            print(f"Черновик сохранён. Продолжить: python galaxy.py interview --resume {session['id']}")
            return 0

        if session["status"] == "CONFIRMED":
            print(grill.markdown(session["id"]))
            return 0


def cmd_logs(args) -> int:
    runs_dir = ROOT / "data" / "runs"
    vault_tasks_dir = ROOT / "data" / "vault" / "tasks"

    target = getattr(args, "target", "latest") or "latest"
    output_json = getattr(args, "json", False)
    lines_limit = getattr(args, "lines", 50)
    stream_type = "stderr" if getattr(args, "stderr", False) else "stdout"
    filter_node = getattr(args, "node", None)
    filter_agent = getattr(args, "agent", None)
    filter_attempt = getattr(args, "attempt", None)

    if target == "list":
        available_runs = []
        if runs_dir.is_dir():
            for r in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not r.is_dir():
                    continue
                state_file = r / "state.json"
                state_data = {}
                if state_file.is_file():
                    try:
                        state_data = json.loads(state_file.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                log_files = list(r.glob("**/*.log"))
                available_runs.append({
                    "id": r.name,
                    "type": "run",
                    "status": state_data.get("status", "UNKNOWN"),
                    "goal": state_data.get("goal", ""),
                    "log_count": len(log_files),
                    "mtime": r.stat().st_mtime,
                })

        available_tasks = []
        if vault_tasks_dir.is_dir():
            for t in sorted(vault_tasks_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if not t.is_dir():
                    continue
                log_files = list(t.glob("**/*.log"))
                if log_files:
                    available_tasks.append({
                        "id": t.name,
                        "type": "task",
                        "log_count": len(log_files),
                        "mtime": t.stat().st_mtime,
                    })

        if output_json:
            print(json.dumps({"runs": available_runs, "tasks": available_tasks}, ensure_ascii=False, indent=2))
        else:
            print("=== Доступные логи Galaxy ===")
            print("\nРан-сессии (data/runs):")
            for r in available_runs[:10]:
                print(f"  - {r['id']:<24} [{r['status']:<10}] ({r['log_count']} логов) {r['goal'][:50]}")
            if available_tasks:
                print("\nЗадачи с логами (data/vault/tasks):")
                for t in available_tasks[:10]:
                    print(f"  - {t['id']:<15} ({t['log_count']} логов)")
            print("\nИспользование: galaxy logs <ID> [--node <name>] [--agent <role>] [-n 50]")
        return 0

    target_dir = None
    if target == "latest":
        if runs_dir.is_dir():
            runs = [p for p in runs_dir.iterdir() if p.is_dir()]
            if runs:
                target_dir = max(runs, key=lambda p: p.stat().st_mtime)
        if not target_dir:
            print("Нет доступных ранов с логами.", file=sys.stderr)
            return 1
    elif (runs_dir / target).is_dir():
        target_dir = runs_dir / target
    elif (vault_tasks_dir / target).is_dir():
        target_dir = vault_tasks_dir / target
    else:
        matched = [p for p in runs_dir.glob(f"*{target}*") if p.is_dir()] if runs_dir.is_dir() else []
        if not matched and vault_tasks_dir.is_dir():
            matched = [p for p in vault_tasks_dir.glob(f"*{target}*") if p.is_dir()]
        if matched:
            target_dir = matched[0]
        else:
            print(f"Сессия или задача '{target}' не найдена.", file=sys.stderr)
            return 1

    log_candidates = []
    for path in target_dir.glob("**/*.log"):
        parts = path.relative_to(target_dir).parts
        stream = path.stem
        node_id = parts[0] if len(parts) > 1 else target_dir.name
        attempt = 1
        for part in parts:
            if part.startswith("attempt-"):
                try:
                    attempt = int(part.split("-")[1])
                except Exception:
                    pass

        if stream_type and stream != stream_type and not getattr(args, "all_streams", False):
            continue
        if filter_node and filter_node.lower() not in node_id.lower():
            continue
        if filter_agent and filter_agent.lower() not in node_id.lower():
            continue
        if filter_attempt is not None and attempt != filter_attempt:
            continue

        log_candidates.append({
            "path": path,
            "node_id": node_id,
            "attempt": attempt,
            "stream": stream,
            "mtime": path.stat().st_mtime,
        })

    if not log_candidates:
        if stream_type == "stdout":
            err_candidates = list(target_dir.glob("**/stderr.log"))
            if err_candidates:
                print(f"Логи {stream_type} не найдены, но есть stderr. Попробуйте с флагом --stderr.", file=sys.stderr)
            else:
                print(f"Логи для '{target}' не найдены.", file=sys.stderr)
        else:
            print(f"Логи для '{target}' не найдены.", file=sys.stderr)
        return 1

    log_candidates.sort(key=lambda item: (item["node_id"], item["attempt"], item["stream"]))

    results = []
    for cand in log_candidates:
        p = cand["path"]
        try:
            raw_text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            raw_text = f"[Ошибка чтения файла {p}: {e}]"

        safe_text = redact_secrets(raw_text)
        all_lines = safe_text.splitlines()
        selected_lines = all_lines[-lines_limit:] if (lines_limit and lines_limit > 0) else all_lines

        results.append({
            "target": target_dir.name,
            "node_id": cand["node_id"],
            "attempt": cand["attempt"],
            "stream": cand["stream"],
            "file": str(p.relative_to(ROOT)),
            "total_lines": len(all_lines),
            "lines": selected_lines,
            "text": "\n".join(selected_lines),
        })

    if output_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    for r in results:
        header = f"=== [{r['target']}] [{r['node_id']}] (Attempt {r['attempt']}, {r['stream']}) — {len(r['lines'])}/{r['total_lines']} строк ==="
        print(header)
        if r["lines"]:
            print(r["text"])
        else:
            print("  <лог пуст>")
        print("=" * len(header) + "\n")

    return 0


def _add_interview_args(p: argparse.ArgumentParser) -> None:
    """Add consistent arguments for interview and grill commands."""
    p.add_argument("idea", nargs="?", help="initial project or feature idea")
    p.add_argument("--project", default="default")
    p.add_argument("--profile", choices=("auto", "general", "product", "engineering", "bug", "research"),
                   default="auto", help="declarative interview profile (default: auto)")
    p.add_argument("--mode", choices=("relentless", "normal"), default="relentless",
                   help="closure mode (default: relentless)")
    p.add_argument("--depth", choices=("quick", "deep", "exhaustive"), default="deep")
    p.add_argument("--engine", choices=("grill-v1", "legacy"), default="grill-v1",
                   help="interrogation engine (default: grill-v1)")
    p.add_argument("--budget", "--max-questions", dest="budget", type=int, default=30,
                   help="question budget limit (default: 30)")
    p.add_argument("--resume", help="session ID to resume")
    p.add_argument("--tree", action="store_true", help="display decision DAG tree")
    p.add_argument("--lang", choices=("ru", "en"), default="ru")
    p.add_argument("--brief-out", help="custom destination path for Goal Brief")
    p.add_argument("--create-plan", action="store_true", help="automatically generate plan upon brief confirmation")
    p.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
    p.add_argument("--model")
    p.add_argument("--plan-out")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=f"Galaxy {VERSION} headless Context OS")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    # plan command
    q = sub.add_parser("plan", help="decompose a goal into a validated DAG")
    q.add_argument("goal"); q.add_argument("--project", default="default")
    q.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
    q.add_argument("--model"); q.add_argument("--constraint", action="append", default=[])
    q.add_argument("--out"); q.add_argument("--direct", action="store_true",
                                             help="skip Sun interview only for an already complete specification")
    q.add_argument("--profile", choices=("auto", "general", "product", "engineering", "bug", "research"),
                   default="auto")
    q.add_argument("--mode", choices=("relentless", "normal"), default="relentless")
    q.add_argument("--depth", choices=("quick", "deep", "exhaustive"), default="deep")
    q.add_argument("--engine", choices=("grill-v1", "legacy"), default="grill-v1")
    q.add_argument("--budget", "--max-questions", dest="budget", type=int, default=30)
    q.add_argument("--brief-out")

    # interview command (defaults to profile auto, mode relentless, engine grill-v1)
    q = sub.add_parser("interview", help="Sun asks detailed questions before planning")
    _add_interview_args(q)

    # grill command (exact alias of interview)
    q = sub.add_parser("grill", help="alias of interview: Sun grill-v1 discovery")
    _add_interview_args(q)

    # interview-start command
    q = sub.add_parser("interview-start", help="start a durable non-interactive Sun interview")
    q.add_argument("idea"); q.add_argument("--project", default="default")
    q.add_argument("--profile", choices=("auto", "general", "product", "engineering", "bug", "research"),
                   default="auto")
    q.add_argument("--mode", choices=("relentless", "normal"), default="relentless")
    q.add_argument("--depth", choices=("quick", "deep", "exhaustive"), default="deep")
    q.add_argument("--engine", choices=("grill-v1", "legacy"), default="grill-v1")
    q.add_argument("--budget", "--max-questions", dest="budget", type=int, default=30)

    # interview-answer command
    q = sub.add_parser("interview-answer", help="answer the pending Sun question(s)")
    q.add_argument("interview_id")
    q.add_argument("answer", nargs="?", default="")
    q.add_argument("--rec", "--accept-recommendations", dest="accept_recommendations", action="store_true")
    q.add_argument("--round-no", type=int)

    # interview-status command
    q = sub.add_parser("interview-status", help="show interview progress or Goal Brief")
    q.add_argument("interview_id")
    q.add_argument("--markdown", action="store_true")
    q.add_argument("--tree", action="store_true")

    # interview-tree command
    q = sub.add_parser("interview-tree", help="show full decision DAG tree")
    q.add_argument("interview_id")
    q.add_argument("--json", action="store_true")

    # interview-pause command
    q = sub.add_parser("interview-pause", help="pause an active interview session")
    q.add_argument("interview_id")

    # interview-resume command
    q = sub.add_parser("interview-resume", help="resume a paused interview session")
    q.add_argument("interview_id")
    q.add_argument("--budget", type=int, default=0, help="additional question budget to add")

    # interview-continue command (alias of interview-resume)
    q = sub.add_parser("interview-continue", help="continue/resume an interview session")
    q.add_argument("interview_id")
    q.add_argument("--budget", type=int, default=0, help="additional question budget to add")

    # interview-finish command
    q = sub.add_parser("interview-finish", help="finalize interview draft immediately")
    q.add_argument("interview_id")

    # dispatch / ask command (natural-language entry point)
    for cmd_name in ("dispatch", "ask"):
        q = sub.add_parser(cmd_name, help="natural-language entry: route to grill-v1 or bypass")
        q.add_argument("query", help="natural language request")
        q.add_argument("--json", action="store_true", help="output JSON response")
        q.add_argument("--project", default="default")
        q.add_argument("--profile", choices=("auto", "general", "product", "engineering", "bug", "research"),
                       default="auto")
        q.add_argument("--mode", choices=("relentless", "normal"), default="relentless")
        q.add_argument("--depth", choices=("quick", "deep", "exhaustive"), default="deep")
        q.add_argument("--budget", "--max-questions", dest="budget", type=int, default=30)
        q.add_argument("--engine", choices=("grill-v1", "legacy"), default="grill-v1")
        q.add_argument("--brief-out")
        q.add_argument("--create-plan", action="store_true")
        q.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
        q.add_argument("--model")
        q.add_argument("--plan-out")

    # interview-confirm command
    q = sub.add_parser("interview-confirm", help="confirm the generated Goal Brief")
    q.add_argument("interview_id"); q.add_argument("--out")

    # interview-revise command
    q = sub.add_parser("interview-revise", help="reopen one Goal Brief category")
    q.add_argument("interview_id"); q.add_argument("category")
    q.add_argument("--answer", help="optional new answer to apply immediately")

    # interview-export command
    q = sub.add_parser("interview-export", help="export complete session data with secret redaction")
    q.add_argument("interview_id")
    q.add_argument("--out", help="destination file path for JSON export")
    q.add_argument("--no-redact", action="store_true", help="disable automatic secret redaction")

    # interview-delete command
    q = sub.add_parser("interview-delete", help="safely delete interview session data")
    q.add_argument("interview_id")
    q.add_argument("--confirm", action="store_true", help="confirm deletion")

    # plan-from-interview command
    q = sub.add_parser("plan-from-interview", help="create a DAG from a confirmed Goal Brief")
    q.add_argument("interview_id")
    q.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
    q.add_argument("--model"); q.add_argument("--out")

    # plan inspect command
    q = sub.add_parser("inspect", help="validate and display a plan")
    q.add_argument("plan")

    # run command
    q = sub.add_parser("run", help="start and execute a plan")
    q.add_argument("plan"); q.add_argument("--models", default=str(DEFAULT_MODELS))
    q.add_argument("--approval-mode", choices=("writes", "once", "off"), default="writes")
    q.add_argument("--concurrency", type=int, default=4)
    q.add_argument("--max-cost", type=float, default=10.0)
    q.add_argument("--max-input-tokens", type=int, default=200_000)
    q.add_argument("--max-output-tokens", type=int, default=80_000)
    q.add_argument("--max-calls", type=int, default=80)
    q.add_argument("--max-seconds", type=int, default=7200)
    q.add_argument("--start-only", action="store_true")
    q.add_argument("--no-team-builder", action="store_true",
                   help="disable automatic Moon delegation for this run")

    q = sub.add_parser("resume", help="continue a durable run")
    q.add_argument("run_id"); q.add_argument("--models", default=str(DEFAULT_MODELS))
    q.add_argument("--concurrency", type=int, default=4)
    q.add_argument("--no-team-builder", action="store_true")

    q = sub.add_parser("status", help="show durable run state")
    q.add_argument("run_id")

    q = sub.add_parser("logs", help="view execution logs of runs, nodes and agents")
    q.add_argument("target", nargs="?", default="latest", help="run ID, task ID, 'latest', or 'list'")
    q.add_argument("--node", help="filter by specific node ID")
    q.add_argument("--agent", help="filter by agent/role name (e.g. sun, earth, venera)")
    q.add_argument("--attempt", type=int, help="attempt number (default: latest attempt)")
    q.add_argument("--stderr", action="store_true", help="show stderr instead of stdout")
    q.add_argument("--all-streams", action="store_true", help="show both stdout and stderr")
    q.add_argument("-n", "--lines", type=int, default=50, help="number of lines from end of log (0 for all, default: 50)")
    q.add_argument("--json", action="store_true", help="output structured JSON")

    q = sub.add_parser("approve", help="approve one waiting node")
    q.add_argument("run_id"); q.add_argument("node_id"); q.add_argument("--note", default="")
    q.add_argument("--resume", action="store_true"); q.add_argument("--models", default=str(DEFAULT_MODELS))
    q.add_argument("--concurrency", type=int, default=4)
    q.add_argument("--no-team-builder", action="store_true")

    q = sub.add_parser("reject", help="reject one waiting node")
    q.add_argument("run_id"); q.add_argument("node_id"); q.add_argument("--note", default="rejected by user")

    q = sub.add_parser("brain-add", help="add a managed memory")
    q.add_argument("kind"); q.add_argument("title"); q.add_argument("summary")
    q.add_argument("--project"); q.add_argument("--tag", action="append", default=[])
    q.add_argument("--importance", type=float, default=.5); q.add_argument("--confirm", action="store_true")

    q = sub.add_parser("brain-search", help="semantic and lexical memory search")
    q.add_argument("query"); q.add_argument("--project"); q.add_argument("--limit", type=int, default=8)

    q = sub.add_parser("brain-show", help="show one memory and its revisions")
    q.add_argument("uid")

    q = sub.add_parser("brain-confirm", help="confirm that a memory is correct")
    q.add_argument("uid")

    q = sub.add_parser("brain-correct", help="supersede a memory with corrected text")
    q.add_argument("uid"); q.add_argument("summary"); q.add_argument("--reason", default="user correction")

    q = sub.add_parser("brain-forget", help="retract a memory without destructive deletion")
    q.add_argument("uid"); q.add_argument("--reason", default="user requested")

    q = sub.add_parser("brain-refresh", help="index Markdown notes from data/vault")
    q.add_argument("--project")

    q = sub.add_parser("brain-stats", help="show second-brain health")

    q = sub.add_parser("goal-add", help="add a long-term goal")
    q.add_argument("title"); q.add_argument("description"); q.add_argument("--project")
    q.add_argument("--priority", type=float, default=.7)
    q.add_argument("--horizon", choices=("long", "quarter", "month", "week", "open"), default="long")

    q = sub.add_parser("goal-list", help="list active long-term goals")
    q.add_argument("--project")

    sub.add_parser("agents", help="show the persistent Galaxy organization")

    q = sub.add_parser("agent-show", help="show one agent, tools and active memory blocks")
    q.add_argument("agent_id"); q.add_argument("--project")

    q = sub.add_parser("moon-spawn", help="create a bounded temporary or persistent subagent")
    q.add_argument("parent_id"); q.add_argument("mission")
    q.add_argument("--name"); q.add_argument("--kind", choices=("temporary", "persistent"), default="temporary")
    q.add_argument("--tool", action="append"); q.add_argument("--run-id")
    q.add_argument("--max-cost", type=float, default=1.0)
    q.add_argument("--max-input-tokens", type=int, default=30_000)
    q.add_argument("--max-output-tokens", type=int, default=10_000)
    q.add_argument("--max-calls", type=int, default=8)

    q = sub.add_parser("moon-list", help="show Moon runs and their durable status")
    q.add_argument("--parent")

    q = sub.add_parser("moon-complete", help="submit a structured Moon result")
    q.add_argument("moon_run_id"); q.add_argument("status", choices=("success", "failure", "blocked"))
    q.add_argument("summary"); q.add_argument("--artifact", action="append", default=[])
    q.add_argument("--finding", action="append", default=[])
    q.add_argument("--memory", action="append", default=[])
    q.add_argument("--evidence", action="append", default=[])
    q.add_argument("--confidence", type=float, default=.5)
    q.add_argument("--input-tokens", type=int, default=0)
    q.add_argument("--output-tokens", type=int, default=0)
    q.add_argument("--cost", type=float, default=0.0)

    q = sub.add_parser("team-run", help="automatically plan and execute a bounded Moon team")
    q.add_argument("parent_id"); q.add_argument("objective")
    q.add_argument("--project"); q.add_argument("--criterion", action="append", default=[])
    q.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
    q.add_argument("--model"); q.add_argument("--force", action="store_true")
    q.add_argument("--max-tasks", type=int, default=4)
    q.add_argument("--max-parallel", type=int, default=2)

    q = sub.add_parser("team-resume", help="resume an interrupted Automatic Team Builder run")
    q.add_argument("team_id"); q.add_argument("--provider", choices=("codex", "antigravity"), default="codex")
    q.add_argument("--model"); q.add_argument("--max-parallel", type=int, default=2)

    q = sub.add_parser("team-status", help="show one durable automatic team")
    q.add_argument("team_id")

    q = sub.add_parser("team-list", help="show recent automatic teams")
    q.add_argument("--parent"); q.add_argument("--limit", type=int, default=20)

    q = sub.add_parser("migrate", help="copy user knowledge from an extracted older build")
    q.add_argument("old_root")

    q = sub.add_parser("guardrail", help="Mars SecOps decision guardrail for command inspection")
    q.add_argument("cmd", help="command to inspect")
    q.add_argument("--context", default="")

    q = sub.add_parser("decide", help="execute a System One decision contract")
    q.add_argument("state", help="context/state string")
    q.add_argument("--question", required=True, help="question instructions")
    q.add_argument("--options", help="comma-separated choice options")
    q.add_argument("--score-levels", type=int, help="score levels (2-10)")

    q = sub.add_parser("route", help="fast DAG branch routing using System One Choice")
    q.add_argument("state", help="current progress state")
    q.add_argument("--candidates", required=True, help="comma-separated candidate nodes")
    q.add_argument("--objective", default="")

    subparser_vault = sub.add_parser("vault", help="manage Galaxy Open Vault knowledge file system")
    subparser_vault.add_argument("action", choices=("sync", "search", "graph", "backlinks", "note", "stats"), help="vault action")
    subparser_vault.add_argument("query", nargs="?", default="", help="query, note id or search string")
    subparser_vault.add_argument("--force", action="store_true", help="force reindex all files")

    q = sub.add_parser("world-sync", help="build/update the living project world model")
    q.add_argument("--root", default=str(ROOT), help="project repository root")
    q.add_argument("--project", default="default")

    q = sub.add_parser("world-status", help="show current project world model statistics")
    q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")

    q = sub.add_parser("world-related", help="show dependency-neighborhood for a file or symbol")
    q.add_argument("seed"); q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")
    q.add_argument("--depth", type=int, default=2); q.add_argument("--limit", type=int, default=30)

    q = sub.add_parser("world-drift", help="compare repository state with the last world-model snapshot")
    q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")

    q = sub.add_parser("future-impact", help="simulate structural blast radius before changing the project")
    q.add_argument("scenario"); q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")
    q.add_argument("--seed", action="append", default=[]); q.add_argument("--depth", type=int, default=3); q.add_argument("--max-nodes", type=int, default=50)

    q = sub.add_parser("attention-build", help="inspect Galaxy 3.2 hybrid retrieval and context selection")
    q.add_argument("task"); q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")
    q.add_argument("--budget-tokens", type=int, default=0, help="0 = adaptive budget")
    q.add_argument("--max-candidates", type=int, default=72); q.add_argument("--max-files", type=int, default=16)
    q.add_argument("--refresh", action="store_true")

    q = sub.add_parser("context-build", help="compile an Attention-Engine task context packet")
    q.add_argument("task"); q.add_argument("--root", default=str(ROOT)); q.add_argument("--project", default="default")
    q.add_argument("--budget-tokens", type=int, default=0, help="0 = adaptive budget"); q.add_argument("--max-files", type=int, default=16)
    q.add_argument("--refresh", action="store_true"); q.add_argument("--out")

    q = sub.add_parser("contextbench-run", help="export Galaxy predictions for the independent ContextBench dataset")
    q.add_argument("dataset", help="official ContextBench .parquet or .jsonl")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--repo", help="local Git repository for a single-repository subset")
    g.add_argument("--repos-dir", help="local Git mirrors under OWNER/REPO")
    q.add_argument("--token-budget", type=int, default=10000); q.add_argument("--max-files", type=int, default=20)
    q.add_argument("--limit", type=int, default=0, help="0 = all tasks")
    q.add_argument("--out", default="benchmarks/contextbench-predictions.jsonl")
    q.add_argument("--evaluate", help="run installed upstream ContextBench evaluator; write scores to this JSONL path")
    q.add_argument("--cache", help="repository cache for upstream evaluator")

    q = sub.add_parser("decision-classify", help="confidence-gated narrow task classification")
    q.add_argument("task"); q.add_argument("--threshold", type=float, default=.90)

    q = sub.add_parser("brain-state", help="mark living memory active, stale or contradicted")
    q.add_argument("uid"); q.add_argument("state", choices=("active", "stale", "contradicted")); q.add_argument("--reason", default="user decision")

    q = sub.add_parser("brain-reconcile", help="detect stale/competing project knowledge from current evidence")
    q.add_argument("--root", default=str(ROOT), help="project repository root")
    q.add_argument("--project", default=None); q.add_argument("--apply", action="store_true", help="apply only high-confidence reconciliation rules")

    sub.add_parser("decision-stats", help="show System One decision layer telemetry")

    sub.add_parser("models", help="show configured model routes")
    sub.add_parser("doctor", help="check local runtime dependencies")
    return p


def main() -> int:
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        known_cmds = {
            "plan", "interview", "grill", "interview-start", "interview-answer",
            "interview-status", "interview-tree", "interview-pause", "interview-resume",
            "interview-continue", "interview-finish", "interview-confirm",
            "interview-revise", "interview-export", "interview-delete",
            "plan-from-interview", "inspect", "run", "resume", "status", "approve",
            "reject", "brain-add", "brain-search", "brain-show", "brain-confirm",
            "brain-correct", "brain-forget", "brain-refresh", "brain-stats",
            "goal-add", "goal-list", "agents", "agent-show",
            "moon-spawn", "moon-list", "moon-complete", "team-run", "team-resume",
            "team-status", "team-list", "migrate", "models", "doctor",
            "dispatch", "ask", "logs",
            "guardrail", "decide", "route", "decision-stats", "vault",
            "world-sync", "world-status", "world-related", "world-drift", "future-impact", "attention-build", "context-build", "contextbench-run", "decision-classify", "brain-state", "brain-reconcile",
        }
        if sys.argv[1] not in known_cmds:
            raw_query = sys.argv[1]
            extra_args = sys.argv[2:]
            sys.argv = [sys.argv[0], "dispatch", raw_query] + extra_args

    args = parser().parse_args()
    try:
        if args.command in {"interview", "grill"}:
            return interactive_interview(args)
        if args.command in {"dispatch", "ask"}:
            classification = classify_request(args.query)
            if getattr(args, "json", False):
                if classification["needs_grill"]:
                    prof = classification["profile"] if args.profile == "auto" else args.profile
                    limit = getattr(args, "budget", 30) or 30
                    if getattr(args, "engine", "grill-v1") == "grill-v1":
                        session = GrillInterview(ROOT).start(
                            idea=args.query,
                            project=args.project,
                            profile=prof,
                            mode=args.mode,
                            depth=args.depth,
                            budget_limit=limit,
                        )
                    else:
                        session = SunInterview(ROOT, engine_type="legacy").start(
                            args.query, args.project, args.depth, limit)
                    res = {
                        "action": "grill",
                        "classification": classification,
                        "session": session,
                    }
                else:
                    res = {
                        "action": "bypass",
                        "classification": classification,
                        "message": f"Запрос не требует интервью ({classification['reason']}).",
                    }
                print(json.dumps(res, ensure_ascii=False, indent=2))
                return 0
            else:
                if classification["needs_grill"]:
                    args.idea = args.query
                    args.resume = None
                    if args.profile == "auto":
                        args.profile = classification["profile"]
                    return interactive_interview(args)
                else:
                    print(f"\n[Galaxy Discovery Bypass] {classification['reason']}")
                    q_lower = args.query.lower()
                    if "doctor" in q_lower:
                        checks = {"python": sys.version.split()[0], "git": shutil.which("git"),
                                  "node": shutil.which("node"), "codex": shutil.which("codex"),
                                  "antigravity": shutil.which("agy")}
                        print(json.dumps(checks, ensure_ascii=False, indent=2))
                    elif "агент" in q_lower or "agent" in q_lower:
                        agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
                        print(json.dumps(agents.tree(), ensure_ascii=False, indent=2))
                    elif "памят" in q_lower or "brain" in q_lower:
                        print(json.dumps(BrainStore(ROOT).search(args.query, project=args.project,
                                                                 limit=5, include_global=True),
                                         ensure_ascii=False, indent=2))
                    elif "задач" in q_lower or "task" in q_lower or "статус" in q_lower or "status" in q_lower:
                        print(json.dumps(BrainStore(ROOT).goals.list(args.project), ensure_ascii=False, indent=2))
                    return 0
        if args.command == "interview-start":
            if getattr(args, "engine", "grill-v1") == "grill-v1":
                limit = getattr(args, "budget", 30) or 30
                session = GrillInterview(ROOT).start(
                    idea=args.idea,
                    project=args.project,
                    profile=getattr(args, "profile", "auto"),
                    mode=getattr(args, "mode", "relentless"),
                    depth=args.depth,
                    budget_limit=limit,
                )
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                session = SunInterview(ROOT, engine_type="legacy").start(
                    args.idea, args.project, args.depth, getattr(args, "budget", 30))
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-answer":
            if args.interview_id.startswith("GRILL-"):
                grill = GrillInterview(ROOT)
                if getattr(args, "accept_recommendations", False):
                    session = grill.answer(args.interview_id, {"accept_recommendations": True})
                else:
                    session = grill.answer(args.interview_id, args.answer)
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                session = SunInterview(ROOT, engine_type="legacy").answer(args.interview_id, args.answer)
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-status":
            if args.interview_id.startswith("GRILL-"):
                grill = GrillInterview(ROOT)
                if getattr(args, "tree", False):
                    print(format_tree(grill.tree(args.interview_id)))
                elif args.markdown:
                    print(grill.markdown(args.interview_id))
                else:
                    print(json.dumps(grill.require(args.interview_id), ensure_ascii=False, indent=2))
                return 0
            else:
                sun = SunInterview(ROOT, engine_type="legacy"); session = sun.require(args.interview_id)
                print(sun.markdown(args.interview_id) if args.markdown else
                      json.dumps(interview_view(session), ensure_ascii=False, indent=2))
                return 0
        if args.command == "interview-tree":
            grill = GrillInterview(ROOT)
            tree_data = grill.tree(args.interview_id)
            if getattr(args, "json", False):
                print(json.dumps(tree_data, ensure_ascii=False, indent=2))
            else:
                print(format_tree(tree_data))
            return 0
        if args.command == "interview-pause":
            if args.interview_id.startswith("GRILL-"):
                session = GrillInterview(ROOT).pause(args.interview_id)
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                session = SunInterview(ROOT, engine_type="legacy").pause(args.interview_id)
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command in {"interview-resume", "interview-continue"}:
            if args.interview_id.startswith("GRILL-"):
                session = GrillInterview(ROOT).resume(args.interview_id, additional_budget=getattr(args, "budget", 0))
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                sun = SunInterview(ROOT, engine_type="legacy")
                sun.next_question(args.interview_id)
                session = sun.require(args.interview_id)
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-finish":
            if args.interview_id.startswith("GRILL-"):
                session = GrillInterview(ROOT).finish(args.interview_id)
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                session = SunInterview(ROOT, engine_type="legacy").finish(args.interview_id)
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-confirm":
            if args.interview_id.startswith("GRILL-"):
                grill = GrillInterview(ROOT)
                session = grill.confirm(args.interview_id)
                path = save_brief(grill, args.interview_id, args.out)
                print(json.dumps({"interview_id": session["id"], "status": session["status"],
                                  "brief": str(path.relative_to(ROOT))}, ensure_ascii=False, indent=2)); return 0
            else:
                sun = SunInterview(ROOT, engine_type="legacy"); session = sun.confirm(args.interview_id)
                path = save_brief(sun, args.interview_id, args.out)
                print(json.dumps({"interview_id": session["id"], "status": session["status"],
                                  "brief": str(path.relative_to(ROOT))}, ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-revise":
            if args.interview_id.startswith("GRILL-"):
                grill = GrillInterview(ROOT)
                session = grill.revise(args.interview_id, args.category, new_answer=getattr(args, "answer", None))
                print(json.dumps(session, ensure_ascii=False, indent=2)); return 0
            else:
                session = SunInterview(ROOT, engine_type="legacy").revise(args.interview_id, args.category)
                print(json.dumps(interview_view(session), ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-export":
            grill = GrillInterview(ROOT)
            exported = grill.export(args.interview_id, redact=not getattr(args, "no_redact", False))
            if getattr(args, "out", None):
                Path(args.out).write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(exported, ensure_ascii=False, indent=2)); return 0
        if args.command == "interview-delete":
            if not getattr(args, "confirm", False):
                print("ERROR: pass --confirm to delete session data.", file=sys.stderr)
                return 1
            grill = GrillInterview(ROOT)
            res = grill.delete(args.interview_id)
            print(json.dumps(res, ensure_ascii=False, indent=2)); return 0
        if args.command == "plan-from-interview":
            if args.interview_id.startswith("GRILL-"):
                sun = GrillInterview(ROOT)
            else:
                sun = SunInterview(ROOT, engine_type="legacy")
            plan, destination = create_plan_from_interview(
                sun, args.interview_id, args.provider, args.model, args.out)
            print(json.dumps({"plan_id": plan.plan_id, "path": str(destination.relative_to(ROOT)),
                              "layers": plan.layers(), "nodes": len(plan.nodes)}, ensure_ascii=False, indent=2)); return 0
        if args.command == "plan":
            if not args.direct:
                args.idea = args.goal
                args.resume = None
                args.create_plan = True
                args.plan_out = args.out
                return interactive_interview(args)
            brain = BrainStore(ROOT)
            memories = brain.context(args.goal, agent="Sun", project=args.project, limit=12)
            trace = ROOT / "data" / "planning" / __import__("uuid").uuid4().hex
            plan = asyncio.run(CLIPlanner(ROOT, args.provider, args.model).plan(
                args.goal, args.project, constraints=args.constraint, memories=memories, evidence=trace))
            destination = Path(args.out) if args.out else ROOT / "data" / "plans" / f"{plan.plan_id.lower()}.json"
            if not destination.is_absolute():
                destination = ROOT / destination
            atomic_json(destination, plan.to_dict())
            print(json.dumps({"plan_id": plan.plan_id, "path": str(destination.relative_to(ROOT)),
                              "layers": plan.layers(), "nodes": len(plan.nodes)}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "inspect":
            plan = ExecutionPlan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
            print(json.dumps({"plan_id": plan.plan_id, "goal": plan.goal, "project": plan.project,
                              "layers": plan.layers(), "nodes": [x.id for x in plan.nodes]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "run":
            plan = ExecutionPlan.from_dict(json.loads(Path(args.plan).read_text(encoding="utf-8")))
            limits = BudgetLimits(args.max_cost, args.max_input_tokens, args.max_output_tokens,
                                  args.max_calls, args.max_seconds)
            runner = engine(args.models, args.concurrency, not args.no_team_builder)
            run_id = runner.start(plan, limits, args.approval_mode)
            run = runner.store.run(run_id) if args.start_only else runner.run(run_id)
            print_run(run)
            return 0 if run["status"] in {"DONE", "IN_PROGRESS"} else 11 if run["status"] == "AWAITING_APPROVAL" else 8
        if args.command == "resume":
            run = engine(args.models, args.concurrency, not args.no_team_builder).run(args.run_id)
            print_run(run)
            return 0 if run["status"] == "DONE" else 11 if run["status"] == "AWAITING_APPROVAL" else 8
        if args.command == "status":
            run = AutonomyStore(ROOT).run(args.run_id)
            if not run: raise ValueError("run not found")
            print_run(run); return 0
        if args.command == "logs":
            return cmd_logs(args)
        if args.command == "approve":
            runner = engine(args.models, args.concurrency, not args.no_team_builder)
            run = runner.approve(args.run_id, args.node_id, args.note)
            if args.resume: run = runner.run(args.run_id)
            print_run(run); return 0 if run["status"] == "DONE" else 11 if run["status"] == "AWAITING_APPROVAL" else 0
        if args.command == "reject":
            runner = engine()
            print_run(runner.reject(args.run_id, args.node_id, args.note)); return 0
        if args.command == "brain-add":
            brain = BrainStore(ROOT)
            memory_id = brain.remember(args.kind, args.title, args.summary, project=args.project,
                                       tags=args.tag, importance=args.importance,
                                       confirmed=args.confirm, confirmed_by="user" if args.confirm else None)
            print(json.dumps(brain.get(memory_id), ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-search":
            print(json.dumps(BrainStore(ROOT).search(args.query, project=args.project,
                                                     limit=args.limit, include_global=True),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-show":
            brain = BrainStore(ROOT); item = brain.get(args.uid)
            if not item: raise ValueError("memory not found")
            print(json.dumps({"memory": item, "history": brain.history(args.uid),
                              "links": brain.links(args.uid)}, ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-confirm":
            print(json.dumps(BrainStore(ROOT).confirm(args.uid), ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-correct":
            print(json.dumps(BrainStore(ROOT).correct(args.uid, args.summary, reason=args.reason),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-forget":
            print(json.dumps(BrainStore(ROOT).forget(args.uid, args.reason),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-refresh":
            try:
                from galaxy_core.vault.engine import VaultEngine
                VaultEngine(ROOT / "data" / "vault").sync()
            except Exception:
                pass
            brain = BrainStore(ROOT); result = brain.ingest_vault()
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        if args.command == "vault":
            from galaxy_core.vault.engine import VaultEngine
            vault = VaultEngine(ROOT / "data" / "vault")
            if args.action == "sync":
                print(json.dumps(vault.sync(force=args.force), ensure_ascii=False, indent=2))
                return 0
            if args.action == "stats":
                print(json.dumps(vault.status(), ensure_ascii=False, indent=2))
                return 0
            if args.action == "search":
                if not args.query:
                    print("Error: query is required for search", file=sys.stderr)
                    return 1
                print(json.dumps(vault.search(args.query), ensure_ascii=False, indent=2))
                return 0
            if args.action == "graph":
                print(json.dumps(vault.get_graph(), ensure_ascii=False, indent=2))
                return 0
            if args.action == "backlinks":
                if not args.query:
                    print("Error: note id is required for backlinks", file=sys.stderr)
                    return 1
                note = vault.read_note(args.query)
                print(json.dumps(note.get("backlinks", []) if note else [], ensure_ascii=False, indent=2))
                return 0
            if args.action == "note":
                if not args.query:
                    print("Error: note id is required", file=sys.stderr)
                    return 1
                note = vault.read_note(args.query)
                if not note:
                    print(json.dumps({"error": "Note not found", "id": args.query}, ensure_ascii=False, indent=2))
                    return 1
                print(json.dumps(note, ensure_ascii=False, indent=2))
                return 0
        if args.command == "brain-stats":
            print(json.dumps(BrainStore(ROOT).cognitive_stats(), ensure_ascii=False, indent=2)); return 0
        if args.command == "goal-add":
            brain = BrainStore(ROOT)
            print(json.dumps(brain.goals.add(args.title, args.description, project=args.project,
                                             priority=args.priority, horizon=args.horizon),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "goal-list":
            print(json.dumps(BrainStore(ROOT).goals.list(args.project), ensure_ascii=False, indent=2)); return 0
        if args.command == "agents":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            print(json.dumps(agents.tree(), ensure_ascii=False, indent=2)); return 0
        if args.command == "agent-show":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            print(json.dumps(agents.context(args.agent_id, project=args.project),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "moon-spawn":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            budget = MoonBudget(args.max_cost, args.max_input_tokens,
                                args.max_output_tokens, args.max_calls)
            moon = SubAgentManager(agents).spawn(
                args.parent_id, args.mission, name=args.name, kind=args.kind,
                tools=args.tool, budget=budget, run_id=args.run_id)
            print(json.dumps(moon, ensure_ascii=False, indent=2)); return 0
        if args.command == "moon-list":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            print(json.dumps(SubAgentManager(agents).list(args.parent),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "moon-complete":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            result = MoonResult(
                args.status, args.summary, artifacts=args.artifact, findings=args.finding,
                memory_candidates=args.memory, evidence=args.evidence,
                confidence=args.confidence, input_tokens=args.input_tokens,
                output_tokens=args.output_tokens, cost_usd=args.cost)
            print(json.dumps(SubAgentManager(agents).complete(args.moon_run_id, result),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "team-run":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            builder = AutomaticTeamBuilder(
                agents, CLITeamPlanner(ROOT, args.provider, args.model),
                CLIMoonExecutor(args.provider, args.model),
                TeamPolicy(max_tasks=args.max_tasks, max_parallel=args.max_parallel))
            outcome = builder.run(args.parent_id, args.objective, project=args.project,
                                  criteria=args.criterion, force=args.force)
            print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
            return 0 if outcome.status in {"SUCCEEDED", "SKIPPED"} else 8
        if args.command == "team-resume":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            builder = AutomaticTeamBuilder(
                agents, CLITeamPlanner(ROOT, args.provider, args.model),
                CLIMoonExecutor(args.provider, args.model),
                TeamPolicy(max_parallel=args.max_parallel))
            team = builder.status(args.team_id)
            outcome = builder.run(team["parent_id"], team["objective"],
                                  resume_team_id=args.team_id)
            print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
            return 0 if outcome.status == "SUCCEEDED" else 8
        if args.command == "team-status":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            dummy = lambda **kwargs: None
            print(json.dumps(AutomaticTeamBuilder(agents, dummy, dummy).status(args.team_id),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "team-list":
            agents = AgentRegistry(ROOT); agents.bootstrap_defaults()
            dummy = lambda **kwargs: None
            print(json.dumps(AutomaticTeamBuilder(agents, dummy, dummy).list(args.parent, args.limit),
                             ensure_ascii=False, indent=2)); return 0
        if args.command == "migrate":
            print(json.dumps(migrate_user_data(args.old_root, ROOT), ensure_ascii=False, indent=2)); return 0
        if args.command == "models":
            print(json.dumps([profile.__dict__ for profile in load_models()], ensure_ascii=False, indent=2)); return 0
        if args.command == "guardrail":
            guardrail = MarsDecisionGuardrail()
            verdict = guardrail.inspect_command(args.cmd, context=args.context)
            print(json.dumps(verdict.to_dict(), ensure_ascii=False, indent=2))
            return 0 if verdict.action == "ALLOW" else 1
        if args.command == "decide":
            engine = DecisionEngine()
            if args.options:
                opts = [o.strip() for o in args.options.split(",") if o.strip()]
                val, dist, conf = engine.ask_choice(args.state, args.question, opts)
                res = {"type": "choice", "selected": val, "confidence": conf, "distribution": dist}
            elif args.score_levels:
                exp, dist, conf = engine.ask_score(args.state, args.question, levels=args.score_levels)
                res = {"type": "score", "expected_score": exp, "confidence": conf, "distribution": dist}
            else:
                val, prob = engine.ask_noul(args.state, args.question)
                res = {"type": "noul", "value": val, "probability": prob}
            print(json.dumps(res, ensure_ascii=False, indent=2)); return 0
        if args.command == "route":
            router = DAGDecisionRouter()
            candidates = [c.strip() for c in args.candidates.split(",") if c.strip()]
            route = router.route_next_branch(args.state, candidates, objective=args.objective)
            print(json.dumps({
                "selected_node": route.selected_node,
                "confidence": route.confidence,
                "distribution": route.distribution,
                "routing_latency_ms": route.routing_latency_ms,
            }, ensure_ascii=False, indent=2)); return 0
        if args.command == "world-sync":
            world = ProjectWorldModel(ROOT, args.root, args.project)
            snap, drift = world.sync_with_drift()
            print(json.dumps({"project": snap.project, "root": snap.root, **snap.stats, "generated_at": snap.generated_at, "drift": drift.to_dict()}, ensure_ascii=False, indent=2)); return 0
        if args.command == "world-status":
            print(json.dumps(ProjectWorldModel(ROOT, args.root, args.project).status(), ensure_ascii=False, indent=2)); return 0
        if args.command == "world-related":
            items = ProjectWorldModel(ROOT, args.root, args.project).related(args.seed, depth=args.depth, limit=args.limit)
            print(json.dumps(items, ensure_ascii=False, indent=2)); return 0
        if args.command == "world-drift":
            report = ProjectWorldModel(ROOT, args.root, args.project).drift()
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2)); return 0
        if args.command == "future-impact":
            world = ProjectWorldModel(ROOT, args.root, args.project)
            scenario = world.future(args.scenario, seeds=args.seed or None, depth=args.depth, max_nodes=args.max_nodes)
            print(json.dumps(scenario.to_dict(), ensure_ascii=False, indent=2)); return 0
        if args.command == "attention-build":
            world = ProjectWorldModel(ROOT, args.root, args.project)
            snap = world.sync() if args.refresh else world.load()
            result = AttentionEngine(args.root, args.project).build(
                args.task, snap, budget_tokens=args.budget_tokens if args.budget_tokens > 0 else None,
                max_candidates=args.max_candidates, max_files=args.max_files,
            )
            print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2)); return 0
        if args.command == "context-build":
            compiler = ContextCompiler(ROOT, args.root, args.project)
            packet = compiler.compile(args.task, budget_tokens=args.budget_tokens, max_files=args.max_files, refresh_world=args.refresh)
            if args.out:
                out = Path(args.out)
                if not out.is_absolute(): out = ROOT / out
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(packet.to_dict(), ensure_ascii=False, indent=2) if out.suffix.lower()==".json" else packet.to_markdown(), encoding="utf-8")
                print(json.dumps({"path": str(out), "estimated_tokens": packet.estimated_tokens, "files": len(packet.files)}, ensure_ascii=False, indent=2))
            else:
                print(packet.to_markdown())
            return 0
        if args.command == "contextbench-run":
            dataset_path = Path(args.dataset)
            if not dataset_path.is_absolute(): dataset_path = ROOT / dataset_path
            out = Path(args.out)
            if not out.is_absolute(): out = ROOT / out
            summary = export_predictions(dataset_path, out, repo=args.repo, repos_dir=args.repos_dir,
                                         limit=args.limit, token_budget=args.token_budget, max_files=args.max_files)
            if args.evaluate and summary["completed"]:
                scores = Path(args.evaluate)
                if not scores.is_absolute(): scores = ROOT / scores
                evaluate_predictions(dataset_path, out, scores, cache=args.cache)
                summary["scores"] = str(scores)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1 if summary["failures"] or not summary["completed"] else 0
        if args.command == "decision-classify":
            from galaxy_core.engine.decisions.fabric import DecisionPolicy
            decision = DecisionFabric(policy=DecisionPolicy(auto_confidence=args.threshold, escalate_below=args.threshold)).classify_task(args.task)
            print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-state":
            brain = BrainStore(ROOT)
            if args.state == "stale": item = brain.mark_stale(args.uid, args.reason)
            elif args.state == "contradicted": item = brain.mark_contradicted(args.uid, args.reason)
            else: item = brain.reactivate(args.uid, args.reason)
            print(json.dumps(item, ensure_ascii=False, indent=2)); return 0
        if args.command == "brain-reconcile":
            brain = BrainStore(ROOT)
            report = MemoryReconciler(brain, args.root).scan(project=args.project, apply=args.apply)
            print(json.dumps(report, ensure_ascii=False, indent=2)); return 0
        if args.command == "decision-stats":
            engine = DecisionEngine()
            print(json.dumps({
                "system_one_decision_engine": "active",
                "primitives": ["Noul", "Choice", "Score"],
                "adapters": ["LocalLLMDecisionAdapter", "TypeSafeJevAdapter"],
                "cache_capacity": engine.cache_size,
                "guardrail": "MarsSecOpsGuardrail (confidence_threshold=0.85)",
            }, ensure_ascii=False, indent=2)); return 0
        if args.command == "doctor":
            checks = {"python": sys.version.split()[0], "git": shutil.which("git"),
                      "node": shutil.which("node"), "codex": shutil.which("codex"),
                      "antigravity": shutil.which("agy"),
                      "decision_layer": "System One (Jev-compatible) Active"}
            print(json.dumps(checks, ensure_ascii=False, indent=2))
            return 0 if checks["git"] else 3
    except (ValueError, KeyError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
