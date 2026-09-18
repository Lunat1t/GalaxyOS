#!/usr/bin/env python3
"""Galaxy 1.0 — evidence-first workflow plus executable eight-agent LLM core."""
import argparse, datetime as dt, json, re, shlex, subprocess, shutil, tempfile, os
from pathlib import Path
from system.agent_core import GalaxyOrchestrator, AGENT_SPECS, TOOL_REGISTRY, validate_contracts, MemoryStore, GalaxyRuntime, resolve_cli_binary

from system.agent_core.storage import VERSION, WorkspaceLock, content_manifest, digest, git_head
ROOT = Path(__file__).resolve().parent
TASKS_DIR = ROOT / "tasks"
STAGES = ("inbox", "spec", "dev", "qa", "done", "blocked")
AGENTS = {"spec":"Venera","dev":"Earth","qa":"Moon","done":"Mercury","blocked":"Sun"}
AGENT_BACKENDS = {
    "codex": ["codex", "exec", "--skip-git-repo-check"],
    "antigravity": ["agy", "-p"],
}

def agent_status():
    rows=[]
    for name, cmd in AGENT_BACKENDS.items():
        binary=cmd[0]; rows.append((name, resolve_cli_binary(binary)))
    for name,path in rows: print(f"{name}: {'AVAILABLE ' + path if path else 'UNAVAILABLE'}")
    return 0 if any(path for _,path in rows) else 3

def backend_command(name, prompt):
    if name not in AGENT_BACKENDS: raise ValueError(f"unknown agent backend: {name}")
    configured=AGENT_BACKENDS[name]; binary=resolve_cli_binary(configured[0])
    return [binary or configured[0],*configured[1:],prompt]

def run_agent_backend(tid, name, prompt, cwd=ROOT, label=None):
    if name not in AGENT_BACKENDS: raise ValueError(f"unknown agent backend: {name}")
    binary=AGENT_BACKENDS[name][0]; role=label or f"Earth-{name}"
    if not resolve_cli_binary(binary):
        write_role(tid,role,{"status":"UNAVAILABLE","exit_code":127,"command":binary})
        return 127
    cmd=backend_command(name,prompt); record(tid,"agent_start",backend=name,cwd=str(cwd))
    try:r=subprocess.run(cmd,cwd=cwd,text=True,capture_output=True,check=False,timeout=900)
    except subprocess.TimeoutExpired as e: output=str(e); code=124
    except OSError as e: output=str(e); code=127
    else: output=(r.stdout or '')+(r.stderr or ''); code=r.returncode
    d=evidence_dir(tid); suffix=(label or name).lower().replace('/','-'); log=d/f"agent_{suffix}.log"; log.write_text(output,encoding="utf-8")
    write_role(tid,role,{"status":"PASS" if code==0 else "FAIL","exit_code":code,"trace":log.name,"cwd":str(cwd)})
    record(tid,"agent_end",backend=name,exit_code=code)
    return code

def _git(args,cwd=ROOT): return subprocess.run(["git",*args],cwd=cwd,text=True,capture_output=True,check=False)

def parallel_agents(tid, names, prompt):
    """Run write-capable agents in isolated Git worktrees and verify each candidate.

    No candidate is merged automatically. Results are preserved in agent_comparison.json;
    an operator can inspect and explicitly apply one candidate later.
    """
    if not (ROOT/'.git').exists():
        write_role(tid,'Earth-AgentRouter',{'status':'BLOCKED','reason':'parallel mode requires a Git repository'})
        print(f"{tid.upper()}: parallel agents require .git / Git worktrees")
        return 7
    t=get_task(tid); verify=t.get('verify_command')
    if not verify: raise ValueError('parallel agent mode requires verify_command')
    available=[n for n in names if n in AGENT_BACKENDS and resolve_cli_binary(AGENT_BACKENDS[n][0])]
    if not available:
        write_role(tid,'Earth-AgentRouter',{'status':'UNAVAILABLE','requested':names}); return 127
    base=_git(['rev-parse','HEAD'])
    if base.returncode: return 7
    base_sha=base.stdout.strip(); d=evidence_dir(tid); candidates=[]
    with tempfile.TemporaryDirectory(prefix='galaxy-agents-') as td:
        work=[]
        try:
            for name in available:
                branch=f"galaxy/{tid.lower()}-{name}-{dt.datetime.now().strftime('%Y%m%d%H%M%S')}"
                path=Path(td)/name
                add=_git(['worktree','add','-b',branch,str(path),base_sha])
                if add.returncode:
                    candidates.append({'backend':name,'status':'WORKTREE_FAIL','detail':add.stderr.strip()}); continue
                work.append((name,branch,path))
            # Launch independently after all worktrees exist.
            procs=[]
            for name,branch,path in work:
                cmd=backend_command(name,prompt)
                procs.append((name,branch,path,subprocess.Popen(cmd,cwd=path,text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)))
            for name,branch,path,proc in procs:
                try: output,_=proc.communicate(timeout=900); agent_code=proc.returncode
                except subprocess.TimeoutExpired: proc.kill(); output,_=proc.communicate(); agent_code=124
                (d/f'agent_{name}_parallel.log').write_text(output or '',encoding='utf-8')
                verify_p=subprocess.run(shlex.split(verify),cwd=path,text=True,capture_output=True,check=False,timeout=120)
                _git(['add','-N','.'],cwd=path)
                diff=_git(['diff','--binary',base_sha],cwd=path).stdout
                (d/f'candidate_{name}.diff').write_text(diff,encoding='utf-8')
                candidates.append({'backend':name,'branch':branch,'agent_exit':agent_code,'verify_exit':verify_p.returncode,'verified':verify_p.returncode==0,'diff':f'candidate_{name}.diff'})
        finally:
            for name,branch,path in work:
                _git(['worktree','remove','--force',str(path)])
    report={'time':now(),'task':tid.upper(),'base_sha':base_sha,'mode':'parallel_isolated','candidates':candidates,'auto_merge':False}
    (d/'agent_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_role(tid,'Earth-AgentRouter',{'status':'PASS' if any(x.get('verified') for x in candidates) else 'FAIL','mode':'parallel_isolated','candidate_count':len(candidates),'verified_count':sum(bool(x.get('verified')) for x in candidates),'report':'agent_comparison.json','auto_merge':False})
    print(f"{tid.upper()}: parallel candidates={len(candidates)}, verified={sum(bool(x.get('verified')) for x in candidates)}; no automatic merge")
    # Parallel mode is a comparison stage. Main working tree remains untouched.
    return 10 if any(x.get('verified') for x in candidates) else 8


def run_agents(tid):
    t=get_task(tid)
    names=[x.strip().lower() for x in t.get("agent_backends","").split(",") if x.strip()]
    if not names: return None
    mode=t.get("agent_mode","sequential").lower()
    if mode not in {"sequential","parallel"}: raise ValueError("agent_mode must be sequential or parallel")
    prompt=t.get("agent_prompt") or ("Work on " + tid.upper() + ". Read its task file, implement the specification in this repository, "
                                     "run relevant tests, and do not mark the task done yourself.")
    available=[n for n in names if n in AGENT_BACKENDS and resolve_cli_binary(AGENT_BACKENDS[n][0])]
    if not available:
        write_role(tid,"Earth-AgentRouter",{"status":"UNAVAILABLE","requested":names})
        return 127
    if mode=="parallel": return parallel_agents(tid,names,prompt)
    # First available backend implements. A second backend reviews after the first finishes.
    implementer=available[0]
    code=run_agent_backend(tid,implementer,prompt)
    if code != 0:return code
    if len(available)>1:
        reviewer=available[1]
        review_prompt=("Review the current repository changes for " + tid.upper() + ". Do not rewrite working code unless needed. "
                       "Run relevant tests, identify defects, and fix only defects you can verify. Do not mark the task done.")
        code=run_agent_backend(tid,reviewer,review_prompt)
    return code


def now(): return dt.datetime.now().astimezone().isoformat(timespec="seconds")

def parse_frontmatter(path):
    text=path.read_text(encoding="utf-8")
    if not text.startswith("---\n"): return {}
    end=text.find("\n---",4)
    if end<0:return {}
    data={}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ","\t")):
            k,v=line.split(":",1); data[k.strip()]=v.strip().strip('"\'')
    return data

def body(path):
    text=path.read_text(encoding="utf-8"); end=text.find("\n---",4)
    return text[end+4:] if text.startswith("---\n") and end>=0 else text

def set_frontmatter(path,updates):
    text=path.read_text(encoding="utf-8"); end=text.find("\n---",4)
    if not text.startswith("---\n") or end<0: raise ValueError(f"{path}: valid frontmatter required")
    lines=text[4:end].splitlines(); seen=set(); out=[]
    for line in lines:
        if ":" in line and not line.startswith((" ","\t")):
            key=line.split(":",1)[0].strip()
            if key in updates: out.append(f"{key}: {updates[key]}"); seen.add(key); continue
        out.append(line)
    out += [f"{k}: {v}" for k,v in updates.items() if k not in seen]
    path.write_text("---\n"+"\n".join(out)+text[end:],encoding="utf-8")

def discover_tasks():
    out={}
    for p in sorted(TASKS_DIR.glob("task-*.md")):
        m=parse_frontmatter(p); tid=m.get("id")
        if tid: out[tid.upper()]={"path":p,**m}
    return out

def get_task(tid):
    t=discover_tasks().get(tid.upper())
    if not t: raise KeyError(tid)
    return t

def evidence_dir(tid):
    p=TASKS_DIR/tid.lower(); p.mkdir(parents=True,exist_ok=True); return p

def record(tid,event,**fields):
    payload={"time":now(),"task":tid.upper(),"event":event,**fields}; d=evidence_dir(tid)
    with (d/"execution.jsonl").open("a",encoding="utf-8") as f:f.write(json.dumps(payload,ensure_ascii=False)+"\n")
    with (d/"full_trace.md").open("a",encoding="utf-8") as f:
        f.write(f"- `{payload['time']}` **{event}**"+(" — "+", ".join(f"{k}={v}" for k,v in fields.items()) if fields else "")+"\n")

def write_role(tid,role,payload):
    d=evidence_dir(tid)/"roles"; d.mkdir(exist_ok=True)
    data={"time":now(),"task":tid.upper(),"role":role,**payload}
    (d/f"{role.lower()}.json").write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    record(tid,"role_result",role=role,status=payload.get("status","UNKNOWN"))
    return data

def transition(tid,new_stage,reason="manual"):
    if new_stage not in STAGES: raise ValueError(f"invalid stage: {new_stage}")
    t=get_task(tid); old=t.get("stage","inbox").lower()
    allowed={"inbox":{"spec","blocked"},"spec":{"dev","blocked"},"dev":{"qa","blocked"},"qa":{"dev","done","blocked"},"done":{"dev"},"blocked":{"spec","dev","qa"}}
    if new_stage!=old and new_stage not in allowed.get(old,set()): raise ValueError(f"illegal transition: {old} -> {new_stage}")
    if new_stage=="done" and definition_of_done(tid): raise ValueError("Fresh verification required before done")
    status="done" if new_stage=="done" else "blocked" if new_stage=="blocked" else "ready"
    set_frontmatter(t["path"],{"stage":new_stage,"status":status,"assignee":AGENTS.get(new_stage,"Sun")})
    record(tid,"transition",old=old,new=new_stage,reason=reason)
    print(f"{tid.upper()}: {old} -> {new_stage} [{AGENTS.get(new_stage,'Sun')}]")

def venera(tid):
    t=get_task(tid); text=body(t["path"])
    required=["Requirements","Technical Specification"]
    missing=[x for x in required if re.search(rf"^##+\s+.*{re.escape(x)}",text,re.I|re.M) is None]
    result={"status":"PASS" if not missing else "FAIL","checks":{"required_sections":required,"missing":missing}}
    write_role(tid,"Venera",result); return not missing

def artifact_paths(task):
    text=body(task["path"]); paths=[]
    for m in re.findall(r"`([^`]+)`",text):
        candidate=m.strip()
        if not candidate or candidate.startswith(("python ","node ","/home/","http")) or any(c in candidate for c in "*|<>"):
            continue
        # Accept both nested paths (`src/app.py`) and root-level artifacts (`solar.py`).
        # A backticked token counts as an artifact only when it resolves to a real local file.
        p=(ROOT/candidate).resolve()
        try:p.relative_to(ROOT)
        except ValueError:continue
        if p.exists() and p.is_file(): paths.append(str(p.relative_to(ROOT)))
    return sorted(set(paths))

def run_repair(tid):
    """Run an explicitly declared local repair command and preserve evidence.

    This is deliberately opt-in: Solar never invents shell commands. A task author
    must declare `repair_command` in frontmatter. The command is executed without
    a shell, inside the Galaxy root, and its stdout/stderr are recorded.
    """
    t=get_task(tid); command=t.get("repair_command")
    if not command:
        write_role(tid,"Earth",{"status":"NO_REPAIR","exit_code":None,"command":"","trace":"repair_command missing"})
        return 3
    record(tid,"repair_start",command=command)
    try:r=subprocess.run(shlex.split(command),cwd=ROOT,text=True,capture_output=True,check=False,timeout=120)
    except (OSError,subprocess.TimeoutExpired) as e:
        output=str(e); code=124
    else: output=(r.stdout or "")+(r.stderr or ""); code=r.returncode
    d=evidence_dir(tid); (d/"last_repair.log").write_text(output,encoding="utf-8")
    write_role(tid,"Earth",{"status":"PASS" if code==0 else "FAIL","exit_code":code,"command":command,"trace":"last_repair.log","artifacts":artifact_paths(t)})
    record(tid,"repair_end",exit_code=code)
    if output: print(output,end="" if output.endswith("\n") else "\n")
    print(f"{tid.upper()}: REPAIR {'PASS' if code==0 else 'FAIL'} (exit={code})")
    return code

def earth(tid):
    t=get_task(tid); arts=artifact_paths(t)
    result={"status":"PASS" if arts else "FAIL","artifacts":arts,"count":len(arts),"note":"Inventory only; Solar does not pretend to generate code."}
    write_role(tid,"Earth",result); return bool(arts)

def definition_of_done(tid):
    t=get_task(tid); d=evidence_dir(tid); problems=[]
    latest=d/'latest-run.json'
    if latest.exists():
        try:
            run_id=json.loads(latest.read_text())['run_id']
            state=GalaxyRuntime(ROOT,AGENT_SPECS).load_run(run_id)
            if state and state['status']=='DONE':
                qa=state.get('verification',{})
                if qa.get('status')=='PASS' and qa.get('digest')==digest(content_manifest(ROOT)) and state.get('commit',qa.get('head'))==git_head(ROOT):
                    return []
        except (KeyError,ValueError,OSError): pass
    if not t.get("verify_command"): problems.append("verify_command missing")
    moon=d/"roles"/"moon.json"
    if not moon.exists(): problems.append("Moon evidence missing")
    else:
        try:
            data=json.loads(moon.read_text(encoding="utf-8"))
            if data.get("status")!="PASS" or data.get("exit_code")!=0: problems.append("Moon verification is not PASS/0")
            if data.get('digest') != digest(content_manifest(ROOT)) or data.get('head') != git_head(ROOT): problems.append('Verification is stale or predates v1.7')
        except (OSError,json.JSONDecodeError): problems.append("Moon evidence unreadable")
    if not (d/"last_verify.log").exists(): problems.append("verification log missing")
    return problems

def run_verify(tid,update_state=False):
    t=get_task(tid); command=t.get("verify_command")
    if not command:
        write_role(tid,"Moon",{"status":"NOT_REPRODUCIBLE","exit_code":None,"target":"","trace":"verify_command missing"})
        print(f"{tid.upper()}: NOT REPRODUCIBLE (verify_command is missing)"); return 3
    verification_before=digest(content_manifest(ROOT))
    record(tid,"verify_start",command=command)
    try:r=subprocess.run(shlex.split(command),cwd=ROOT,text=True,capture_output=True,check=False,timeout=120)
    except (OSError,subprocess.TimeoutExpired) as e:
        output=str(e); code=124
    else: output=(r.stdout or "")+(r.stderr or ""); code=r.returncode
    d=evidence_dir(tid); (d/"last_verify.log").write_text(output,encoding="utf-8")
    write_role(tid,"Moon",{"status":"PASS" if code==0 else "FAIL","exit_code":code,"target":command,"trace":"last_verify.log","digest":verification_before,"head":git_head(ROOT)})
    record(tid,"verify_end",exit_code=code)
    if output: print(output,end="" if output.endswith("\n") else "\n")
    print(f"{tid.upper()}: {'PASS' if code==0 else 'FAIL'} (exit={code})")
    if update_state:
        t=get_task(tid); retries=int(t.get("retries_count","0") or 0); limit=int(t.get("circuit_breaker_limit","2") or 2)
        if code==0:
            problems=definition_of_done(tid)
            if problems:
                set_frontmatter(t["path"],{"stage":"blocked","status":"blocked","assignee":"Sun"}); record(tid,"dod_fail",problems="; ".join(problems)); print(f"{tid.upper()}: DoD FAIL — {'; '.join(problems)}"); return 6
            set_frontmatter(t["path"],{"stage":"done","status":"done","assignee":"Mercury"}); write_role(tid,"Mercury",{"status":"CLOSED","qa_exit_code":0,"dod":"PASS"}); record(tid,"workflow_pass",stage="done")
        else:
            retries+=1; stage="blocked" if retries>=limit else "dev"; status="blocked" if stage=="blocked" else "ready"
            set_frontmatter(t["path"],{"stage":stage,"status":status,"assignee":AGENTS[stage],"retries_count":retries}); record(tid,"workflow_fail",retries=retries,limit=limit,next_stage=stage)
    return code

def workflow(tid):
    t=get_task(tid); stage=t.get("stage","inbox").lower(); record(tid,"workflow_start",stage=stage,version=VERSION)
    if stage=="blocked": print(f"{tid.upper()}: BLOCKED — operator action required"); return 5
    if stage=="inbox": transition(tid,"spec","workflow")
    t=get_task(tid); stage=t.get("stage","inbox").lower()
    if stage=="spec":
        if not venera(tid): print(f"{tid.upper()}: SPEC FAIL"); return 4
        transition(tid,"dev","Venera checks passed")
    t=get_task(tid); stage=t.get("stage","").lower()
    if stage=="dev":
        agent_code=run_agents(tid)
        if agent_code==10:
            set_frontmatter(t["path"],{"stage":"blocked","status":"blocked","assignee":"Sun"})
            record(tid,"candidate_selection_required",report="agent_comparison.json")
            print(f"{tid.upper()}: candidate selection required; main working tree was not modified")
            return 10
        if agent_code is not None and agent_code!=0:
            record(tid,"agent_work_fail",exit_code=agent_code); return agent_code
        if t.get("repair_command"):
            repair_code=run_repair(tid)
            if repair_code!=0:
                retries=int(t.get("retries_count","0") or 0)+1; limit=int(t.get("circuit_breaker_limit","2") or 2)
                next_stage="blocked" if retries>=limit else "dev"; status="blocked" if next_stage=="blocked" else "ready"
                set_frontmatter(t["path"],{"stage":next_stage,"status":status,"assignee":AGENTS[next_stage],"retries_count":retries})
                record(tid,"repair_fail",retries=retries,limit=limit,next_stage=next_stage); return repair_code
        if not earth(tid): print(f"{tid.upper()}: DEV EVIDENCE FAIL — no local implementation artifacts found"); return 4
        if not t.get("verify_command"): print(f"{tid.upper()}: cannot enter QA without verify_command"); record(tid,"workflow_blocked",reason="missing verify_command"); return 3
        transition(tid,"qa","Earth repair/artifact checks passed")
    if get_task(tid).get("stage","").lower() in {"qa","done"}: return run_verify(tid,update_state=True)
    return 5

def git_snapshot(tid):
    if not (ROOT/".git").exists(): print("GIT: unavailable (.git directory is not present in this archive)"); return 3
    d=evidence_dir(tid); diff=subprocess.run(["git","diff","--binary"],cwd=ROOT,text=True,capture_output=True); status=subprocess.run(["git","status","--short"],cwd=ROOT,text=True,capture_output=True)
    (d/"patch.diff").write_text(diff.stdout,encoding="utf-8"); (d/"git_status.txt").write_text(status.stdout,encoding="utf-8"); record(tid,"git_snapshot",bytes=len(diff.stdout.encode())); print(f"{tid.upper()}: Git evidence saved"); return 0

def audit(json_mode=False):
    issues=[]; wikilink=re.compile(r"\[\[([^\]|#]+)"); secret=re.compile('(?i)\\b(?:api[_-]?key|secret|password|access_token|auth_token)\\b\\s*[=:]\\s*["\']?[A-Za-z0-9_+/=-]{16,}')
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts: continue
        rel=path.relative_to(ROOT)
        if rel==Path("solar.py") or rel.parts[:2]==(".obsidian","plugins") or rel.parts[:1]==("templates",): continue
        if path.suffix.lower() not in {".md",".json",".py",".js",".txt",".yml",".yaml"}: continue
        try:text=path.read_text(encoding="utf-8")
        except UnicodeDecodeError:continue
        for m in secret.finditer(text):
            value=m.group(0)
            if any(x in value.lower() for x in ("${","<","example","your_")):continue
            issues.append({"kind":"SECRET","path":str(rel),"detail":"possible embedded credential"})
        if path.suffix.lower()==".md":
            for target in wikilink.findall(text):
                target=target.strip()
                if not target or target.startswith(("http://","https://")):continue
                if target.startswith("/home/"):
                    issues.append({"kind":"EXTERNAL_PATH","path":str(rel),"detail":target}); continue
                if target.startswith("galaxy/"):target=target[7:]
                c=ROOT/target; options=[c,c.with_suffix(".md") if not c.suffix else c]
                if not any(p.exists() for p in options): issues.append({"kind":"BROKEN_LINK","path":str(rel),"detail":target})
    for tid,t in discover_tasks().items():
        if t.get("stage","").lower()=="done":
            for problem in definition_of_done(tid):
                issues.append({"kind":"DOD_VIOLATION","path":str(t["path"].relative_to(ROOT)),"detail":problem})
    summary={}
    for i in issues:summary[i["kind"]]=summary.get(i["kind"],0)+1
    if json_mode: print(json.dumps({"version":VERSION,"issues":issues,"summary":summary},ensure_ascii=False,indent=2))
    else:
        print("AUDIT: PASS" if not issues else f"AUDIT: {len(issues)} issue(s) {summary}")
        for i in issues:print(f"- {i['kind']}: {i['path']}: {i['detail']}")
    return 0 if not issues else 1

def agent_contracts(json_mode=False):
    errors=validate_contracts()
    payload={"version":VERSION,"agents":AGENT_SPECS,"tools":TOOL_REGISTRY,"errors":errors}
    if json_mode: print(json.dumps(payload,ensure_ascii=False,indent=2))
    else:
        print(f"Galaxy {VERSION}: {len(AGENT_SPECS)} executable planet agents, {len(TOOL_REGISTRY)} registered tools")
        for name,spec in AGENT_SPECS.items(): print(f"{name}: tools={len(spec['tools'])}, next={','.join(spec['next']) or '-'}")
        print("CONTRACTS:","PASS" if not errors else "FAIL")
    return 0 if not errors else 9

def _agent_stage_hook(tid, agent, state):
    # Connect the agent runtime to the official task state machine.
    # Mercury is intentionally kept at QA until deterministic DoD succeeds.
    stage_map={"Sun":"inbox","Venera":"spec","Ceres":"spec","Mars":"spec",
               "Earth":"dev","Neptun":"dev","Moon":"qa","Mercury":"qa"}
    stage=stage_map[agent]
    t=get_task(tid)
    set_frontmatter(t["path"],{"stage":stage,"status":"in_progress","assignee":agent})
    record(tid,"agent_stage",agent=agent,stage=stage,revision=state.get("revision",0))

def agent_workflow(tid, provider, route=None, resume=None):
    names=[x.strip() for x in route.split(',')] if route else None
    report=GalaxyOrchestrator(ROOT,provider).run(tid,names,resume=resume)
    print(json.dumps({k:v for k,v in report.items() if k not in {'base_manifest','task_text'}},ensure_ascii=False,indent=2))
    return 0 if report.get('status')=='DONE' else 8

def locked_call(fn,*args):
    with WorkspaceLock(ROOT):
        return fn(*args)


def main():
    p=argparse.ArgumentParser(description=f"Galaxy v{VERSION} evidence-first workflow engine"); p.add_argument("--version",action="version",version=VERSION); sub=p.add_subparsers(dest="command",required=True); sub.add_parser("list"); sub.add_parser("agents"); sub.add_parser("agent-contracts")
    q=sub.add_parser("agent-run"); q.add_argument("task"); q.add_argument("--provider",choices=("codex","antigravity"),default="codex"); q.add_argument("--route",help="comma-separated planet route"); q.add_argument("--resume",help="resume a durable run ID")
    q=sub.add_parser("finalize"); q.add_argument("task"); q.add_argument("--run",required=True); q.add_argument("--message",required=True)
    for name in ("verify","run","repair","git-snapshot"):q=sub.add_parser(name);q.add_argument("task")
    q=sub.add_parser("transition");q.add_argument("task");q.add_argument("stage",choices=STAGES);q.add_argument("--reason",default="manual")
    q=sub.add_parser("audit");q.add_argument("--json",action="store_true")
    q=sub.add_parser("memory-search"); q.add_argument("query"); q.add_argument("--agent", choices=tuple(AGENT_SPECS), default=None); q.add_argument("--project",default=None)
    sub.add_parser("memory-stats")
    q=sub.add_parser("runtime-history"); q.add_argument("--task", default=None); q.add_argument("--limit", type=int, default=50)
    sub.add_parser("runtime-status")
    q=sub.add_parser("chat", help="interactive chat with Galaxy core and planet personas")
    q.add_argument("--provider", choices=("codex","antigravity"), default="codex")
    q.add_argument("--role", choices=tuple(AGENT_SPECS), default=None)
    q.add_argument("--task", default=None)
    q.add_argument("--save", metavar="PATH")
    a=p.parse_args()
    try:
        if a.command=="agents": return agent_status()
        if a.command=="agent-contracts": return agent_contracts()
        if a.command=="agent-run": return agent_workflow(a.task,a.provider,a.route,a.resume)
        if a.command=="chat":
            from chat import chat_main
            chat_args = ["--provider", a.provider]
            if a.role: chat_args += ["--role", a.role]
            if a.task: chat_args += ["--task", a.task]
            if a.save: chat_args += ["--save", a.save]
            return chat_main(chat_args)
        if a.command=="finalize":
            print(json.dumps(GalaxyOrchestrator(ROOT,"codex").finalize(a.task,a.run,a.message),ensure_ascii=False,indent=2)); return 0
        if a.command=="memory-search":
            print(json.dumps(MemoryStore(ROOT).search(a.query,agent=a.agent,project=a.project),ensure_ascii=False,indent=2)); return 0
        if a.command=="memory-stats":
            print(json.dumps(MemoryStore(ROOT).stats(),ensure_ascii=False,indent=2)); return 0
        if a.command=="runtime-status":
            rt=GalaxyRuntime(ROOT,AGENT_SPECS); print(json.dumps({"pending":rt.pending()},ensure_ascii=False,indent=2)); return 0
        if a.command=="runtime-history":
            rt=GalaxyRuntime(ROOT,AGENT_SPECS); print(json.dumps(rt.history(task=a.task,limit=max(1,a.limit)),ensure_ascii=False,indent=2)); return 0
        if a.command=="list":
            for tid,t in discover_tasks().items():print(f"{tid}: stage={t.get('stage','?')} status={t.get('status','?')} retries={t.get('retries_count','0')} [{'reproducible' if t.get('verify_command') else 'notes-only'}]")
            return 0
        if a.command=="verify":return locked_call(run_verify,a.task)
        if a.command=="run":return locked_call(workflow,a.task)
        if a.command=="repair":return locked_call(run_repair,a.task)
        if a.command=="transition":locked_call(transition,a.task,a.stage,a.reason);return 0
        if a.command=="git-snapshot":return git_snapshot(a.task)
        return audit(a.json)
    except (KeyError,ValueError,RuntimeError) as e:print(f"ERROR: {e}");return 2
if __name__=="__main__":raise SystemExit(main())
