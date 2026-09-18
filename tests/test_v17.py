"""Regression and integration tests for Galaxy 1.7."""
import asyncio, datetime as dt, json, os, shlex, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from system.agent_core import Agent, AgentResult, AGENT_SPECS, GalaxyOrchestrator, GalaxyRuntime, Event, MemoryStore, WorkerMesh
from system.agent_core.agents import parse_result,run_process
from system.agent_core.sandbox import AgentSandbox
from system.agent_core.storage import WorkspaceLock,content_manifest,digest,git_head

class Fixture(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  (self.root/'tasks').mkdir();(self.root/'code.txt').write_text('fixed')
  (self.root/'verify.py').write_text("from pathlib import Path\nassert Path('code.txt').read_text() == 'fixed'\nprint('verified')\n")
  (self.root/'tasks/task-900.md').write_text(f'---\nid: TASK-900\nproject: test\nstage: inbox\nstatus: ready\nassignee: Sun\nverify_command: {shlex.quote(sys.executable)} verify.py\n---\n# Fix calculator implementation\n')
  (self.root/'.gitignore').write_text('traces/\nmemory/\n__pycache__/\ntasks/*/\n')
 def git(self,*args):return subprocess.run(['git',*args],cwd=self.root,text=True,capture_output=True,check=True)
 def init_git(self):
  self.git('init','-q');self.git('config','user.name','Galaxy Test');self.git('config','user.email','test@example.invalid');self.git('add','.');self.git('commit','-qm','initial')
DEFAULT={'Sun':'Earth','Earth':'Moon','Moon':'Mercury','Mercury':None}
def factory(actions=None,seen=None):
 actions=actions or {};seen=seen if seen is not None else []
 class Fake:
  def __init__(self,name,provider,root,evidence):self.name,self.root=name,root
  def run(self,task,text,history,memories=None):
   seen.append((self.name,json.loads(json.dumps(history)),memories));a=actions.get(self.name,{})
   if callable(a):return a(self,history)
   if 'write' in a:(self.root/'code.txt').write_text(a['write'])
   if 'artifact' in a:(self.root/'finding.md').write_text(a['artifact'])
   return AgentResult(self.name,'fake',a.get('status','PASS'),0,'',a.get('next',DEFAULT.get(self.name)),a.get('summary',f'{self.name}: inspected calculator implementation'),['finding.md'] if 'artifact' in a else [],['fixture evidence'])
 return Fake

class ResultTests(Fixture):
 def response(self,**changes):
  v={'status':'PASS','summary':'Detailed finding','next_agent':'Mercury','artifacts':[],'evidence':[]};v.update(changes);return json.dumps(v)
 def test_fail_is_not_pass(self):
  with patch('system.agent_core.agents.shutil.which',return_value='/fake'),patch('system.agent_core.agents.run_process',new=AsyncMock(return_value=(0,self.response(status='FAIL'),'diagnostic'))):r=Agent('Moon','codex',self.root,self.root/'evidence').run('TASK-900','task',[])
  self.assertEqual(r.status,'FAIL')
 def test_invalid_json_and_schema(self):
  for text in ['hello','{}',self.response(status='SUCCESS'),self.response(status=[]),self.response(artifacts='file'),self.response(next_agent='Mars'),self.response()+self.response()]:
   with self.subTest(text=text),self.assertRaises(ValueError):parse_result(text,'Moon')
 def test_fenced_json(self):self.assertEqual(parse_result('```json\n'+self.response()+'\n```','Moon')['summary'],'Detailed finding')
 def test_stderr_not_parsed_as_result(self):
  with patch('system.agent_core.agents.shutil.which',return_value='/fake'),patch('system.agent_core.agents.run_process',new=AsyncMock(return_value=(0,'not json',self.response()))):r=Agent('Moon','codex',self.root,self.root/'evidence').run('TASK-900','task',[])
  self.assertEqual(r.status,'INVALID_RESULT')
 def test_unavailable(self):
  with patch('system.agent_core.agents.shutil.which',return_value=None),patch('system.agent_core.agents.Path.is_file',return_value=False):r=Agent('Sun','codex',self.root,self.root/'evidence').run('TASK-900','task',[])
  self.assertEqual(r.status,'UNAVAILABLE')
 def test_process_timeout(self):
  async def check():
   with self.assertRaises(asyncio.TimeoutError):await run_process([sys.executable,'-c','import time;time.sleep(10)'],self.root,.05)
  asyncio.run(check())

class MemoryTests(Fixture):
 def test_relevance_project_case_and_dedupe(self):
  m=MemoryStore(self.root);a=m.save('Earth','solution','Калькулятор','Исправлено деление',project='one',success=True,qa_pass=True,run_id='r',dedupe_key='one');b=m.save('Earth','solution','duplicate','ignored',dedupe_key='one')
  self.assertEqual(a,b);self.assertEqual(m.search('dinosaurs volcano'),[]);self.assertEqual(m.search('КАЛЬКУЛЯТОР',project='two'),[]);self.assertEqual(m.search('КАЛЬКУЛЯТОР',project='one')[0]['id'],a)
 def test_legacy_migration(self):
  d=self.root/'memory';d.mkdir();c=sqlite3.connect(d/'index.db');c.execute('CREATE TABLE memories(id INTEGER PRIMARY KEY,agent TEXT,kind TEXT,task TEXT,project TEXT,title TEXT,summary TEXT,tags TEXT,success INTEGER,qa_pass INTEGER,created_at TEXT)');c.execute("INSERT INTO memories VALUES(1,'Moon','result',NULL,NULL,'old','status pass','[]',1,1,'old')");c.commit();c.close()
  self.assertEqual(MemoryStore(self.root).recent()[0]['qa_pass'],0)

class QueueTests(Fixture):
 def runtime(self):return GalaxyRuntime(self.root,AGENT_SPECS,lease_seconds=.12)
 def publish(self,r,p='Moon'):return r.publish_sync(Event('work','TASK-900','Sun',p,{}))
 def test_stale_ack_nack_reused_worker(self):
  r=self.runtime();eid=self.publish(r);old=r.claim('Moon','same')
  with r._connect() as c:c.execute('UPDATE events SET lease_until=? WHERE id=?',((r._now()-dt.timedelta(seconds=1)).isoformat(),eid))
  new=r.claim('Moon','same');self.assertFalse(r.ack(eid,'same',old[2]));self.assertFalse(r.nack(eid,'same',old[2],'stale'));self.assertTrue(r.ack(eid,'same',new[2]))
 def test_receive_requires_ack(self):
  r=self.runtime();self.publish(r)
  async def check():
   eid,e,t=await r.receive('Moon','reader');self.assertEqual(r.pending()['Moon'],1);r.ack(eid,'reader',t)
  asyncio.run(check())
 def test_heartbeat(self):
  async def check():
   r=self.runtime();self.publish(r);calls=[]
   async def handle(e):calls.append(e.task);await asyncio.sleep(.32)
   task=asyncio.create_task(WorkerMesh(r,{'Moon':handle}).run_until_idle());await asyncio.sleep(.22);self.assertIsNone(r.claim('Moon','competitor'));await task;self.assertEqual(calls,['TASK-900'])
  asyncio.run(check())
 def test_deadline(self):
  async def check():
   r=self.runtime();self.publish(r);stopped=asyncio.Event()
   async def handler(e):
    try:await asyncio.sleep(10)
    finally:stopped.set()
   start=time.monotonic()
   with self.assertRaises(TimeoutError):await WorkerMesh(r,{'Moon':handler}).run_until_idle(timeout=.06)
   self.assertTrue(stopped.is_set());self.assertLess(time.monotonic()-start,1)
  asyncio.run(check())
 def test_sync_parallelism(self):
  async def check():
   r=self.runtime();self.publish(r,'Ceres');self.publish(r,'Mars');intervals=[]
   def handler(e):
    start=time.monotonic();time.sleep(.15);intervals.append((start,time.monotonic()))
   await WorkerMesh(r,{'Ceres':handler,'Mars':handler}).run_until_idle();self.assertLess(max(x[0] for x in intervals),min(x[1] for x in intervals))
  asyncio.run(check())
 def test_deduplication(self):
  r=self.runtime();e=Event('work','TASK-900','Sun','Moon',{});self.assertEqual(r.publish_sync(e,'unique'),r.publish_sync(e,'unique'));self.assertEqual(r.pending()['Moon'],1)
 def test_failed_handler(self):
  async def check():
   r=self.runtime();self.publish(r)
   async def fail(e):raise ValueError('broken')
   with self.assertRaises(RuntimeError):await WorkerMesh(r,{'Moon':fail}).run_until_idle()
   self.assertEqual(r.history()[0]['status'],'failed')
  asyncio.run(check())
 def test_atomic_checkpoint(self):
  r=self.runtime();eid=self.publish(r);claim=r.claim('Moon','w');s={'run_id':'r','task':'TASK-900','revision':1}
  with self.assertRaises(ValueError):r.checkpoint(s,Event('x','TASK-900','Moon','Unknown',{}),(eid,'w',claim[2]))
  self.assertIsNone(r.load_run('r'));self.assertEqual(r.history()[0]['status'],'processing')
 def test_legacy_migration(self):
  d=self.root/'traces';d.mkdir();c=sqlite3.connect(d/'runtime.db');c.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,created_at TEXT,type TEXT,task TEXT,sender TEXT,recipient TEXT,payload TEXT,status TEXT,consumed_at TEXT)');c.commit();c.close();r=self.runtime();self.publish(r);self.assertIsNotNone(r.claim('Moon','w'))

class OrchestratorTests(Fixture):
 def flow(self,actions=None,seen=None,**kwargs):return GalaxyOrchestrator(self.root,'codex',factory(actions,seen)).run('TASK-900',**kwargs)
 def test_success_context_artifacts_memory(self):
  seen=[];s=self.flow({'Sun':{'summary':'Use exact decimal arithmetic','artifact':'Calculator architecture'}},seen);self.assertEqual(s['status'],'DONE',s);self.assertEqual(seen[1][1][0]['summary'],'Use exact decimal arithmetic');self.assertTrue((self.root/s['history'][0]['artifacts'][0]['saved']).exists())
  r=GalaxyRuntime(self.root,AGENT_SPECS);self.assertEqual(sum(r.pending(s['run_id']).values()),0);self.assertEqual(len(r.history('TASK-900')),4)
  memories=MemoryStore(self.root).recent();self.assertEqual(len(memories),4);self.assertTrue(all(x['qa_pass'] for x in memories));self.assertIn('stage: done',(self.root/'tasks/task-900.md').read_text())
 def test_qa_failure_precedes_mercury(self):
  (self.root/'code.txt').write_text('broken');seen=[];s=self.flow(seen=seen);self.assertEqual(s['status'],'BLOCKED');self.assertNotIn('Mercury',[x[0] for x in seen]);self.assertEqual(s['verification']['status'],'FAIL');self.assertEqual(GalaxyRuntime(self.root,AGENT_SPECS).load_run(s['run_id'])['status'],'BLOCKED');self.assertIn('stage: blocked',(self.root/'tasks/task-900.md').read_text());self.assertFalse(any(x['qa_pass'] for x in MemoryStore(self.root).recent()))
 def test_no_moon_gate(self):
  s=self.flow({'Sun':{'next':'Mercury'}});self.assertEqual(s['status'],'BLOCKED');self.assertIn('Moon',s['reason'])
 def test_missing_verification(self):
  p=self.root/'tasks/task-900.md';p.write_text('\n'.join(x for x in p.read_text().splitlines() if not x.startswith('verify_command:'))+'\n');self.assertEqual(self.flow()['status'],'BLOCKED')
 def test_qa_repair_loop(self):
  count=[0]
  def moon(a,h):
   count[0]+=1;return AgentResult('Moon','fake','FAIL' if count[0]==1 else 'PASS',0,'','Earth' if count[0]==1 else 'Mercury','QA finding',[],[])
  s=self.flow({'Moon':moon});self.assertEqual(s['status'],'DONE',s);self.assertEqual([x['agent'] for x in s['history']],['Sun','Earth','Moon','Earth','Moon','Mercury']);self.assertEqual(s['history'][2]['status'],'FAIL')
 def test_loop_guard(self):self.assertEqual(self.flow({'Earth':{'next':'Neptun'},'Neptun':{'next':'Earth'}},max_visits=2)['status'],'LOOP_GUARD')
 def test_content_enforcement_without_git(self):
  root=self.root
  def moon(a,h):
   (root/'code.txt').write_text('unauthorized');return AgentResult('Moon','fake','PASS',0,'','Mercury','QA',[],[])
  s=self.flow({'Moon':moon});self.assertEqual(s['status'],'BLOCKED');self.assertEqual(s['history'][-1]['status'],'DENIED')
 def test_already_dirty_content_enforcement(self):
  self.init_git();(self.root/'code.txt').write_text('already dirty');self.test_content_enforcement_without_git()
 def test_spec_protected(self):
  def earth(a,h):
   p=a.root/'tasks/task-900.md';p.write_text(p.read_text()+'changed requirements');return AgentResult('Earth','fake','PASS',0,'','Moon','changed task',[],[])
  self.assertIn('specification',self.flow({'Earth':earth})['reason'])
 def test_done_resume_idempotent(self):
  s=self.flow();seen=[];r=self.flow(seen=seen,resume=s['run_id']);self.assertEqual(r['status'],'DONE');self.assertEqual(seen,[]);self.assertEqual(MemoryStore(self.root).stats()['total'],4)
 def test_run_ids(self):self.assertNotEqual(self.flow()['run_id'],self.flow()['run_id'])
 def test_lock(self):
  with WorkspaceLock(self.root):
   with self.assertRaises(RuntimeError):self.flow()
 def test_checkpoint_resume(self):
  class Crash(BaseException):pass
  class Crashing(GalaxyOrchestrator):
   async def _handle(obj,event,receipt):
    if event.recipient=='Earth':await asyncio.sleep(60)
    return await super()._handle(event,receipt)
   def _save(obj,s,next_agent=None,receipt=None):
    super()._save(s,next_agent,receipt)
    if s['revision']==1 and s['inflight'] is None:raise Crash()
  with self.assertRaises(Crash):Crashing(self.root,'codex',factory()).run('TASK-900')
  rid=json.loads((self.root/'tasks/task-900/latest-run.json').read_text())['run_id'];seen=[];s=self.flow(seen=seen,resume=rid);self.assertEqual(s['status'],'DONE',s);self.assertEqual(seen[0][0],'Earth')
 def test_inflight_resume_refuses_replay(self):
  class Crash(BaseException):pass
  def crash(a,h):raise Crash()
  with self.assertRaises(Crash):self.flow({'Earth':crash})
  rid=json.loads((self.root/'tasks/task-900/latest-run.json').read_text())['run_id'];self.assertEqual(self.flow(resume=rid)['status'],'INTERRUPTED')
 def test_finalize(self):
  (self.root/'code.txt').write_text('broken');self.init_git();s=self.flow({'Earth':{'write':'fixed'}});self.assertEqual(s['status'],'DONE',s);r=GalaxyOrchestrator(self.root,'codex').finalize('TASK-900',s['run_id'],'Fix calculator');self.assertEqual(r['status'],'COMMITTED');self.assertEqual(r['files'],['code.txt'])
 def test_finalize_stale(self):
  (self.root/'code.txt').write_text('broken');self.init_git();s=self.flow({'Earth':{'write':'fixed'}});(self.root/'code.txt').write_text('changed after QA')
  with self.assertRaises(ValueError):GalaxyOrchestrator(self.root,'codex').finalize('TASK-900',s['run_id'],'bad')
 def test_mercury_copy_git(self):
  self.init_git()
  with AgentSandbox(self.root,'Mercury') as sb:
   self.assertEqual(sb.mode,'disposable-copy');self.assertIsNotNone(git_head(sb.execution_root));(sb.execution_root/'code.txt').write_text('discard')
  self.assertEqual((self.root/'code.txt').read_text(),'fixed')

class MCPTests(Fixture):
 def request(self,requests,role='Reader'):
  env=dict(os.environ,VAULT_PATH=str(self.root),GALAXY_AGENT=role);env.pop('GALAXY_ALLOW_TERMINAL',None)
  data=''.join(json.dumps({'jsonrpc':'2.0','id':i+1,**r})+'\n' for i,r in enumerate(requests));p=subprocess.run(['node',str(ROOT/'system/mcp/obsidian_bridge.js')],env=env,input=data,text=True,capture_output=True,timeout=5);self.assertEqual(p.returncode,0,p.stderr);return [json.loads(x) for x in p.stdout.splitlines()]
 def test_initialize_ping_list(self):
  r=self.request([{'method':'initialize'},{'method':'ping'},{'method':'tools/list'}]);self.assertEqual(r[0]['result']['serverInfo']['version'],'1.7.0');self.assertEqual(r[1]['result'],{});names={x['name'] for x in r[2]['result']['tools']};self.assertNotIn('filesystem.write',names);self.assertNotIn('terminal.run',names)
 def test_write_commit_denied(self):
  r=self.request([{'method':'tools/call','params':{'name':'filesystem.write','arguments':{'path':'code.txt','content':'bad'}}},{'method':'tools/call','params':{'name':'git.commit','arguments':{'message':'bad'}}}],'Moon');self.assertTrue(all(x['result']['isError'] for x in r));self.assertEqual((self.root/'code.txt').read_text(),'fixed')
 def test_traversal_symlink(self):
  outside=self.root.parent/('outside-'+self.root.name);outside.write_text('outside');self.addCleanup(outside.unlink,missing_ok=True);(self.root/'link').symlink_to(outside)
  r=self.request([{'method':'tools/call','params':{'name':'filesystem.read','arguments':{'path':x}}} for x in ['../'+outside.name,'link']]);self.assertTrue(all(x['result']['isError'] for x in r))
 def test_roles_synchronized(self):
  p=json.loads((ROOT/'system/mcp/role-policy.json').read_text());self.assertEqual({k:p[k] for k in AGENT_SPECS},{k:v['tools'] for k,v in AGENT_SPECS.items()})


class AdditionalRegressionTests(Fixture):
 def test_artifact_available_to_readonly_successor(self):
  def mars(a,h):
   self.assertEqual((a.root/h[-1]['artifacts'][0]['saved']).read_text(),'persistent finding')
   return AgentResult('Mars','fake','PASS',0,'','Earth','Architecture uses previous finding',[],[])
  s=GalaxyOrchestrator(self.root,'codex',factory({'Sun':{'next':'Mars','artifact':'persistent finding'},'Mars':mars})).run('TASK-900')
  self.assertEqual(s['status'],'DONE',s)
 def test_full_eight_role_route(self):
  route=['Sun','Venera','Ceres','Mars','Earth','Neptun','Moon','Mercury']
  actions={a:{'next':b} for a,b in zip(route,route[1:])}
  s=GalaxyOrchestrator(self.root,'codex',factory(actions)).run('TASK-900',route=route)
  self.assertEqual(s['status'],'DONE',s);self.assertEqual([x['agent'] for x in s['history']],route)
 def test_qa_cannot_modify_source_and_pass(self):
  (self.root/'verify.py').write_text("from pathlib import Path\nPath('code.txt').write_text('silently repaired')\n")
  s=GalaxyOrchestrator(self.root,'codex',factory()).run('TASK-900')
  self.assertEqual(s['status'],'BLOCKED');self.assertEqual((self.root/'code.txt').read_text(),'fixed')
 def test_concurrent_claims(self):
  import concurrent.futures
  r=GalaxyRuntime(self.root,AGENT_SPECS);r.publish_sync(Event('work','TASK-900','Sun','Moon',{}))
  with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda n:r.claim('Moon',f'w{n}'),range(4)))
  self.assertEqual(sum(x is not None for x in results),1)
 def test_terminal_disabled_even_for_writer(self):
  env=dict(os.environ,VAULT_PATH=str(self.root),GALAXY_AGENT='Earth');env.pop('GALAXY_ALLOW_TERMINAL',None)
  request={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'terminal.run','arguments':{'argv':[sys.executable,'-c','print(1)']}}}
  p=subprocess.run(['node',str(ROOT/'system/mcp/obsidian_bridge.js')],input=json.dumps(request)+'\n',text=True,capture_output=True,env=env)
  self.assertTrue(json.loads(p.stdout)['result']['isError'])
 def test_finalize_refuses_preexisting_change(self):
  self.init_git();(self.root/'code.txt').write_text('dirty before run')
  s=GalaxyOrchestrator(self.root,'codex',factory({'Earth':{'write':'fixed'}})).run('TASK-900')
  with self.assertRaisesRegex(ValueError,'Pre-existing'):GalaxyOrchestrator(self.root,'codex').finalize('TASK-900',s['run_id'],'bad')
 def test_finalize_new_files_included(self):
  self.init_git()
  def earth(a,h):
   (a.root/'new.py').write_text('VALUE=42\n');return AgentResult('Earth','fake','PASS',0,'','Moon','Added module',['new.py'],[])
  s=GalaxyOrchestrator(self.root,'codex',factory({'Earth':earth})).run('TASK-900')
  result=GalaxyOrchestrator(self.root,'codex').finalize('TASK-900',s['run_id'],'Add module');self.assertEqual(result['files'],['new.py'])
 def test_provider_cli_adapter_with_external_fixture(self):
  if os.name=='nt':self.skipTest('POSIX executable fixture')
  import shutil
  for p in ['solar.py','system']: 
   src=ROOT/p;dest=self.root/p
   if src.is_dir():shutil.copytree(src,dest,ignore=shutil.ignore_patterns('__pycache__'))
   else:shutil.copy2(src,dest)
  bindir=self.root/'fixture-bin';bindir.mkdir();fake=bindir/'codex'
  fake.write_text('#!'+sys.executable+'\n'+'''import json,sys
name=sys.argv[-1].split('You are ',1)[1].split(',',1)[0]
route={'Sun':'Earth','Earth':'Moon','Moon':'Mercury','Mercury':None}
print(json.dumps({'status':'PASS','summary':name+' verified fixture','next_agent':route[name],'artifacts':[],'evidence':['fixture, not live LLM']}))
''');fake.chmod(0o755)
  p=subprocess.run([sys.executable,'solar.py','agent-run','TASK-900','--provider','codex'],cwd=self.root,env=dict(os.environ,PATH=str(bindir)+os.pathsep+os.environ.get('PATH','')),text=True,capture_output=True,timeout=20)
  self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertEqual(json.loads(p.stdout)['status'],'DONE')


class VerificationInputTests(Fixture):
 def test_agent_cannot_weaken_existing_verifier(self):
  (self.root/'code.txt').write_text('broken')
  def earth(a,h):
   (a.root/'verify.py').write_text("print('pretend pass')\n")
   return AgentResult('Earth','fake','PASS',0,'','Moon','Changed verifier',[],[])
  s=GalaxyOrchestrator(self.root,'codex',factory({'Earth':earth})).run('TASK-900')
  self.assertEqual(s['status'],'BLOCKED');self.assertIn('verification inputs',s['verification']['reason'])


class StagedContentTests(Fixture):
 def test_git_filter_cannot_replace_verified_bytes(self):
  (self.root/'code.txt').write_text('broken')
  (self.root/'.gitattributes').write_text('code.txt filter=uppercase\n')
  self.init_git()
  s=GalaxyOrchestrator(self.root,'codex',factory({'Earth':{'write':'fixed'}})).run('TASK-900')
  command=shlex.quote(sys.executable)+' -c '+shlex.quote('import sys;sys.stdout.write(sys.stdin.read().upper())')
  self.git('config','filter.uppercase.clean',command)
  with self.assertRaisesRegex(ValueError,'Staged content differs'):
   GalaxyOrchestrator(self.root,'codex').finalize('TASK-900',s['run_id'],'Do not commit transformed candidate')

if __name__=='__main__':unittest.main(verbosity=2)
