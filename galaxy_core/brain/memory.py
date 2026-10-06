"""Galaxy 2.0 managed cognitive memory.

The store keeps provenance, confidence, confirmation, revisions and lifecycle state.
It remains dependency-free: SQLite/FTS5 is used when available and a weighted lexical
index is always maintained as a portable fallback.
"""
from __future__ import annotations
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3

MEMORY_KINDS = {
    'fact','decision','preference','constraint','procedure','insight','lesson','question',
    'reference','note','context','outcome','verification','solution','result','agent_finding',
    'goal','commitment','idea'
}
MEMORY_STATUSES = {'active','stale','contradicted','superseded','retracted'}
RELATIONS = {'relates_to','supports','contradicts','derived_from','supersedes','superseded_by'}

class MemoryStore:
    """Managed, project-aware knowledge store used by agents and the CLI."""

    def __init__(self, root):
        self.root = Path(root)
        self.dir = self.root/'data'/'brain'; self.dir.mkdir(parents=True,exist_ok=True)
        self.db = self.dir/'index.db'
        self.fts = False
        self._init_schema()

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db,timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA foreign_keys=ON')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback(); raise
        finally:
            c.close()

    @staticmethod
    def _now(): return dt.datetime.now(dt.timezone.utc).isoformat()

    def _init_schema(self):
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS memories(
              id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT NOT NULL, kind TEXT NOT NULL,
              task TEXT, project TEXT, title TEXT NOT NULL, summary TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '[]', success INTEGER NOT NULL DEFAULT 0,
              qa_pass INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
              run_id TEXT, source TEXT, dedupe_key TEXT)''')
            cols = {r['name'] for r in c.execute('PRAGMA table_info(memories)')}
            additions = {
                'run_id':'TEXT','source':'TEXT','dedupe_key':'TEXT','uid':'TEXT',
                'status':"TEXT NOT NULL DEFAULT 'active'",'source_type':"TEXT NOT NULL DEFAULT 'legacy'",
                'source_ref':'TEXT','source_hash':'TEXT','confidence':'REAL NOT NULL DEFAULT 0.5',
                'importance':'REAL NOT NULL DEFAULT 0.5','confirmed_at':'TEXT','confirmed_by':'TEXT',
                'updated_at':'TEXT','valid_from':'TEXT','valid_to':'TEXT','supersedes_uid':'TEXT',
                'metadata':"TEXT NOT NULL DEFAULT '{}'",'access_count':'INTEGER NOT NULL DEFAULT 0',
                'last_accessed_at':'TEXT'
            }
            for name, typ in additions.items():
                if name not in cols: c.execute(f'ALTER TABLE memories ADD COLUMN {name} {typ}')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_dedupe ON memories(dedupe_key)')
            c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_uid ON memories(uid)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_memory_project ON memories(project,agent)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_memory_status ON memories(status,kind,project)')
            c.execute('''CREATE TABLE IF NOT EXISTS memory_terms(
                memory_id INTEGER NOT NULL, term TEXT NOT NULL, weight REAL NOT NULL DEFAULT 1.0,
                PRIMARY KEY(memory_id,term), FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE)''')
            term_cols={r['name'] for r in c.execute('PRAGMA table_info(memory_terms)')}
            if 'weight' not in term_cols: c.execute('ALTER TABLE memory_terms ADD COLUMN weight REAL NOT NULL DEFAULT 1.0')
            c.execute('CREATE INDEX IF NOT EXISTS idx_memory_term ON memory_terms(term,memory_id)')
            c.execute('''CREATE TABLE IF NOT EXISTS memory_links(
                from_uid TEXT NOT NULL, to_uid TEXT NOT NULL, relation TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(from_uid,to_uid,relation))''')
            c.execute('''CREATE TABLE IF NOT EXISTS memory_revisions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, action TEXT NOT NULL,
                changed_at TEXT NOT NULL, reason TEXT, changes TEXT NOT NULL DEFAULT '{}')''')
            c.execute('''CREATE TABLE IF NOT EXISTS memory_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, action TEXT NOT NULL,
                changed_at TEXT NOT NULL, reason TEXT)''')
            # Pre-1.7 qa_pass meant an LLM claimed success, not deterministic QA.
            c.execute('UPDATE memories SET qa_pass=0 WHERE run_id IS NULL AND qa_pass<>0')
            rows=c.execute('SELECT * FROM memories ORDER BY id').fetchall()
            for row in rows:
                updates={}
                if not row['uid']: updates['uid']=f"MEM-{row['id']:08d}"
                if not row['updated_at']: updates['updated_at']=row['created_at'] or self._now()
                if row['source_ref'] is None and row['source'] is not None: updates['source_ref']=row['source']
                if row['source_type'] in (None,'legacy') and row['run_id']:
                    updates['source_type']='agent_run'
                if row['qa_pass']:
                    updates['confidence']=max(float(row['confidence'] or 0),0.95)
                    if not row['confirmed_at']: updates['confirmed_at']=row['created_at'] or self._now()
                    if not row['confirmed_by']: updates['confirmed_by']='deterministic_qa'
                elif row['success']:
                    updates['confidence']=max(float(row['confidence'] or 0),0.7)
                if updates:
                    c.execute('UPDATE memories SET '+','.join(f'{k}=?' for k in updates)+' WHERE id=?',[*updates.values(),row['id']])
            # FTS5 is optional. Weighted token index remains the guaranteed fallback.
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title,summary,tags,task,project, tokenize='unicode61')")
                self.fts=True
            except sqlite3.OperationalError:
                self.fts=False
            self._rebuild_indexes(c)

    @staticmethod
    def words(text):
        return set(re.findall(r'[^\W_]{3,}',(text or '').lower(),flags=re.UNICODE))

    @staticmethod
    def _json(value, default):
        try: return json.loads(value) if value else default
        except (TypeError,json.JSONDecodeError): return default

    @staticmethod
    def _frontmatter(text):
        if not text.startswith('---\n'): return {},text
        end=text.find('\n---',4)
        if end < 0: return {},text
        data={}
        for line in text[4:end].splitlines():
            if ':' in line and not line.startswith((' ','\t')):
                k,v=line.split(':',1);data[k.strip()]=v.strip().strip('"\'')
        return data,text[end+4:].lstrip('\n')

    @staticmethod
    def _tags(value):
        if not value:return []
        if isinstance(value,list):return [str(x).strip() for x in value if str(x).strip()]
        s=str(value).strip()
        try:
            parsed=json.loads(s)
            if isinstance(parsed,list):return [str(x).strip() for x in parsed if str(x).strip()]
        except json.JSONDecodeError:pass
        return [x.strip().lstrip('#') for x in re.split(r'[,; ]+',s) if x.strip()]

    def _term_weights(self,title,summary,tags,task=None,project=None):
        weights={}
        for text,weight in ((title,3.0),(summary,1.0),(' '.join(tags or []),2.0),(task or '',1.25),(project or '',1.25)):
            for term in self.words(text): weights[term]=max(weights.get(term,0),weight)
        return weights

    def _index_one(self,c,row):
        tags=self._json(row['tags'],[])
        weights=self._term_weights(row['title'],row['summary'],tags,row['task'],row['project'])
        c.execute('DELETE FROM memory_terms WHERE memory_id=?',(row['id'],))
        c.executemany('INSERT INTO memory_terms(memory_id,term,weight) VALUES(?,?,?)',[(row['id'],term,w) for term,w in weights.items()])
        if self.fts:
            c.execute('DELETE FROM memory_fts WHERE rowid=?',(row['id'],))
            c.execute('INSERT INTO memory_fts(rowid,title,summary,tags,task,project) VALUES(?,?,?,?,?,?)',
                      (row['id'],row['title'],row['summary'],' '.join(tags),row['task'] or '',row['project'] or ''))

    def _rebuild_indexes(self,c):
        # Small local stores favor correctness over clever incremental migration.
        c.execute('DELETE FROM memory_terms')
        if self.fts:c.execute('DELETE FROM memory_fts')
        for row in c.execute('SELECT * FROM memories'):
            self._index_one(c,row)

    def _record_revision(self,c,uid,action,reason=None,changes=None):
        c.execute('INSERT INTO memory_revisions(uid,action,changed_at,reason,changes) VALUES(?,?,?,?,?)',
                  (uid,action,self._now(),reason,json.dumps(changes or {},ensure_ascii=False)))

    def _row(self,row,score=None):
        d=dict(row);d['tags']=self._json(d.get('tags'),[]);d['metadata']=self._json(d.get('metadata'),{})
        d['verified']=bool(d.get('qa_pass'));d['confirmed']=bool(d.get('confirmed_at'))
        if score is not None:d['score']=round(float(score),4)
        return d

    def get(self,uid_or_id):
        with self._connect() as c:
            if isinstance(uid_or_id,int) or str(uid_or_id).isdigit():row=c.execute('SELECT * FROM memories WHERE id=?',(int(uid_or_id),)).fetchone()
            else:row=c.execute('SELECT * FROM memories WHERE uid=?',(str(uid_or_id),)).fetchone()
        return self._row(row) if row else None

    def remember(self,kind,title,summary,*,agent='User',task=None,project=None,tags=None,
                 success=False,qa_pass=False,run_id=None,source_type='manual',source_ref=None,
                 source_hash=None,confidence=0.75,importance=0.5,confirmed=False,confirmed_by=None,
                 valid_from=None,valid_to=None,metadata=None,dedupe_key=None,supersedes_uid=None,replace_existing=False):
        if kind not in MEMORY_KINDS: raise ValueError(f'Unknown memory kind: {kind}')
        title=(title or '').strip();summary=(summary or '').strip()
        if not title or not summary:raise ValueError('Memory title and summary are required')
        confidence=max(0.0,min(1.0,float(confidence)));importance=max(0.0,min(1.0,float(importance)))
        tags=self._tags(tags);now=self._now();confirmed_at=now if confirmed else None
        content_hash=hashlib.sha256((title+'\n'+summary).encode()).hexdigest()
        dedupe_key=dedupe_key or None
        with self._connect() as c:
            existing=None
            if dedupe_key:existing=c.execute('SELECT * FROM memories WHERE dedupe_key=?',(dedupe_key,)).fetchone()
            if not existing and not dedupe_key:
                existing=c.execute('''SELECT * FROM memories WHERE status='active' AND kind=? AND COALESCE(project,'')=COALESCE(?, '')
                                      AND lower(title)=lower(?) AND source_hash=? LIMIT 1''',(kind,project,title,source_hash or content_hash)).fetchone()
            if existing:
                # Stable source keys are mutable projections: update instead of duplicating stale knowledge.
                changed = any([
                    existing['kind']!=kind,existing['agent']!=agent,existing['title']!=title,existing['summary']!=summary,
                    existing['project']!=project,existing['tags']!=json.dumps(tags,ensure_ascii=False),
                    existing['source_type']!=source_type,existing['source_ref']!=source_ref,
                    existing['source_hash']!=(source_hash or content_hash),existing['status']!='active'])
                if changed and replace_existing:
                    before={'title':existing['title'],'summary':existing['summary'],'status':existing['status']}
                    c.execute('''UPDATE memories SET agent=?,kind=?,task=?,project=?,title=?,summary=?,tags=?,success=?,qa_pass=?,
                               run_id=?,source=?,status='active',source_type=?,source_ref=?,source_hash=?,confidence=?,importance=?,
                               confirmed_at=COALESCE(?,confirmed_at),confirmed_by=COALESCE(?,confirmed_by),updated_at=?,valid_from=?,valid_to=?,
                               metadata=?,supersedes_uid=COALESCE(?,supersedes_uid) WHERE id=?''',
                              (agent,kind,task,project,title,summary,json.dumps(tags,ensure_ascii=False),int(success),int(qa_pass),run_id,
                               source_ref,source_type,source_ref,source_hash or content_hash,confidence,importance,confirmed_at,confirmed_by,
                               now,valid_from,valid_to,json.dumps(metadata or {},ensure_ascii=False),supersedes_uid,existing['id']))
                    row=c.execute('SELECT * FROM memories WHERE id=?',(existing['id'],)).fetchone();self._index_one(c,row)
                    self._record_revision(c,row['uid'],'update',changes={'before':before,'source_ref':source_ref})
                return existing['id']
            values=(agent,kind,task,project,title,summary,json.dumps(tags,ensure_ascii=False),int(success),int(qa_pass),now,run_id,source_ref,dedupe_key,
                    None,'active',source_type,source_ref,source_hash or content_hash,confidence,importance,confirmed_at,confirmed_by,now,valid_from,valid_to,
                    supersedes_uid,json.dumps(metadata or {},ensure_ascii=False),0)
            cur=c.execute('''INSERT INTO memories(agent,kind,task,project,title,summary,tags,success,qa_pass,created_at,run_id,source,dedupe_key,
                           uid,status,source_type,source_ref,source_hash,confidence,importance,confirmed_at,confirmed_by,updated_at,valid_from,valid_to,
                           supersedes_uid,metadata,access_count)
                           VALUES('''+','.join('?' for _ in values)+')',values)
            mid=cur.lastrowid;uid=f'MEM-{mid:08d}';c.execute('UPDATE memories SET uid=? WHERE id=?',(uid,mid))
            row=c.execute('SELECT * FROM memories WHERE id=?',(mid,)).fetchone();self._index_one(c,row);self._record_revision(c,uid,'create',changes={'source_ref':source_ref})
            if supersedes_uid:self._link_locked(c,uid,supersedes_uid,'supersedes');self._link_locked(c,supersedes_uid,uid,'superseded_by')
            return mid

    # Backwards compatible API used by Galaxy <=1.8 and existing integrations.
    def save(self,agent,kind,title,summary,task=None,project=None,tags=None,success=False,
             qa_pass=False,run_id=None,source=None,dedupe_key=None):
        mapped=kind if kind in MEMORY_KINDS else 'note'
        return self.remember(mapped,title,summary,agent=agent,task=task,project=project,tags=tags,
                             success=success,qa_pass=qa_pass,run_id=run_id,source_type='agent_run' if run_id else 'legacy',
                             source_ref=source,confidence=.95 if qa_pass else (.7 if success else .5),importance=.55,
                             confirmed=bool(qa_pass),confirmed_by='deterministic_qa' if qa_pass else None,dedupe_key=dedupe_key)

    def confirm(self,uid,by='user'):
        now=self._now()
        with self._connect() as c:
            row=c.execute('SELECT * FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            if row['status']!='active':raise ValueError('Only active memory can be confirmed')
            c.execute('UPDATE memories SET confirmed_at=?,confirmed_by=?,updated_at=?,confidence=MAX(confidence,0.9) WHERE uid=?',(now,by,now,uid))
            self._record_revision(c,uid,'confirm',changes={'confirmed_by':by})
        return self.get(uid)

    def correct(self,uid,summary,*,title=None,reason='correction',confidence=None,importance=None):
        old=self.get(uid)
        if not old:raise KeyError(uid)
        if old['status']!='active':raise ValueError('Only active memory can be corrected')
        new_id=self.remember(old['kind'],title or old['title'],summary,agent='User',task=old['task'],project=old['project'],tags=old['tags'],
                             source_type='correction',source_ref=uid,confidence=old['confidence'] if confidence is None else confidence,
                             importance=old['importance'] if importance is None else importance,confirmed=True,confirmed_by='user',
                             metadata={'correction_reason':reason},supersedes_uid=uid)
        new=self.get(new_id)
        with self._connect() as c:
            c.execute("UPDATE memories SET status='superseded',updated_at=? WHERE uid=?",(self._now(),uid))
            self._record_revision(c,uid,'supersede',reason,{'replacement':new['uid']})
        return new

    def forget(self,uid,reason='user requested'):
        with self._connect() as c:
            row=c.execute('SELECT * FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            c.execute("UPDATE memories SET status='retracted',updated_at=? WHERE uid=?",(self._now(),uid))
            self._record_revision(c,uid,'retract',reason)
        return self.get(uid)

    def mark_stale(self,uid,reason='source no longer matches current project state'):
        with self._connect() as c:
            row=c.execute('SELECT * FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            if row['status'] in {'superseded','retracted'}:raise ValueError('terminal memory cannot be marked stale')
            c.execute("UPDATE memories SET status='stale',updated_at=? WHERE uid=?",(self._now(),uid))
            self._record_revision(c,uid,'mark_stale',reason)
        return self.get(uid)

    def mark_contradicted(self,uid,reason='contradicted by newer project evidence',by_uid=None):
        with self._connect() as c:
            row=c.execute('SELECT * FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            if row['status'] in {'superseded','retracted'}:raise ValueError('terminal memory cannot be marked contradicted')
            c.execute("UPDATE memories SET status='contradicted',updated_at=? WHERE uid=?",(self._now(),uid))
            self._record_revision(c,uid,'mark_contradicted',reason,{'by_uid':by_uid} if by_uid else {})
            if by_uid:
                self._link_locked(c,uid,by_uid,'contradicts')
        return self.get(uid)

    def reactivate(self,uid,reason='memory revalidated'):
        with self._connect() as c:
            row=c.execute('SELECT * FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            if row['status'] in {'superseded','retracted'}:raise ValueError('terminal memory cannot be reactivated')
            c.execute("UPDATE memories SET status='active',updated_at=? WHERE uid=?",(self._now(),uid))
            self._record_revision(c,uid,'reactivate',reason)
        return self.get(uid)

    def delete(self,uid,reason='user requested permanent deletion'):
        with self._connect() as c:
            row=c.execute('SELECT id FROM memories WHERE uid=?',(uid,)).fetchone()
            if not row:raise KeyError(uid)
            c.execute('INSERT INTO memory_audit(uid,action,changed_at,reason) VALUES(?,?,?,?)',(uid,'delete',self._now(),reason))
            c.execute('DELETE FROM memory_links WHERE from_uid=? OR to_uid=?',(uid,uid))
            c.execute('DELETE FROM memory_terms WHERE memory_id=?',(row['id'],))
            c.execute('DELETE FROM memory_revisions WHERE uid=?',(uid,))
            if self.fts:c.execute('DELETE FROM memory_fts WHERE rowid=?',(row['id'],))
            c.execute('DELETE FROM memories WHERE uid=?',(uid,))
        return {'uid':uid,'deleted':True,'reason':reason}

    def _link_locked(self,c,from_uid,to_uid,relation):
        if relation not in RELATIONS:raise ValueError(f'Unknown relation: {relation}')
        if from_uid==to_uid:raise ValueError('A memory cannot link to itself')
        found=c.execute('SELECT count(*) FROM memories WHERE uid IN (?,?)',(from_uid,to_uid)).fetchone()[0]
        if found!=2:raise KeyError('One or both memory UIDs do not exist')
        c.execute('INSERT OR IGNORE INTO memory_links VALUES(?,?,?,?)',(from_uid,to_uid,relation,self._now()))

    def link(self,from_uid,to_uid,relation='relates_to'):
        with self._connect() as c:self._link_locked(c,from_uid,to_uid,relation)
        return {'from_uid':from_uid,'to_uid':to_uid,'relation':relation}

    def links(self,uid):
        with self._connect() as c:
            rows=c.execute('''SELECT l.*,m.title,m.kind,m.status FROM memory_links l JOIN memories m ON m.uid=l.to_uid
                              WHERE l.from_uid=? ORDER BY l.created_at DESC''',(uid,)).fetchall()
        return [dict(r) for r in rows]

    def history(self,uid):
        with self._connect() as c:rows=c.execute('SELECT * FROM memory_revisions WHERE uid=? ORDER BY id',(uid,)).fetchall()
        out=[]
        for r in rows:
            d=dict(r);d['changes']=self._json(d['changes'],{});out.append(d)
        return out

    def search(self,query,agent=None,limit=6,project=None,kinds=None,status='active',include_global=False):
        words=sorted(self.words(query))[:64]
        if not words or limit<=0:return []
        with self._connect() as c:
            ids={};placeholders=','.join('?' for _ in words)
            sql=f'''SELECT m.id,SUM(t.weight) AS lexical FROM memories m JOIN memory_terms t ON t.memory_id=m.id
                    WHERE t.term IN ({placeholders})''';args=list(words)
            if status:sql+=' AND m.status=?';args.append(status)
            if agent:sql+=' AND m.agent=?';args.append(agent)
            if project is not None:
                if include_global:sql+=' AND (m.project=? OR m.project IS NULL OR m.project=\'\')'
                else:sql+=' AND m.project=?'
                args.append(project)
            if kinds:
                ks=[k for k in kinds if k in MEMORY_KINDS]
                if not ks:return []
                sql+=' AND m.kind IN ('+','.join('?' for _ in ks)+')';args.extend(ks)
            sql+=' GROUP BY m.id ORDER BY lexical DESC LIMIT 120'
            for r in c.execute(sql,args):ids[r['id']]={'lexical':float(r['lexical'] or 0),'fts_bonus':0}
            if self.fts:
                fts_query=' OR '.join(f'"{w}"*' for w in words)
                try:
                    frows=c.execute('SELECT rowid,bm25(memory_fts,5.0,1.0,2.0,0.7,0.8) rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 120',(fts_query,)).fetchall()
                    for idx,r in enumerate(frows):ids.setdefault(r['rowid'],{'lexical':0,'fts_bonus':0})['fts_bonus']=max(0.0,1.0-idx/120)
                except sqlite3.OperationalError:pass
            if not ids:return []
            rows=c.execute('SELECT * FROM memories WHERE id IN ('+','.join('?' for _ in ids)+')',tuple(ids)).fetchall()
        now=dt.datetime.now(dt.timezone.utc);query_l=query.lower().strip();ranked=[]
        for row in rows:
            if status and row['status']!=status:continue
            if agent and row['agent']!=agent:continue
            if project is not None and not (row['project']==project or (include_global and not row['project'])):continue
            if kinds and row['kind'] not in kinds:continue
            meta=ids[row['id']];score=meta['lexical']+meta['fts_bonus']*.75
            hay=(row['title']+' '+row['summary']).lower()
            if query_l and query_l in hay:score+=2.0
            score+=float(row['confidence'] or .5)*.55+float(row['importance'] or .5)*.55
            if row['qa_pass']:score+=.9
            if row['confirmed_at']:score+=.65
            try:
                changed=dt.datetime.fromisoformat(row['updated_at'] or row['created_at']);age=max(0,(now-changed).days);score+=max(0,.3*(1-age/180))
            except (ValueError,TypeError):pass
            ranked.append(self._row(row,score))
        ranked.sort(key=lambda x:(x['score'],x['id']),reverse=True)
        return ranked[:limit]

    def _anchors(self,project,limit):
        kinds=('decision','constraint','preference','procedure','fact','goal','commitment')
        sql='''SELECT * FROM memories WHERE status='active' AND kind IN (?,?,?,?,?,?,?) AND (project=? OR project IS NULL OR project='')
               ORDER BY (confirmed_at IS NOT NULL) DESC, qa_pass DESC, importance DESC, updated_at DESC LIMIT ?'''
        with self._connect() as c:rows=c.execute(sql,[*kinds,project,limit]).fetchall()
        return [self._row(r) for r in rows]

    def context(self,query,*,project=None,agent=None,limit=8):
        """Return compact working context, not just query matches.

        Relevant memories are combined with high-value project anchors. This lets a task
        inherit standing decisions/constraints even when the task wording does not repeat them.
        """
        relevant=self.search(query,agent=agent,project=project,limit=max(3,limit),include_global=True)
        relevant+=self.search(query,project=project,limit=max(3,limit),include_global=True)
        anchors=self._anchors(project,max(2,limit//2)) if project is not None else []
        chosen={}
        for reason,items in (('relevant',relevant),('project_anchor',anchors)):
            for item in items:
                if item['uid'] not in chosen:
                    item=dict(item);item['selection_reason']=reason;chosen[item['uid']]=item
                if len(chosen)>=limit:break
            if len(chosen)>=limit:break
        out=list(chosen.values())[:limit]
        if out:
            now=self._now()
            with self._connect() as c:
                c.executemany('UPDATE memories SET access_count=access_count+1,last_accessed_at=? WHERE uid=?',[(now,x['uid']) for x in out])
        # Agents only need a compact, provenance-rich projection.
        keys=('uid','kind','title','summary','tags','project','confidence','importance','verified','confirmed','source_type','source_ref','updated_at','selection_reason')
        return [{k:x.get(k) for k in keys} for x in out]

    def recent(self,agent=None,limit=10,status=None):
        sql,args='SELECT * FROM memories',[];where=[]
        if agent:where.append('agent=?');args.append(agent)
        if status:where.append('status=?');args.append(status)
        if where:sql+=' WHERE '+' AND '.join(where)
        with self._connect() as c:rows=c.execute(sql+' ORDER BY id DESC LIMIT ?',[*args,limit]).fetchall()
        return [self._row(r) for r in rows]

    def review_queue(self,project=None,limit=20,stale_days=90):
        cutoff=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=stale_days)).isoformat()
        sql="""SELECT * FROM memories WHERE status IN ('active','stale','contradicted') AND (status<>'active' OR confirmed_at IS NULL AND qa_pass=0 OR updated_at<?)""";args=[cutoff]
        if project is not None:sql+=' AND (project=? OR project IS NULL OR project=\'\')';args.append(project)
        sql+=' ORDER BY importance DESC, confidence ASC, updated_at ASC LIMIT ?';args.append(limit)
        with self._connect() as c:rows=c.execute(sql,args).fetchall()
        return [self._row(r) for r in rows]

    def brief(self,project=None,limit=6):
        def pick(kinds,n=limit):
            q='''SELECT * FROM memories WHERE status='active' AND kind IN ('''+','.join('?' for _ in kinds)+')';args=list(kinds)
            if project is not None:q+=' AND (project=? OR project IS NULL OR project=\'\')';args.append(project)
            q+=' ORDER BY (confirmed_at IS NOT NULL OR qa_pass=1) DESC, importance DESC, updated_at DESC LIMIT ?';args.append(n)
            with self._connect() as c:return [self._row(r) for r in c.execute(q,args)]
        return {
            'project':project,
            'goals_and_commitments':pick(['goal','commitment']),
            'decisions_and_constraints':pick(['decision','constraint','preference']),
            'procedures_and_lessons':pick(['procedure','lesson','solution']),
            'open_questions':pick(['question']),
            'recent_verified':self._recent_verified(project,limit),
            'needs_review':self.review_queue(project,limit),
        }

    def _recent_verified(self,project,limit):
        q="SELECT * FROM memories WHERE status='active' AND (qa_pass=1 OR confirmed_at IS NOT NULL)";args=[]
        if project is not None:q+=' AND (project=? OR project IS NULL OR project=\'\')';args.append(project)
        q+=' ORDER BY updated_at DESC LIMIT ?';args.append(limit)
        with self._connect() as c:return [self._row(r) for r in c.execute(q,args)]

    def brief_markdown(self,project=None,limit=6):
        data=self.brief(project,limit)
        lines=['# Galaxy Brain — Working Memory','',f"Project: `{project or 'all'}`  ",f"Generated: `{self._now()}`",'']
        sections=[
            ('Goals & commitments','goals_and_commitments'),
            ('Decisions & constraints','decisions_and_constraints'),
            ('Procedures & lessons','procedures_and_lessons'),
            ('Open questions','open_questions'),
            ('Recent verified / confirmed','recent_verified'),
            ('Needs review','needs_review'),
        ]
        for title,key in sections:
            lines += [f'## {title}','']
            items=data[key]
            if not items: lines += ['_None._',''];continue
            for item in items:
                trust='verified' if item.get('qa_pass') else 'confirmed' if item.get('confirmed_at') else f"confidence={item.get('confidence',.5):.2f}"
                source=item.get('source_ref') or item.get('source_type') or 'unknown'
                lines.append(f"- **[{item['uid']}] {item['title']}** — {item['kind']} · {trust} · source: `{source}`")
                compact=' '.join((item.get('summary') or '').split())
                compact=re.sub(r'\[\[([^]\|]+)\|([^]]+)\]\]',r'\2',compact)
                compact=re.sub(r'\[\[([^]]+)\]\]',r'\1',compact)
                if compact:lines.append('  '+compact[:360]+('…' if len(compact)>360 else ''))
            lines.append('')
        lines += ['---','Use `python galaxy.py brain-show MEM-...` for provenance and revisions.',
                  'Use `brain-confirm`, `brain-correct` or `brain-forget` to manage knowledge.','']
        return '\n'.join(lines)

    def export_brief(self,project=None,limit=6):
        path=self.dir/'BRAIN.md';path.write_text(self.brief_markdown(project,limit),encoding='utf-8')
        return path

    def stats(self):
        with self._connect() as c:
            total=c.execute('SELECT count(*) FROM memories').fetchone()[0]
            active=c.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0]
            verified=c.execute("SELECT count(*) FROM memories WHERE status='active' AND (qa_pass=1 OR confirmed_at IS NOT NULL)").fetchone()[0]
            return {
                'total':total,'active':active,'verified_or_confirmed':verified,
                'needs_review':c.execute("SELECT count(*) FROM memories WHERE status='active' AND qa_pass=0 AND confirmed_at IS NULL").fetchone()[0],
                'by_agent':{r[0]:r[1] for r in c.execute('SELECT agent,count(*) FROM memories GROUP BY agent')},
                'by_kind':{r[0]:r[1] for r in c.execute("SELECT kind,count(*) FROM memories WHERE status='active' GROUP BY kind")},
                'by_project':{(r[0] or '_global'):r[1] for r in c.execute("SELECT project,count(*) FROM memories WHERE status='active' GROUP BY project")},
                'by_status':{r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM memories GROUP BY status')},
                'fts5':self.fts,
            }

    @staticmethod
    def _sections(body,default_title):
        matches=list(re.finditer(r'(?m)^(#{1,6})\s+(.+?)\s*$',body))
        if not matches:
            text=body.strip();return [(default_title,text)] if text else []
        out=[]
        prefix=body[:matches[0].start()].strip()
        if prefix:out.append((default_title,prefix))
        for i,m in enumerate(matches):
            start=m.end();end=matches[i+1].start() if i+1<len(matches) else len(body)
            content=body[start:end].strip();title=m.group(2).strip()
            if content:out.append((title,content))
        return out

    @staticmethod
    def _infer_kind(heading,default_kind='note'):
        h=(heading or '').lower()
        rules=(
            ('constraint',('requirement','требован','constraint','огранич','rule','правил')),
            ('decision',('decision','решени')),
            ('preference',('preference','предпоч')),
            ('procedure',('technical specification','specification','спецификац','procedure','инструкц','how to','runbook')),
            ('lesson',('lesson','урок','ретро','retrospective')),
            ('question',('question','вопрос')),
            ('outcome',('outcome','result','итог','результат')),
            ('goal',('goal','цель','objective')),
            ('commitment',('commitment','обязательств','deadline','дедлайн')),
            ('idea',('idea','идея')),
            ('reference',('reference','resource','artifact','context','справ','ресурс','артефакт','контекст')),
        )
        for kind,needles in rules:
            if any(x in h for x in needles):return kind
        return default_kind

    def _vault_files(self):
        vault=self.root/'data'/'vault'
        if not vault.exists():return []
        return sorted(p for p in vault.rglob('*.md') if p.is_file() and not p.is_symlink())

    def ingest_vault(self):
        """Incrementally project existing Markdown notes into managed knowledge."""
        created=updated=unchanged=retracted=files_count=0;seen_by_source={}
        for path in self._vault_files():
            files_count+=1;rel=path.relative_to(self.root).as_posix();text=path.read_text(encoding='utf-8',errors='replace')
            fm,body=self._frontmatter(text);project=fm.get('project')
            if not project and path.parent != self.root/'data'/'vault':project=path.relative_to(self.root/'data'/'vault').parts[0]
            kind=fm.get('memory_type') or fm.get('type') or 'note';kind=kind if kind in MEMORY_KINDS else 'note';tags=self._tags(fm.get('tags'))
            agent=fm.get('agent') or 'Vault';source_type='markdown';source_ref=rel;sections=self._sections(body,path.stem);seen=set()
            for idx,(heading,content) in enumerate(sections):
                # Keep useful chunks while avoiding empty formatting-only headings.
                plain=re.sub(r'\s+',' ',content).strip()
                if len(plain)<8:continue
                section_kind=self._infer_kind(heading,kind)
                key=f'note:{rel}#{idx}:{heading.lower()}';seen.add(key);source_hash=hashlib.sha256((heading+'\n'+content).encode()).hexdigest()
                with self._connect() as c:old=c.execute('SELECT id,source_hash FROM memories WHERE dedupe_key=?',(key,)).fetchone()
                mid=self.remember(section_kind,heading,content,agent=agent,project=project,tags=tags,source_type=source_type,source_ref=source_ref,
                                  source_hash=source_hash,confidence=.6,importance=.45,dedupe_key=key,metadata={'frontmatter':fm},replace_existing=True)
                if old is None:created+=1
                elif old['source_hash']!=source_hash:updated+=1
                else:unchanged+=1
            seen_by_source[source_ref]=seen
        # Retract sections removed from a Markdown source so stale text stops influencing agents.
        with self._connect() as c:
            for source_ref,seen in seen_by_source.items():
                rows=c.execute("SELECT uid,dedupe_key FROM memories WHERE source_type='markdown' AND source_ref=? AND status='active'",(source_ref,)).fetchall()
                for row in rows:
                    if row['dedupe_key'] not in seen:
                        c.execute("UPDATE memories SET status='retracted',updated_at=? WHERE uid=?",(self._now(),row['uid']))
                        self._record_revision(c,row['uid'],'retract','source section removed');retracted+=1
        return {'files':files_count,'created':created,'updated':updated,'unchanged':unchanged,'retracted':retracted}
