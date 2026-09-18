"""Project-scoped lexical memory with source attribution and verified outcomes."""
from __future__ import annotations
from contextlib import contextmanager
import datetime as dt
import json
from pathlib import Path
import re
import sqlite3

class MemoryStore:
    def __init__(self,root):
        self.root = Path(root)
        self.dir = self.root/'memory'; self.dir.mkdir(parents=True,exist_ok=True)
        self.db = self.dir/'index.db'
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS memories(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, kind TEXT NOT NULL,
              task TEXT, project TEXT, title TEXT NOT NULL, summary TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '[]', success INTEGER NOT NULL DEFAULT 0,
              qa_pass INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              run_id TEXT, source TEXT, dedupe_key TEXT)''')
            cols = {r['name'] for r in c.execute('PRAGMA table_info(memories)')}
            for name in ('run_id','source','dedupe_key'):
                if name not in cols: c.execute(f'ALTER TABLE memories ADD COLUMN {name} TEXT')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe ON memories(dedupe_key)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_memory_project ON memories(project,agent)')
            c.execute('CREATE TABLE IF NOT EXISTS memory_terms(memory_id INTEGER NOT NULL, term TEXT NOT NULL, PRIMARY KEY(memory_id,term))')
            c.execute('CREATE INDEX IF NOT EXISTS idx_memory_term ON memory_terms(term,memory_id)')
            missing = c.execute('SELECT id,title,summary,tags,task FROM memories WHERE id NOT IN (SELECT memory_id FROM memory_terms)').fetchall()
            for row in missing:
                text = ' '.join([row['title'],row['summary'],' '.join(json.loads(row['tags'])),row['task'] or ''])
                c.executemany('INSERT OR IGNORE INTO memory_terms VALUES(?,?)',[(row['id'],term) for term in self.words(text)])
            # Pre-1.7 qa_pass meant an LLM claimed success, not deterministic QA.
            c.execute('UPDATE memories SET qa_pass=0 WHERE run_id IS NULL')

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db,timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback(); raise
        finally:
            c.close()

    def save(self,agent,kind,title,summary,task=None,project=None,tags=None,success=False,
             qa_pass=False,run_id=None,source=None,dedupe_key=None):
        with self._connect() as c:
            cur = c.execute('''INSERT OR IGNORE INTO memories
                (agent,kind,task,project,title,summary,tags,success,qa_pass,created_at,run_id,source,dedupe_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (agent,kind,task,project,title,summary,json.dumps(tags or [],ensure_ascii=False),
                 int(success),int(qa_pass),dt.datetime.now(dt.timezone.utc).isoformat(),run_id,source,dedupe_key))
            memory_id = c.execute('SELECT id FROM memories WHERE dedupe_key=?',(dedupe_key,)).fetchone()[0] if dedupe_key else cur.lastrowid
            if cur.rowcount:
                text = ' '.join([title,summary,' '.join(tags or []),task or ''])
                c.executemany('INSERT OR IGNORE INTO memory_terms VALUES(?,?)',[(memory_id,term) for term in self.words(text)])
            return memory_id

    @staticmethod
    def words(text):
        return set(re.findall(r'[^\W_]{3,}',text.lower(),flags=re.UNICODE))

    def search(self,query,agent=None,limit=6,project=None):
        words = sorted(self.words(query))[:64]
        if not words or limit <= 0: return []
        placeholders = ','.join('?' for _ in words)
        sql = f'SELECT m.*, COUNT(t.term) AS hits FROM memories m JOIN memory_terms t ON t.memory_id=m.id WHERE t.term IN ({placeholders})'
        args = list(words)
        if agent: sql += ' AND m.agent=?'; args.append(agent)
        if project is not None: sql += ' AND m.project=?'; args.append(project)
        sql += ' GROUP BY m.id ORDER BY COUNT(t.term) + m.qa_pass * 0.25 DESC, m.id DESC LIMIT ?'
        args.append(limit)
        with self._connect() as c: rows = [dict(r) for r in c.execute(sql,args)]
        for row in rows:
            row['tags'] = json.loads(row['tags'])
            row['score'] = row.pop('hits') + row['qa_pass'] * 0.25
        return rows

    def recent(self,agent=None,limit=10):
        sql,args = 'SELECT * FROM memories',[]
        if agent: sql += ' WHERE agent=?'; args.append(agent)
        with self._connect() as c:
            rows = [dict(r) for r in c.execute(sql+' ORDER BY id DESC LIMIT ?',[*args,limit])]
        for row in rows: row['tags'] = json.loads(row['tags'])
        return rows

    def stats(self):
        with self._connect() as c:
            return {'total':c.execute('SELECT count(*) FROM memories').fetchone()[0],
                    'by_agent':{r[0]:r[1] for r in c.execute('SELECT agent,count(*) FROM memories GROUP BY agent')}}
