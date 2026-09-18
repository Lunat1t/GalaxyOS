"""Queue-driven orchestration with durable runs and verified finalization."""
from __future__ import annotations
import asyncio
from dataclasses import asdict
import datetime as dt
import inspect
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import uuid
from .agents import Agent, AGENT_SPECS, validate_contracts, run_process
from .memory import MemoryStore
from .runtime import GalaxyRuntime, Event, WorkerMesh
from .sandbox import AgentSandbox
from .storage import VERSION, WorkspaceLock, atomic_json, content_manifest, digest, git_head

DEFAULT_FULL_ROUTE = ['Sun','Venera','Ceres','Mars','Earth','Neptun','Moon','Mercury']
TERMINAL_STATES = {'DONE','BLOCKED','CONTRACT_ERROR','TASK_NOT_FOUND','LOOP_GUARD','INTERRUPTED'}

def _frontmatter(text):
    if not text.startswith('---\n'): return {}
    end = text.find('\n---',4)
    if end < 0: return {}
    return {line.split(':',1)[0]:line.split(':',1)[1].strip().strip('\"\'')
            for line in text[4:end].splitlines() if ':' in line and not line.startswith((' ','\t'))}

def task_path(root,task):
    if not re.fullmatch(r'TASK-[A-Z0-9][A-Z0-9_-]*',task):
        raise ValueError('Invalid task identifier')
    path = Path(root)/'tasks'/f'{task.lower()}.md'
    if path.is_symlink(): raise ValueError('Task cannot be a symlink')
    return path

class GalaxyOrchestrator:
    def __init__(self,root,provider,agent_factory=Agent,stage_hook=None):
        self.root = Path(root).resolve()
        self.provider,self.agent_factory,self.stage_hook = provider,agent_factory,stage_hook
        self.memory = MemoryStore(self.root)
        self.runtime = GalaxyRuntime(self.root,AGENT_SPECS)

    def _evidence(self,state):
        return self.root/'tasks'/state['task'].lower()/'runs'/state['run_id']

    def _project_state(self,state):
        """SQLite is authoritative; JSON and task frontmatter are projections."""
        evidence = self._evidence(state)
        atomic_json(evidence/'state.json',state)
        atomic_json(self.root/'tasks'/state['task'].lower()/'latest-run.json',{'run_id':state['run_id']})
        path = task_path(self.root,state['task'])
        text = path.read_text(encoding='utf-8')
        end = text.find('\n---',4)
        if not text.startswith('---\n') or end < 0: return
        agent = state.get('current_agent')
        stages = {'Sun':'inbox','Venera':'spec','Ceres':'spec','Mars':'spec','Earth':'dev','Neptun':'dev','Moon':'qa','Mercury':'qa'}
        stage = 'done' if state['status']=='DONE' else 'blocked' if state['status'] in TERMINAL_STATES else stages.get(agent,'inbox')
        updates = {'stage':stage,'status':'done' if stage=='done' else 'blocked' if stage=='blocked' else 'in_progress',
                   'assignee':'Mercury' if stage=='done' else agent or 'Sun'}
        lines,seen = [],set()
        for line in text[4:end].splitlines():
            key = line.split(':',1)[0]
            if key in updates: line=f'{key}: {updates[key]}'; seen.add(key)
            lines.append(line)
        lines.extend(f'{k}: {v}' for k,v in updates.items() if k not in seen)
        # Atomic text replacement, same pattern as atomic_json.
        import os,tempfile
        fd,tmp = tempfile.mkstemp(dir=path.parent,prefix='.task-')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as f:
                f.write('---\n'+'\n'.join(lines)+text[end:]); f.flush(); os.fsync(f.fileno())
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def _event(self,state,agent):
        return Event('agent_step',state['task'],'Solar',agent,{'run_id':state['run_id'],'hop':state['revision']+1})

    def _save(self,state,next_agent=None,receipt=None):
        state['updated_at'] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.runtime.checkpoint(state,self._event(state,next_agent) if next_agent else None,receipt)
        self._project_state(state)

    def _stop(self,state,status,reason,receipt=None):
        state.update(status=status,reason=reason,inflight=None)
        self._save(state,receipt=receipt)

    async def _verify(self,state):
        command = state['verify_command']
        evidence = self._evidence(state)
        if not command:
            return {'status':'FAIL','exit_code':None,'reason':'verify_command missing'}
        before = content_manifest(self.root)
        protected = state.get('verification_inputs',{})
        if any(before.get(path) != value for path,value in protected.items()):
            result = {'status':'FAIL','exit_code':None,'reason':'Existing verification inputs were modified; review test changes separately'}
            atomic_json(evidence/'verification.json',result)
            return result
        head = git_head(self.root)
        try:
            with AgentSandbox(self.root,'Moon') as sandbox:
                qa_before = content_manifest(sandbox.execution_root)
                code,out,err = await run_process(shlex.split(command),sandbox.execution_root,120)
                qa_after = content_manifest(sandbox.execution_root)
            unchanged = before == content_manifest(self.root) and head == git_head(self.root)
            qa_unchanged = qa_before == qa_after
            reason = '' if code == 0 and unchanged and qa_unchanged else 'Verification failed or changed project inputs'
            result = {'status':'PASS' if not reason else 'FAIL','exit_code':code,'reason':reason,
                      'command':command,'digest':digest(before),'head':head,'run_id':state['run_id']}
            (evidence/'verify.log').write_text(out+'\n'+err,encoding='utf-8')
        except (OSError,ValueError,asyncio.TimeoutError) as exc:
            result = {'status':'FAIL','exit_code':124,'reason':str(exc),'command':command,'run_id':state['run_id']}
            (evidence/'verify.log').write_text(str(exc),encoding='utf-8')
        atomic_json(evidence/'verification.json',result)
        return result

    def _capture_artifacts(self,result,sandbox,hop_dir):
        captured = []
        for rel in result.artifacts or []:
            path = Path(rel)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] in {'.git','traces','memory'}:
                raise ValueError(f'Invalid artifact path: {rel}')
            src = sandbox.execution_root/path
            if not src.is_file() or src.is_symlink() or not src.resolve().is_relative_to(sandbox.execution_root.resolve()):
                raise ValueError(f'Artifact not found or outside workspace: {rel}')
            if src.stat().st_size > 10*1024*1024:
                raise ValueError('Artifact exceeds 10 MiB per-file limit')
            dest = hop_dir/'artifacts'/path
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(src,dest)
            captured.append({'path':rel,'saved':dest.relative_to(self.root).as_posix()})
        return captured

    async def _handle(self,event,receipt):
        state = self.runtime.load_run(event.payload['run_id'])
        if not state or state['status'] in TERMINAL_STATES:
            self.runtime.ack(*receipt); return
        name = event.recipient
        hop = state['revision']+1
        if event.payload['hop'] != hop or state['current_agent'] != name:
            self._stop(state,'BLOCKED','Out-of-order event',receipt); return
        if state.get('inflight'):
            self._stop(state,'INTERRUPTED','Uncertain interrupted step: inspect changes, then start a new run',receipt); return
        if hop > state['max_hops'] or state['visits'].get(name,0) >= state['max_visits']:
            self._stop(state,'LOOP_GUARD','Hop/visit limit reached',receipt); return
        if name == 'Mercury':
            if not state['history'] or state['history'][-1]['agent'] != 'Moon' or state['history'][-1]['status'] != 'PASS':
                self._stop(state,'BLOCKED','Mercury gate: latest step must be successful Moon QA',receipt); return
        state['inflight'] = {'hop':hop,'agent':name}
        self._save(state)
        if name == 'Mercury':
            state['verification'] = await self._verify(state)
            self._save(state)
            if state['verification']['status'] != 'PASS':
                self._stop(state,'BLOCKED','Deterministic QA failed before Mercury',receipt); return
        if self.stage_hook:
            self.stage_hook(state['task'],name,state)
        before,head = content_manifest(self.root),git_head(self.root)
        hop_dir = self._evidence(state)/f'{hop:03d}-{name.lower()}'
        hop_dir.mkdir(parents=True,exist_ok=True)
        memories = self.memory.search(state['task_text'],agent=name,project=state['project'],limit=4)
        memories += self.memory.search(state['task_text'],project=state['project'],limit=4)
        memories = list({m['id']:m for m in memories}.values())[:6]
        try:
            with AgentSandbox(self.root,name) as sandbox:
                if sandbox.execution_root != self.root:
                    for previous in state['history']:
                        for artifact in previous['artifacts']:
                            source = self.root/artifact['saved']
                            dest = sandbox.execution_root/artifact['saved']
                            dest.parent.mkdir(parents=True,exist_ok=True)
                            shutil.copy2(source,dest)
                agent = self.agent_factory(name,self.provider,sandbox.execution_root,hop_dir)
                kwargs = {'memories':memories} if 'memories' in inspect.signature(agent.run).parameters else {}
                if hasattr(agent,'run_async'):
                    result = await agent.run_async(state['task'],state['task_text'],state['history'],**kwargs)
                else:
                    result = await asyncio.to_thread(agent.run,state['task'],state['task_text'],state['history'],**kwargs)
                artifacts = self._capture_artifacts(result,sandbox,hop_dir)
                mode = sandbox.mode
        except Exception as exc:
            self._stop(state,'BLOCKED',f'{name}: {exc}',receipt); return
        after = content_manifest(self.root)
        violations = []
        if git_head(self.root) != head: violations.append('Agent changed Git HEAD; commits are reserved for finalize')
        if name not in {'Earth','Neptun'} and before != after: violations.append('Read-only agent changed canonical project')
        if any(before.get(p)!=after.get(p) for p in set(before)|set(after) if p.startswith('tasks/')):
            violations.append('Agent changed task specification')
        status = 'DENIED' if violations else result.status
        item = {'hop':hop,**result.structured(),'status':status,'sandbox':mode,'artifacts':artifacts,
                'violations':violations,'source':hop_dir.relative_to(self.root).as_posix()}
        atomic_json(hop_dir/'result.json',item)
        state['history'].append(item)
        state['revision'] = hop
        state['visits'][name] = state['visits'].get(name,0)+1
        state['inflight'] = None
        state['checkpoint_digest'] = digest(after)
        state['checkpoint_head'] = git_head(self.root)
        if violations:
            self._stop(state,'BLOCKED','; '.join(violations),receipt); return
        if status != 'PASS':
            # An explicit QA failure can request a repair; it remains FAIL in history.
            if name == 'Moon' and status == 'FAIL' and result.next_agent == 'Earth' and not state['route']:
                state['current_agent'] = 'Earth'
                self._save(state,'Earth',receipt); return
            self._stop(state,'BLOCKED',f'{name}: {status}: {result.summary}',receipt); return
        if name == 'Mercury':
            if digest(after) != state['verification']['digest'] or git_head(self.root) != state['verification']['head']:
                self._stop(state,'BLOCKED','Project changed after verification',receipt); return
            state.update(status='DONE',current_agent=None,official_task_status='done',definition_of_done='PASS')
            self._save(state,receipt=receipt)
            self._sync_memories(state)
            return
        if state['route']:
            nxt = state['route'][hop] if hop < len(state['route']) else None
        else:
            nxt = result.next_agent
        if nxt not in AGENT_SPECS[name]['next']:
            self._stop(state,'BLOCKED',f'Illegal or missing transition: {name} -> {nxt}',receipt); return
        state['current_agent'] = nxt
        self._save(state,nxt,receipt)

    def _sync_memories(self,state):
        # Idempotent post-commit projection, rebuilt on resume if the process died here.
        for item in state['history']:
            self.memory.save(item['agent'],'agent_finding',f"{state['task']} — {item['agent']}",
                item['summary']+'\nEvidence: '+'; '.join(item['evidence']),task=state['task'],project=state['project'],
                success=item['status']=='PASS',qa_pass=item['status']=='PASS' and state['status']=='DONE',
                run_id=state['run_id'],source=item['source'],dedupe_key=f"{state['run_id']}:{item['hop']}")

    def run(self,task_id,route=None,max_hops=20,max_visits=4,resume=None,timeout=18000):
        return asyncio.run(self.run_async(task_id,route,max_hops,max_visits,resume,timeout))

    async def run_async(self,task_id,route=None,max_hops=20,max_visits=4,resume=None,timeout=18000):
        task = task_id.upper()
        path = task_path(self.root,task)
        if not path.exists(): return {'status':'TASK_NOT_FOUND','task':task}
        errors = validate_contracts()
        if errors: return {'status':'CONTRACT_ERROR','errors':errors}
        if route and (not all(x in AGENT_SPECS for x in route) or len(route)>max_hops):
            raise ValueError('Invalid fixed route')
        with WorkspaceLock(self.root):
            if resume:
                state = self.runtime.load_run(resume)
                if not state or state['task'] != task: raise ValueError('Run not found for this task')
                self.provider = state['provider']
                if state['status'] in TERMINAL_STATES:
                    if state['status']=='DONE' and digest(content_manifest(self.root))!=state.get('verification',{}).get('digest'):
                        return {**state,'workspace_verified':False,'notice':'Historical result; current workspace has changed'}
                    self._project_state(state)
                    self._sync_memories(state)
                    return state
                if state.get('inflight'):
                    state.update(status='INTERRUPTED',reason='Interrupted provider side effects are uncertain; inspect changes and start a new run')
                    self.runtime.stop_run(state); self._project_state(state); return state
                if digest(content_manifest(self.root)) != state['checkpoint_digest'] or git_head(self.root)!=state['checkpoint_head']:
                    state.update(status='BLOCKED',reason='Workspace changed since checkpoint')
                    self.runtime.stop_run(state); self._project_state(state); return state
                # Safe recovery only when no provider was in flight at last commit.
                with self.runtime._connect() as c:
                    c.execute("UPDATE events SET status='queued',lease_until=NULL,worker_id=NULL,claim_token=NULL WHERE run_id=? AND dedupe_key=? AND status IN ('processing','failed')",(resume,f"{resume}:{state['revision']+1}"))
            else:
                text = path.read_text(encoding='utf-8')
                fm = _frontmatter(text)
                manifest = content_manifest(self.root)
                state = {'version':VERSION,'run_id':uuid.uuid4().hex,'task':task,'provider':self.provider,
                    'project':fm.get('project','default'),'task_text':text,'verify_command':fm.get('verify_command',''),
                    'status':'IN_PROGRESS','current_agent':route[0] if route else 'Sun','revision':0,
                    'history':[],'visits':{},'route':list(route) if route else None,'max_hops':max_hops,'max_visits':max_visits,
                    'inflight':None,'base_manifest':manifest,'checkpoint_digest':digest(manifest),
                    'checkpoint_head':git_head(self.root),'base_head':git_head(self.root)}
                verify_args = set(shlex.split(state['verify_command']))
                state['verification_inputs'] = {p:h for p,h in manifest.items()
                    if p in verify_args or p.startswith('tests/') or Path(p).name.startswith('test_') or Path(p).name.endswith(('.test.js','.test.py'))}
                self._save(state,state['current_agent'])
            self._project_state(state)
            mesh = WorkerMesh(self.runtime,{name:self._handle for name in AGENT_SPECS},run_id=state['run_id'],with_receipt=True)
            try:
                await mesh.run_until_idle(timeout=timeout)
            except (TimeoutError,RuntimeError) as exc:
                state = self.runtime.load_run(state['run_id'])
                state.update(status='INTERRUPTED',reason=str(exc))
                self.runtime.stop_run(state); self._project_state(state)
            state = self.runtime.load_run(state['run_id'])
            if state['status']=='IN_PROGRESS':
                state.update(status='BLOCKED',reason='Queue ended without terminal result')
                self.runtime.stop_run(state)
            self._project_state(state)
            self._sync_memories(state)
            return state

    def finalize(self,task,run_id,message):
        """Explicit local commit of a verified, unchanged, initially clean candidate."""
        if not message.strip(): raise ValueError('Commit message is required')
        with WorkspaceLock(self.root):
            state = self.runtime.load_run(run_id)
            if not state or state['task'] != task.upper() or state['status']!='DONE':
                raise ValueError('A DONE run for this task is required')
            qa = state.get('verification',{})
            current = content_manifest(self.root)
            if qa.get('status')!='PASS' or digest(current)!=qa.get('digest') or git_head(self.root)!=qa.get('head'):
                raise ValueError('Verification is stale; run QA again through agent-run')
            if not state['base_head']: raise ValueError('Git repository with an initial commit required')
            changed = sorted(p for p in set(state['base_manifest'])|set(current) if state['base_manifest'].get(p)!=current.get(p))
            if not changed: raise ValueError('No implementation changes to commit')
            staged = subprocess.run(['git','diff','--cached','--quiet'],cwd=self.root)
            if staged.returncode != 0: raise ValueError('Git index must be clean before finalize')
            # Compare tracked HEAD blobs with baseline content to reject pre-existing changes.
            for p in changed:
                old = subprocess.run(['git','show',f"{state['base_head']}:{p}"],cwd=self.root,capture_output=True)
                import hashlib
                baseline = state['base_manifest'].get(p)
                if (old.returncode==0 and hashlib.sha256(old.stdout).hexdigest()!=baseline) or (old.returncode!=0 and baseline is not None):
                    raise ValueError(f'Pre-existing change in {p}; commit it separately before starting a new run')
            subprocess.run(['git','add','--',*changed],cwd=self.root,check=True)
            staged_paths = subprocess.run(['git','diff','--cached','--name-only','-z'],cwd=self.root,capture_output=True,check=True).stdout.decode().strip('\0').split('\0')
            if set(staged_paths) != set(changed):
                raise ValueError('Index differs from expected candidate; review staged files')
            for path in changed:
                blob = subprocess.run(['git','show',f':{path}'],cwd=self.root,capture_output=True)
                if path not in current:
                    if blob.returncode == 0: raise ValueError('Deleted file remains staged')
                elif blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != current[path]:
                    raise ValueError(f'Staged content differs from verified bytes: {path}; review filters/line endings')
            if digest(content_manifest(self.root))!=qa['digest'] or git_head(self.root)!=qa['head']:
                raise ValueError('Files or HEAD changed while staging; commit refused, review index')
            p = subprocess.run(['git','-c','core.hooksPath=/dev/null' if __import__('os').name!='nt' else 'core.hooksPath=NUL',
                                'commit','-m',message],cwd=self.root,text=True,capture_output=True)
            if p.returncode: raise RuntimeError(p.stderr)
            state['commit'] = git_head(self.root)
            self._save(state)
            return {'status':'COMMITTED','commit':state['commit'],'files':changed,'run_id':run_id}
