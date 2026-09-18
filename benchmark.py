#!/usr/bin/env python3
"""Galaxy 1.7 deterministic workflow benchmark; not an AI autonomy score."""
import json, statistics, subprocess, sys, tempfile, time, shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parent
from system.agent_core.storage import VERSION

def run(cmd,cwd=ROOT):
    t=time.perf_counter(); p=subprocess.run(cmd,cwd=cwd,text=True,capture_output=True); return p,time.perf_counter()-t

def fm(path,key):
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.startswith(key+':'): return line.split(':',1)[1].strip()
    return None

def main():
    n=int(sys.argv[1]) if len(sys.argv)>1 else 20
    samples=[]; failures=0
    for _ in range(n):
        p,elapsed=run([sys.executable,'solar.py','verify','TASK-002']); samples.append(elapsed); failures += p.returncode!=0
    audit,audit_t=run([sys.executable,'solar.py','audit'])
    compile_p,compile_t=run([sys.executable,'-m','py_compile','solar.py','benchmark.py'])
    scenarios={}
    with tempfile.TemporaryDirectory() as td:
        copy=Path(td)/'galaxy'; shutil.copytree(ROOT,copy,ignore=shutil.ignore_patterns('.git','__pycache__'))
        # 1: already-correct work should pass without repair.
        good_check=copy/'autonomy_good.py'; good_check.write_text("print(42)\n",encoding='utf-8')
        good=copy/'tasks'/'task-auto-good.md'
        good.write_text('''---\nid: TASK-AUTO-GOOD\ntitle: Already correct\nstatus: ready\nstage: qa\nassignee: Moon\nretries_count: 0\ncircuit_breaker_limit: 2\nverify_command: python3 autonomy_good.py\n---\n## Requirements\nVerification exits zero.\n## Technical Specification\nArtifact: `solar.py`.\n''',encoding='utf-8')
        pg,_=run([sys.executable,'solar.py','run','TASK-AUTO-GOOD'],copy)
        scenarios['already_correct']={'pass':pg.returncode==0 and fm(good,'stage')=='done','exit':pg.returncode,'stage':fm(good,'stage')}

        # 2: broken file; declared Earth repair changes it, Moon verifies result.
        target=copy/'autonomy_target.txt'; target.write_text('BROKEN\n',encoding='utf-8')
        fixer=copy/'autonomy_fix.py'; fixer.write_text("from pathlib import Path\nPath('autonomy_target.txt').write_text('FIXED\\n', encoding='utf-8')\nprint('fixed')\n",encoding='utf-8')
        check=copy/'autonomy_check.py'; check.write_text("from pathlib import Path\nraise SystemExit(0 if Path('autonomy_target.txt').read_text().strip()=='FIXED' else 1)\n",encoding='utf-8')
        repair=copy/'tasks'/'task-auto-repair.md'
        repair.write_text('''---\nid: TASK-AUTO-REPAIR\ntitle: Repair a broken artifact\nstatus: ready\nstage: dev\nassignee: Earth\nretries_count: 0\ncircuit_breaker_limit: 2\nrepair_command: python3 autonomy_fix.py\nverify_command: python3 autonomy_check.py\n---\n## Requirements\nChange autonomy_target.txt from BROKEN to FIXED.\n## Technical Specification\nArtifacts: `autonomy_target.txt`, `autonomy_fix.py`, `autonomy_check.py`.\n''',encoding='utf-8')
        pr,_=run([sys.executable,'solar.py','run','TASK-AUTO-REPAIR'],copy)
        scenarios['declared_repair']={'pass':pr.returncode==0 and fm(repair,'stage')=='done' and target.read_text().strip()=='FIXED','exit':pr.returncode,'stage':fm(repair,'stage')}

        # 3: impossible verification should stop after retry limit.
        bad=copy/'tasks'/'task-auto-bad.md'
        bad.write_text('''---\nid: TASK-AUTO-BAD\ntitle: Intentional permanent failure\nstatus: ready\nstage: qa\nassignee: Moon\nretries_count: 0\ncircuit_breaker_limit: 2\nverify_command: python3 definitely_missing_benchmark_file.py\n---\n## Requirements\nFail deterministically.\n## Technical Specification\nArtifact: `solar.py`.\n''',encoding='utf-8')
        p1,_=run([sys.executable,'solar.py','run','TASK-AUTO-BAD'],copy)
        p2,_=run([sys.executable,'solar.py','run','TASK-AUTO-BAD'],copy)
        scenarios['circuit_breaker']={'pass':fm(bad,'stage')=='blocked' and fm(bad,'retries_count')=='2' and p1.returncode!=0 and p2.returncode!=0,'exit_sequence':[p1.returncode,p2.returncode],'stage':fm(bad,'stage')}

    autonomy_passes=sum(1 for x in scenarios.values() if x['pass'])
    result={'version':VERSION,'verify_runs':n,'verify_passes':n-failures,'repeatability_pct':round((n-failures)*100/n,2),
      'verify_latency_ms':{'mean':round(statistics.mean(samples)*1000,2),'median':round(statistics.median(samples)*1000,2),'min':round(min(samples)*1000,2),'max':round(max(samples)*1000,2)},
      'audit_pass':audit.returncode==0,'audit_ms':round(audit_t*1000,2),'py_compile_pass':compile_p.returncode==0,'compile_ms':round(compile_t*1000,2),
      'workflow_scenarios':scenarios,'workflow_scenarios_passed':autonomy_passes,'live_llm_tested':False,
      'overall_pass':failures==0 and audit.returncode==0 and compile_p.returncode==0 and autonomy_passes==len(scenarios)}
    (ROOT/'benchmark-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2)); return 0 if result['overall_pass'] else 1
if __name__=='__main__': raise SystemExit(main())
