"""Durable mailboxes. Delivery is at-least-once, never exactly-once side effects."""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass, asdict
import datetime as dt
import inspect
import json
from pathlib import Path
import sqlite3
import uuid

@dataclass
class Event:
    type: str
    task: str
    sender: str
    recipient: str
    payload: dict

class GalaxyRuntime:
    def __init__(self, root, planets, lease_seconds=30):
        self.root = Path(root)
        self.planets = tuple(planets)
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError('lease_seconds must be positive')
        self.dir = self.root/'traces'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = self.dir/'runtime.db'
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                type TEXT NOT NULL, task TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                worker_id TEXT, lease_until TEXT, consumed_at TEXT, error TEXT,
                run_id TEXT, dedupe_key TEXT, claim_token TEXT)''')
            cols = {r['name'] for r in c.execute('PRAGMA table_info(events)')}
            for name, ddl in [('attempts','INTEGER NOT NULL DEFAULT 0'),('worker_id','TEXT'),('lease_until','TEXT'),('error','TEXT'),('run_id','TEXT'),('dedupe_key','TEXT'),('claim_token','TEXT')]:
                if name not in cols:
                    c.execute(f'ALTER TABLE events ADD COLUMN {name} {ddl}')
            c.execute('CREATE INDEX IF NOT EXISTS idx_events_mailbox ON events(recipient,status,id)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_event_dedupe ON events(dedupe_key)')
            c.execute('CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, task TEXT NOT NULL, state TEXT NOT NULL)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task)')

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA busy_timeout=10000')
        try:
            yield c
        except BaseException:
            if c.in_transaction:
                c.rollback()
            raise
        finally:
            c.close()

    @staticmethod
    def _now():
        return dt.datetime.now(dt.timezone.utc)
    @staticmethod
    def _iso(value):
        return value.isoformat()

    def _insert(self, c, event, dedupe_key=None):
        if event.recipient not in self.planets:
            raise ValueError(f'unknown recipient: {event.recipient}')
        c.execute('''INSERT OR IGNORE INTO events(created_at,type,task,sender,recipient,payload,run_id,dedupe_key,status)
                  VALUES(?,?,?,?,?,?,?,?,'queued')''', (self._iso(self._now()),event.type,event.task,event.sender,event.recipient,
                  json.dumps(event.payload,ensure_ascii=False),event.payload.get('run_id'),dedupe_key))
        if dedupe_key:
            return c.execute('SELECT id FROM events WHERE dedupe_key=?',(dedupe_key,)).fetchone()[0]
        return c.execute('SELECT last_insert_rowid()').fetchone()[0]

    def publish_sync(self, event, dedupe_key=None):
        with self._connect() as c:
            return self._insert(c,event,dedupe_key)
    async def publish(self, event, dedupe_key=None):
        return self.publish_sync(event,dedupe_key)

    def claim(self, planet, worker_id, run_id=None):
        if planet not in self.planets:
            raise ValueError(f'unknown planet: {planet}')
        now = self._now()
        token = uuid.uuid4().hex
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE events SET status='queued',worker_id=NULL,claim_token=NULL,lease_until=NULL WHERE status='processing' AND lease_until<=?",(self._iso(now),))
            query = "SELECT id FROM events WHERE recipient=? AND status='queued'"
            args = [planet]
            if run_id is not None:
                query += ' AND run_id=?'; args.append(run_id)
            row = c.execute(query+' ORDER BY id LIMIT 1',args).fetchone()
            if not row:
                c.commit(); return None
            c.execute("UPDATE events SET status='processing',worker_id=?,claim_token=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                      (worker_id,token,self._iso(now+dt.timedelta(seconds=self.lease_seconds)),row['id']))
            r = c.execute('SELECT * FROM events WHERE id=?',(row['id'],)).fetchone()
            c.commit()
        return r['id'], Event(r['type'],r['task'],r['sender'],r['recipient'],json.loads(r['payload'])), token

    def renew(self, event_id, worker_id, token):
        now = self._now()
        with self._connect() as c:
            return c.execute("UPDATE events SET lease_until=? WHERE id=? AND status='processing' AND worker_id=? AND claim_token=? AND lease_until>?",
                (self._iso(now+dt.timedelta(seconds=self.lease_seconds)),event_id,worker_id,token,self._iso(now))).rowcount == 1

    def ack(self, event_id, worker_id, token):
        with self._connect() as c:
            return self._ack(c,event_id,worker_id,token)
    def _ack(self,c,event_id,worker_id,token):
        now = self._iso(self._now())
        return c.execute("UPDATE events SET status='consumed',consumed_at=?,lease_until=NULL,error=NULL WHERE id=? AND status='processing' AND worker_id=? AND claim_token=? AND lease_until>?",
                         (now,event_id,worker_id,token,now)).rowcount == 1

    def nack(self, event_id, worker_id, token, error='', retry=True):
        with self._connect() as c:
            return c.execute("UPDATE events SET status=?,worker_id=NULL,claim_token=NULL,lease_until=NULL,error=? WHERE id=? AND status='processing' AND worker_id=? AND claim_token=? AND lease_until>?",
                ('queued' if retry else 'failed',str(error),event_id,worker_id,token,self._iso(self._now()))).rowcount == 1

    async def receive(self, planet, worker_id, timeout=1):
        """Return an unacknowledged (id, event, token); caller owns ACK/NACK."""
        deadline = asyncio.get_running_loop().time()+timeout
        while True:
            got = self.claim(planet,worker_id)
            if got:
                return got
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError()
            await asyncio.sleep(.02)

    def pending(self, run_id=None):
        with self._connect() as c:
            tail, args = (' AND run_id=?',[run_id]) if run_id is not None else ('',[])
            return {p:c.execute("SELECT count(*) FROM events WHERE recipient=? AND status IN ('queued','processing')"+tail,[p,*args]).fetchone()[0] for p in self.planets}

    def history(self, task=None, limit=100):
        query, args = 'SELECT * FROM events', []
        if task:
            query += ' WHERE task=?'; args.append(task)
        with self._connect() as c:
            rows = [dict(r) for r in c.execute(query+' ORDER BY id DESC LIMIT ?',[*args,limit])]
        for row in rows:
            row['payload'] = json.loads(row['payload'])
        return list(reversed(rows))

    def load_run(self, run_id):
        with self._connect() as c:
            row = c.execute('SELECT state FROM runs WHERE id=?',(run_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def checkpoint(self, state, next_event=None, receipt=None):
        """State + receipt + next message share one SQLite transaction."""
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if receipt and not self._ack(c,*receipt):
                raise RuntimeError('Lease lost: refusing stale checkpoint')
            c.execute('INSERT OR REPLACE INTO runs(id,task,state) VALUES(?,?,?)',
                      (state['run_id'],state['task'],json.dumps(state,ensure_ascii=False)))
            if next_event:
                self._insert(c,next_event,f"{state['run_id']}:{state['revision']+1}")
            c.commit()

    def stop_run(self, state):
        with self._connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE events SET status='failed',error='run stopped',lease_until=NULL WHERE run_id=? AND status IN ('queued','processing')",(state['run_id'],))
            c.execute('INSERT OR REPLACE INTO runs(id,task,state) VALUES(?,?,?)',
                      (state['run_id'],state['task'],json.dumps(state,ensure_ascii=False)))
            c.commit()

class PlanetWorker:
    def __init__(self,runtime,planet,handler,worker_id=None,poll_interval=.02,run_id=None,with_receipt=False):
        self.runtime,self.planet,self.handler = runtime,planet,handler
        self.worker_id = worker_id or f'{planet}-{uuid.uuid4().hex[:8]}'
        self.poll_interval,self.run_id,self.with_receipt = poll_interval,run_id,with_receipt
        self.processed = 0
        self.running = False
        self.errors = []

    async def _heartbeat(self,eid,token,work):
        while True:
            await asyncio.sleep(self.runtime.lease_seconds/3)
            if not self.runtime.renew(eid,self.worker_id,token):
                if not work.done():
                    work.cancel()
                return

    async def _invoke(self,event,receipt):
        args = (event,receipt) if self.with_receipt else (event,)
        if inspect.iscoroutinefunction(self.handler):
            return await self.handler(*args)
        result = await asyncio.to_thread(self.handler,*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def run(self, stop_event):
        self.running = True
        try:
            while not stop_event.is_set():
                got = self.runtime.claim(self.planet,self.worker_id,self.run_id)
                if not got:
                    await asyncio.sleep(self.poll_interval); continue
                eid,event,token = got
                work = asyncio.create_task(self._invoke(event,(eid,self.worker_id,token)))
                heartbeat = asyncio.create_task(self._heartbeat(eid,token,work))
                try:
                    await work
                    # Orchestrator handlers ACK within their atomic checkpoint.
                    if self.with_receipt or self.runtime.ack(eid,self.worker_id,token):
                        self.processed += 1
                except asyncio.CancelledError:
                    work.cancel()
                    await asyncio.gather(work,return_exceptions=True)
                    self.runtime.nack(eid,self.worker_id,token,'cancelled; execution may be uncertain',retry=False)
                    raise
                except Exception as exc:
                    self.errors.append(str(exc))
                    self.runtime.nack(eid,self.worker_id,token,exc,retry=False)
                finally:
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat,return_exceptions=True)
        finally:
            self.running = False

class WorkerMesh:
    def __init__(self,runtime,handlers,run_id=None,with_receipt=False):
        self.runtime,self.handlers = runtime,handlers
        self.run_id,self.with_receipt = run_id,with_receipt
        self.stop_event = asyncio.Event()
        self.workers = []

    async def run_until_idle(self,idle_for=.05,timeout=5):
        self.stop_event.clear()
        self.workers = [PlanetWorker(self.runtime,p,h,run_id=self.run_id,with_receipt=self.with_receipt) for p,h in self.handlers.items()]
        tasks = [asyncio.create_task(w.run(self.stop_event)) for w in self.workers]
        loop = asyncio.get_running_loop()
        deadline,idle_since = loop.time()+timeout,None
        try:
            while True:
                for task in tasks:
                    if task.done():
                        task.result()
                errors = [e for w in self.workers for e in w.errors]
                if errors:
                    raise RuntimeError('; '.join(errors))
                if not sum(self.runtime.pending(self.run_id).values()):
                    idle_since = idle_since or loop.time()
                    if loop.time()-idle_since >= idle_for:
                        return {w.planet:w.processed for w in self.workers}
                else:
                    idle_since = None
                if loop.time() >= deadline:
                    raise TimeoutError('Worker mesh deadline exceeded')
                await asyncio.sleep(.01)
        finally:
            self.stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
