"""Semantic memory primitives for Galaxy 2.0.

The default embedder is dependency-free and deterministic. It creates normalized dense
vectors from Unicode word/character features so Galaxy always has vector search even on
an offline machine. A locally installed SentenceTransformer model can be selected with
GALAXY_EMBEDDING_BACKEND=sentence-transformers and GALAXY_EMBEDDING_MODEL=/local/path.
No network download is attempted by Galaxy.
"""
from __future__ import annotations
from array import array
from contextlib import contextmanager
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from difflib import SequenceMatcher


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class PortableEmbedder:
    """Small offline embedding backend based on signed feature hashing.

    This is intentionally not presented as a neural model. It gives Galaxy a portable
    vector index, typo/morphology tolerance through character n-grams and a stable API
    that can be upgraded to a local neural embedding model without changing storage.
    """
    model = "galaxy-portable-embedding-v1"

    def __init__(self, dims=384):
        self.dims = int(dims)
        if self.dims < 64:
            raise ValueError("embedding dimensions must be >= 64")

    @staticmethod
    def _tokens(text):
        return re.findall(r"[^\W_]{2,}", (text or "").casefold(), flags=re.UNICODE)

    @staticmethod
    def _stemish(token):
        # Character features carry most morphology tolerance; this light suffix trimming
        # helps Russian/English variants without pretending to be a language stemmer.
        if len(token) <= 5:
            return token
        endings = (
            "иями","ями","ами","ого","ему","ыми","ими","tion","ing","ers","ies",
            "ами","ями","ой","ый","ий","ая","ое","ые","ов","ев","ах","ях",
            "ed","er","es","s",
        )
        for end in endings:
            if token.endswith(end) and len(token) - len(end) >= 3:
                return token[:-len(end)]
        return token

    @staticmethod
    def _slot(feature, dims):
        raw = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(raw, "little")
        return value % dims, -1.0 if (value >> 63) else 1.0

    def encode(self, text):
        vec = [0.0] * self.dims
        tokens = self._tokens(text)
        features = []
        for token in tokens:
            stem = self._stemish(token)
            features.append(("w:" + token, 1.0))
            if stem != token:
                features.append(("s:" + stem, 0.8))
            padded = "^" + token + "$"
            for n, weight in ((3, 0.22), (4, 0.18)):
                if len(padded) >= n:
                    for i in range(len(padded)-n+1):
                        features.append((f"c{n}:" + padded[i:i+n], weight))
        for a, b in zip(tokens, tokens[1:]):
            features.append(("b:" + a + "_" + b, 0.65))
        for feature, weight in features:
            idx, sign = self._slot(feature, self.dims)
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v*v for v in vec)) or 1.0
        return [v / norm for v in vec]


class LocalSentenceTransformerEmbedder:
    """Optional neural backend using an already-local SentenceTransformer model."""
    def __init__(self, model_path):
        path = Path(model_path or "")
        if not path.exists():
            raise ValueError("GALAXY_EMBEDDING_MODEL must point to an existing local model")
        from sentence_transformers import SentenceTransformer  # optional dependency
        self._model = SentenceTransformer(str(path))
        sample = self._model.encode(["galaxy"], normalize_embeddings=True)[0]
        self.dims = len(sample)
        self.model = "sentence-transformers:" + path.name

    def encode(self, text):
        return [float(x) for x in self._model.encode([text or ""], normalize_embeddings=True)[0]]


def build_embedder():
    backend = os.getenv("GALAXY_EMBEDDING_BACKEND", "portable").strip().lower()
    if backend in {"portable", "hash", "local"}:
        return PortableEmbedder(int(os.getenv("GALAXY_EMBEDDING_DIMS", "384")))
    if backend in {"sentence-transformers", "sentence_transformers", "st"}:
        return LocalSentenceTransformerEmbedder(os.getenv("GALAXY_EMBEDDING_MODEL"))
    raise ValueError(f"Unknown embedding backend: {backend}")


def pack_vector(values):
    return array("f", values).tobytes()


def unpack_vector(blob):
    values = array("f")
    values.frombytes(blob or b"")
    return list(values)


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    # Stored/query vectors are normalized, but keep this safe for migrated data.
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    return dot / (na*nb) if na and nb else 0.0


class SemanticIndex:
    def __init__(self, db_path, embedder=None):
        self.db = Path(db_path)
        self.embedder = embedder or build_embedder()
        self._init_schema()

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback(); raise
        finally:
            c.close()

    def _init_schema(self):
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS memory_embeddings(
                memory_uid TEXT PRIMARY KEY, model TEXT NOT NULL, dims INTEGER NOT NULL,
                vector BLOB NOT NULL, source_hash TEXT, updated_at TEXT NOT NULL)''')
            c.execute("CREATE INDEX IF NOT EXISTS idx_embeddings_model ON memory_embeddings(model)")

    def _text(self, row):
        tags = row.get("tags") if isinstance(row, dict) else row["tags"]
        try:
            if isinstance(tags, str): tags = json.loads(tags or "[]")
        except json.JSONDecodeError:
            tags = []
        parts = [row["title"], row["summary"], " ".join(tags or []), row["project"] or "", row["task"] or ""]
        return "\n".join(str(x) for x in parts if x)

    def index_row(self, row):
        data = dict(row)
        text = self._text(data)
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._connect() as c:
            existing = c.execute("SELECT model,source_hash FROM memory_embeddings WHERE memory_uid=?", (data["uid"],)).fetchone()
            if existing and existing["model"] == self.embedder.model and existing["source_hash"] == source_hash:
                return False
            vector = self.embedder.encode(text)
            c.execute('''INSERT OR REPLACE INTO memory_embeddings(memory_uid,model,dims,vector,source_hash,updated_at)
                         VALUES(?,?,?,?,?,?)''', (data["uid"], self.embedder.model, len(vector), pack_vector(vector), source_hash, _now()))
        return True

    def sync(self, status="active"):
        with self._connect() as c:
            rows = c.execute("SELECT * FROM memories WHERE status=?", (status,)).fetchall()
            active = {r["uid"] for r in rows}
            c.execute("DELETE FROM memory_embeddings WHERE memory_uid NOT IN (SELECT uid FROM memories WHERE status='active')")
        changed = 0
        for row in rows:
            changed += int(self.index_row(row))
        return {"model": self.embedder.model, "dims": self.embedder.dims, "indexed": len(active), "changed": changed}

    def delete(self, uid):
        with self._connect() as c:
            c.execute("DELETE FROM memory_embeddings WHERE memory_uid=?", (uid,))

    def search(self, query, *, project=None, agent=None, kinds=None, include_global=False, limit=20, min_similarity=0.08):
        if not (query or "").strip() or limit <= 0:
            return []
        qvec = self.embedder.encode(query)
        sql = '''SELECT m.*,e.model,e.dims,e.vector FROM memories m
                 JOIN memory_embeddings e ON e.memory_uid=m.uid WHERE m.status='active' AND e.model=?'''
        args = [self.embedder.model]
        if project is not None:
            sql += " AND (m.project=? OR m.project IS NULL OR m.project='')" if include_global else " AND m.project=?"
            args.append(project)
        if agent:
            sql += " AND m.agent=?"; args.append(agent)
        if kinds:
            kinds = list(kinds)
            sql += " AND m.kind IN (" + ",".join("?" for _ in kinds) + ")"; args.extend(kinds)
        with self._connect() as c:
            rows = c.execute(sql, args).fetchall()
        scored = []
        for row in rows:
            sim = cosine(qvec, unpack_vector(row["vector"]))
            if sim >= min_similarity:
                scored.append({"uid": row["uid"], "similarity": round(float(sim), 6)})
        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:limit]

    def vector(self, uid):
        with self._connect() as c:
            row = c.execute("SELECT * FROM memory_embeddings WHERE memory_uid=? AND model=?", (uid, self.embedder.model)).fetchone()
        return unpack_vector(row["vector"]) if row else None

    def stats(self):
        with self._connect() as c:
            count = c.execute("SELECT count(*) FROM memory_embeddings WHERE model=?", (self.embedder.model,)).fetchone()[0]
        return {"model": self.embedder.model, "dims": self.embedder.dims, "vectors": count}


class EntityGraph:
    """A compact entity graph grounded in memory provenance."""
    ENTITY_RELATIONS = {"related_to", "depends_on", "part_of", "uses", "owned_by", "conflicts_with"}

    def __init__(self, db_path):
        self.db = Path(db_path)
        self._init_schema()

    @contextmanager
    def _connect(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c; c.commit()
        except BaseException:
            c.rollback(); raise
        finally:
            c.close()

    def _init_schema(self):
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS entities(
                uid TEXT PRIMARY KEY, canonical TEXT NOT NULL, entity_type TEXT NOT NULL,
                display_name TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(canonical,entity_type))''')
            c.execute('''CREATE TABLE IF NOT EXISTS entity_aliases(
                entity_uid TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY(entity_uid,alias))''')
            c.execute('''CREATE TABLE IF NOT EXISTS memory_entities(
                memory_uid TEXT NOT NULL, entity_uid TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'auto', PRIMARY KEY(memory_uid,entity_uid))''')
            c.execute('''CREATE TABLE IF NOT EXISTS entity_relations(
                from_uid TEXT NOT NULL, to_uid TEXT NOT NULL, relation TEXT NOT NULL,
                weight REAL NOT NULL DEFAULT 1, evidence TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(from_uid,to_uid,relation))''')
            c.execute("CREATE INDEX IF NOT EXISTS idx_entity_canonical ON entities(canonical,entity_type)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_memory_entity ON memory_entities(entity_uid,memory_uid)")

    @staticmethod
    def _canon(value):
        value = re.sub(r"\s+", " ", (value or "").strip().casefold())
        return value[:240]

    @staticmethod
    def _uid(entity_type, canonical):
        return "ENT-" + hashlib.sha1((entity_type + "\0" + canonical).encode()).hexdigest()[:12].upper()

    def ensure(self, name, entity_type="concept", aliases=None):
        canonical = self._canon(name)
        if not canonical:
            raise ValueError("entity name required")
        uid = self._uid(entity_type, canonical)
        with self._connect() as c:
            c.execute("INSERT OR IGNORE INTO entities(uid,canonical,entity_type,display_name,created_at) VALUES(?,?,?,?,?)",
                      (uid, canonical, entity_type, str(name).strip()[:240], _now()))
            for alias in aliases or []:
                a = self._canon(alias)
                if a: c.execute("INSERT OR IGNORE INTO entity_aliases(entity_uid,alias) VALUES(?,?)", (uid, a))
        return uid

    def link_memory(self, memory_uid, entity_uid, confidence=1.0, source="manual"):
        with self._connect() as c:
            if not c.execute("SELECT 1 FROM entities WHERE uid=?", (entity_uid,)).fetchone():
                raise KeyError(entity_uid)
            if not c.execute("SELECT 1 FROM memories WHERE uid=?", (memory_uid,)).fetchone():
                raise KeyError(memory_uid)
            c.execute("INSERT OR REPLACE INTO memory_entities(memory_uid,entity_uid,confidence,source) VALUES(?,?,?,?)",
                      (memory_uid, entity_uid, max(0.0, min(1.0, float(confidence))), source))
        return {"memory_uid": memory_uid, "entity_uid": entity_uid}

    def relate(self, from_uid, to_uid, relation="related_to", weight=1.0, evidence=None):
        if relation not in self.ENTITY_RELATIONS:
            raise ValueError(f"unknown entity relation: {relation}")
        if from_uid == to_uid:
            raise ValueError("entity cannot relate to itself")
        with self._connect() as c:
            found = c.execute("SELECT count(*) FROM entities WHERE uid IN (?,?)", (from_uid, to_uid)).fetchone()[0]
            if found != 2: raise KeyError("one or both entities do not exist")
            c.execute("INSERT OR REPLACE INTO entity_relations VALUES(?,?,?,?,?,?)",
                      (from_uid, to_uid, relation, float(weight), evidence, _now()))
        return {"from_uid": from_uid, "to_uid": to_uid, "relation": relation}

    def auto_link(self, memory):
        m = dict(memory)
        specs = []
        if m.get("project"): specs.append((m["project"], "project", 1.0))
        if m.get("task"): specs.append((m["task"], "task", 1.0))
        if m.get("agent"): specs.append((m["agent"], "agent", 0.9))
        try:
            tags = m.get("tags")
            if isinstance(tags, str): tags = json.loads(tags or "[]")
        except json.JSONDecodeError:
            tags = []
        for tag in tags or []: specs.append((tag, "tag", 0.85))
        if m.get("title"): specs.append((m["title"], "topic", 0.8))
        body = (m.get("title") or "") + "\n" + (m.get("summary") or "")
        for target in re.findall(r"\[\[([^\]|#]+)", body): specs.append((target, "note", 0.9))
        file_re = r"(?<![\w/.-])([\w.@+-]+(?:/[\w.@+-]+)*\.(?:py|js|ts|json|md|yml|yaml|toml|java|kt|cpp|c|h|css|html))\b"
        for target in re.findall(file_re, body, flags=re.I): specs.append((target, "file", 0.8))
        linked = []
        typed = {}
        seen = set()
        for name, kind, conf in specs:
            key = (self._canon(name), kind)
            if not key[0] or key in seen: continue
            seen.add(key)
            ent = self.ensure(name, kind)
            self.link_memory(m["uid"], ent, conf, "auto")
            linked.append(ent); typed.setdefault(kind,[]).append(ent)
        # Build conservative, provenance-backed structural edges. These are not model
        # guesses: they encode relationships implied by the memory record itself.
        project = (typed.get('project') or [None])[0]
        topic = (typed.get('topic') or [None])[0]
        task = (typed.get('task') or [None])[0]
        if project:
            if task: self.relate(task,project,'part_of',1.0,m['uid'])
            if topic: self.relate(topic,project,'part_of',.85,m['uid'])
            for tag in typed.get('tag',[]): self.relate(tag,project,'part_of',.55,m['uid'])
            for note in typed.get('note',[]): self.relate(note,project,'part_of',.65,m['uid'])
        if topic:
            for file_uid in typed.get('file',[]): self.relate(topic,file_uid,'uses',.8,m['uid'])
            for tag in typed.get('tag',[]): self.relate(topic,tag,'related_to',.6,m['uid'])
            for agent_uid in typed.get('agent',[]): self.relate(agent_uid,topic,'related_to',.45,m['uid'])
        return linked

    def sync(self):
        with self._connect() as c:
            rows = c.execute("SELECT * FROM memories WHERE status='active'").fetchall()
            active = {r["uid"] for r in rows}
            c.execute("DELETE FROM memory_entities WHERE memory_uid NOT IN (SELECT uid FROM memories WHERE status='active')")
        for row in rows: self.auto_link(row)
        return {"memories": len(active), "entities": self.stats()["entities"]}

    def entity(self, uid):
        with self._connect() as c:
            row = c.execute("SELECT * FROM entities WHERE uid=?", (uid,)).fetchone()
            if not row: return None
            memories = [dict(r) for r in c.execute('''SELECT m.uid,m.kind,m.title,m.project,me.confidence,me.source
                FROM memory_entities me JOIN memories m ON m.uid=me.memory_uid
                WHERE me.entity_uid=? AND m.status='active' ORDER BY me.confidence DESC,m.importance DESC LIMIT 50''', (uid,))]
            outgoing = [dict(r) for r in c.execute("SELECT * FROM entity_relations WHERE from_uid=?", (uid,))]
        out = dict(row); out["memories"] = memories; out["relations"] = outgoing
        return out

    def list(self, project=None, limit=50):
        sql = '''SELECT e.*,count(DISTINCT me.memory_uid) AS memory_count
                 FROM entities e LEFT JOIN memory_entities me ON me.entity_uid=e.uid
                 LEFT JOIN memories m ON m.uid=me.memory_uid AND m.status='active' '''
        args=[]
        if project is not None:
            sql += "WHERE m.project=? "; args.append(project)
        sql += "GROUP BY e.uid ORDER BY memory_count DESC,e.display_name LIMIT ?"; args.append(limit)
        with self._connect() as c: rows=c.execute(sql,args).fetchall()
        return [dict(r) for r in rows]

    def expand_memory_uids(self, seeds, limit=20):
        seeds = [s for s in seeds if s]
        if not seeds: return []
        ph = ",".join("?" for _ in seeds)
        with self._connect() as c:
            rows = c.execute(f'''SELECT DISTINCT me2.memory_uid, SUM(me1.confidence*me2.confidence) AS strength
                FROM memory_entities me1 JOIN memory_entities me2 ON me1.entity_uid=me2.entity_uid
                JOIN memories m ON m.uid=me2.memory_uid AND m.status='active'
                WHERE me1.memory_uid IN ({ph}) AND me2.memory_uid NOT IN ({ph})
                GROUP BY me2.memory_uid ORDER BY strength DESC LIMIT ?''', [*seeds,*seeds,limit]).fetchall()
        return [{"uid":r["memory_uid"],"strength":float(r["strength"] or 0)} for r in rows]

    def mermaid(self, project=None, limit=24):
        entities = self.list(project, limit)
        keep = {e["uid"]:e for e in entities}
        with self._connect() as c:
            rels = [dict(r) for r in c.execute("SELECT * FROM entity_relations ORDER BY weight DESC") if r["from_uid"] in keep and r["to_uid"] in keep]
            # Co-occurrence gives useful graph edges even before explicit relations exist.
            co = c.execute('''SELECT a.entity_uid x,b.entity_uid y,count(*) n FROM memory_entities a
                JOIN memory_entities b ON a.memory_uid=b.memory_uid AND a.entity_uid<b.entity_uid
                JOIN memories m ON m.uid=a.memory_uid AND m.status='active'
                GROUP BY a.entity_uid,b.entity_uid HAVING n>=2 ORDER BY n DESC LIMIT 80''').fetchall()
        lines=["graph LR"]
        def safe(s): return re.sub(r"[^A-Za-z0-9_]", "_", s)
        for uid,e in keep.items():
            label=e["display_name"].replace('"',"'")[:50]
            lines.append(f'  {safe(uid)}["{label}"]')
        seen=set()
        for r in rels:
            seen.add((r["from_uid"],r["to_uid"]))
            lines.append(f'  {safe(r["from_uid"])} -- {r["relation"]} --> {safe(r["to_uid"])}')
        for r in co:
            if r["x"] in keep and r["y"] in keep and (r["x"],r["y"]) not in seen:
                lines.append(f'  {safe(r["x"])} ---|{r["n"]}| {safe(r["y"])}')
        return "\n".join(lines) + "\n"

    def stats(self):
        with self._connect() as c:
            return {
                "entities": c.execute("SELECT count(*) FROM entities").fetchone()[0],
                "memory_links": c.execute("SELECT count(*) FROM memory_entities").fetchone()[0],
                "relations": c.execute("SELECT count(*) FROM entity_relations").fetchone()[0],
            }


class GoalStore:
    STATUSES = {"active", "paused", "done", "cancelled"}
    HORIZONS = {"long", "quarter", "month", "week", "open"}

    def __init__(self, db_path):
        self.db=Path(db_path); self._init_schema()

    @contextmanager
    def _connect(self):
        c=sqlite3.connect(self.db,timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA journal_mode=WAL")
        try: yield c; c.commit()
        except BaseException: c.rollback(); raise
        finally: c.close()

    def _init_schema(self):
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS goals(
                id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT UNIQUE, project TEXT,
                title TEXT NOT NULL, description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                priority REAL NOT NULL DEFAULT 0.7, horizon TEXT NOT NULL DEFAULT 'long',
                success_criteria TEXT, review_at TEXT, source TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            c.execute('''CREATE TABLE IF NOT EXISTS goal_memory_links(
                goal_uid TEXT NOT NULL, memory_uid TEXT NOT NULL, relation TEXT NOT NULL DEFAULT 'supports',
                created_at TEXT NOT NULL, PRIMARY KEY(goal_uid,memory_uid,relation))''')
            c.execute("CREATE INDEX IF NOT EXISTS idx_goal_project ON goals(project,status,priority)")

    def add(self,title,description,*,project=None,priority=.7,horizon="long",success_criteria=None,review_at=None,source="user"):
        if horizon not in self.HORIZONS: raise ValueError("invalid horizon")
        now=_now(); priority=max(0,min(1,float(priority)))
        with self._connect() as c:
            cur=c.execute('''INSERT INTO goals(project,title,description,status,priority,horizon,success_criteria,review_at,source,created_at,updated_at)
                             VALUES(?,?,?,'active',?,?,?,?,?,?,?)''',(project,title.strip(),description.strip(),priority,horizon,success_criteria,review_at,source,now,now))
            uid=f"GOAL-{cur.lastrowid:08d}"; c.execute("UPDATE goals SET uid=? WHERE id=?",(uid,cur.lastrowid))
        return self.get(uid)

    def get(self,uid):
        with self._connect() as c: row=c.execute("SELECT * FROM goals WHERE uid=?",(uid,)).fetchone()
        return dict(row) if row else None

    def list(self,project=None,status="active",limit=50):
        sql="SELECT * FROM goals WHERE 1=1"; args=[]
        if project is not None: sql+=" AND (project=? OR project IS NULL OR project='')"; args.append(project)
        if status: sql+=" AND status=?"; args.append(status)
        sql+=" ORDER BY priority DESC,updated_at DESC LIMIT ?"; args.append(limit)
        with self._connect() as c: rows=c.execute(sql,args).fetchall()
        return [dict(r) for r in rows]

    def update(self,uid,**changes):
        allowed={"title","description","status","priority","horizon","success_criteria","review_at"}
        clean={k:v for k,v in changes.items() if k in allowed and v is not None}
        if "status" in clean and clean["status"] not in self.STATUSES: raise ValueError("invalid goal status")
        if "horizon" in clean and clean["horizon"] not in self.HORIZONS: raise ValueError("invalid goal horizon")
        if "priority" in clean: clean["priority"]=max(0,min(1,float(clean["priority"])))
        if not self.get(uid): raise KeyError(uid)
        clean["updated_at"]=_now()
        with self._connect() as c:
            c.execute("UPDATE goals SET "+",".join(f"{k}=?" for k in clean)+" WHERE uid=?",[*clean.values(),uid])
        return self.get(uid)

    def link_memory(self,goal_uid,memory_uid,relation="supports"):
        if not self.get(goal_uid): raise KeyError(goal_uid)
        with self._connect() as c:
            if not c.execute("SELECT 1 FROM memories WHERE uid=?",(memory_uid,)).fetchone(): raise KeyError(memory_uid)
            c.execute("INSERT OR IGNORE INTO goal_memory_links VALUES(?,?,?,?)",(goal_uid,memory_uid,relation,_now()))
        return {"goal_uid":goal_uid,"memory_uid":memory_uid,"relation":relation}

    def context(self,project=None,limit=3):
        out=[]
        for g in self.list(project,"active",limit):
            summary=g["description"]
            if g.get("success_criteria"): summary += "\nSuccess criteria: "+g["success_criteria"]
            out.append({"uid":g["uid"],"kind":"goal","title":g["title"],"summary":summary,
                        "tags":["long-term-goal",g["horizon"]],"project":g["project"],"confidence":1.0,
                        "importance":g["priority"],"verified":False,"confirmed":True,"source_type":"goal",
                        "source_ref":g.get("source"),"updated_at":g["updated_at"],"selection_reason":"long_term_goal"})
        return out

    def stats(self):
        with self._connect() as c:
            rows=c.execute("SELECT status,count(*) n FROM goals GROUP BY status").fetchall()
        return {r["status"]:r["n"] for r in rows}


class Consolidator:
    """Detect duplicates/conflicts automatically; resolution remains human-controlled."""
    def __init__(self, db_path, semantic: SemanticIndex):
        self.db=Path(db_path); self.semantic=semantic; self._init_schema()

    @contextmanager
    def _connect(self):
        c=sqlite3.connect(self.db,timeout=10); c.row_factory=sqlite3.Row; c.execute("PRAGMA journal_mode=WAL")
        try: yield c; c.commit()
        except BaseException: c.rollback(); raise
        finally: c.close()

    def _init_schema(self):
        with self._connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS consolidation_candidates(
                id INTEGER PRIMARY KEY AUTOINCREMENT, left_uid TEXT NOT NULL, right_uid TEXT NOT NULL,
                candidate_type TEXT NOT NULL, score REAL NOT NULL, reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(left_uid,right_uid,candidate_type))''')
            c.execute("CREATE INDEX IF NOT EXISTS idx_consolidation_status ON consolidation_candidates(status,candidate_type,score)")

    @staticmethod
    def _norm(text): return re.sub(r"\W+"," ",(text or "").casefold()).strip()

    @staticmethod
    def _negation(text):
        t=" "+Consolidator._norm(text)+" "
        markers=(" not "," never "," no "," disable"," forbidden"," deny"," cannot "," must not ",
                 " не "," нельзя "," никогда "," запрещ"," отключ"," без ")
        return any(x in t for x in markers)

    @staticmethod
    def _opposite(text_a,text_b):
        a=Consolidator._norm(text_a); b=Consolidator._norm(text_b)
        pairs=(("enabled","disabled"),("allow","deny"),("allowed","forbidden"),("yes","no"),
               ("можно","нельзя"),("включен","выключен"),("sqlite","supabase"))
        return any((x in a and y in b) or (y in a and x in b) for x,y in pairs)

    def _classify(self,a,b,sim):
        title_a=self._norm(a["title"]); title_b=self._norm(b["title"])
        title_sim=SequenceMatcher(None,title_a,title_b).ratio()
        sa,sb=self._norm(a["summary"]),self._norm(b["summary"])

        # Markdown vaults often repeat structural headings such as
        # "Requirements (Immutable)" in every task.  The heading alone is
        # not evidence that two facts are about the same thing.
        generic_prefixes=(
            "requirements", "technical specification", "context artifacts",
            "execution log", "context", "artifacts", "overview",
            "active context slice", "recovered media record",
        )
        generic_a=any(title_a.startswith(x) for x in generic_prefixes)
        generic_b=any(title_b.startswith(x) for x in generic_prefixes)

        def scope(row):
            if row["task"]:
                return f"task:{row['task']}"
            ref=(row["source_ref"] or "").split("#",1)[0].strip().lower()
            if ref:
                return f"file:{ref}"
            return None

        scope_a,scope_b=scope(a),scope(b)
        different_scope=bool(scope_a and scope_b and scope_a != scope_b)

        if sa == sb and sa:
            # Identical template/reference sections are document structure,
            # not duplicated knowledge.  Preserve exact duplicates for
            # knowledge-bearing entries such as constraints and decisions.
            structural_kinds={"reference","note","procedure"}
            if different_scope and generic_a and generic_b and (a["kind"] in structural_kinds or b["kind"] in structural_kinds):
                return None,None,None
            return "duplicate",1.0,"same normalized summary"

        # Repeated structural/template sections are provenance, not knowledge
        # consolidation candidates.  Their vectors are often almost identical
        # across files simply because the scaffold text is repeated.
        structural_kinds={"reference","note","procedure"}
        if different_scope and generic_a and generic_b and (a["kind"] in structural_kinds or b["kind"] in structural_kinds):
            return None,None,None

        # Repeated knowledge-bearing headings from different tasks need strong
        # semantic evidence before they are even considered related.
        if different_scope and generic_a and generic_b and sim < .78:
            return None,None,None

        if sim >= .90 and title_sim >= .55:
            return "duplicate",sim,"high vector similarity and matching topic"

        conflict_kinds={"fact","decision","constraint","preference","procedure","goal","commitment"}
        if a["kind"] in conflict_kinds and b["kind"] in conflict_kinds and title_sim >= .55:
            conflict_floor=.65 if different_scope else .35
            if sim >= conflict_floor and (self._negation(sa) != self._negation(sb) or self._opposite(sa,sb)):
                return "conflict",sim,"same topic contains opposing/negated claims"
            if title_sim >= .88 and sim >= (.72 if different_scope else .55) and sa != sb:
                return "possible_conflict",sim,"same named topic has materially different active statements"

        if sim >= (.82 if different_scope else .76) and title_sim >= .4:
            return "related",sim,"similar active knowledge may be consolidatable"
        return None,None,None

    def scan_for(self,uid,limit=80):
        with self._connect() as c:
            a=c.execute("SELECT * FROM memories WHERE uid=? AND status='active'",(uid,)).fetchone()
            if not a:return []
            rows=c.execute('''SELECT * FROM memories WHERE status='active' AND uid<>? AND
                (COALESCE(project,'')=COALESCE(?, '') OR project IS NULL OR project='') ORDER BY importance DESC,updated_at DESC LIMIT ?''',
                (uid,a["project"],limit)).fetchall()
        va=self.semantic.vector(uid)
        if not va:return []
        found=[]
        for b in rows:
            vb=self.semantic.vector(b["uid"])
            if not vb:continue
            sim=cosine(va,vb)
            typ,score,reason=self._classify(a,b,sim)
            if not typ or typ=="related":continue
            left,right=sorted((uid,b["uid"]))
            with self._connect() as c:
                c.execute('''INSERT INTO consolidation_candidates(left_uid,right_uid,candidate_type,score,reason,status,created_at,updated_at)
                             VALUES(?,?,?,?,?,'open',?,?) ON CONFLICT(left_uid,right_uid,candidate_type)
                             DO UPDATE SET score=excluded.score,reason=excluded.reason,updated_at=excluded.updated_at,
                             status=CASE WHEN consolidation_candidates.status='dismissed' THEN 'dismissed' ELSE 'open' END''',
                          (left,right,typ,float(score),reason,_now(),_now()))
                cid=c.execute("SELECT id FROM consolidation_candidates WHERE left_uid=? AND right_uid=? AND candidate_type=?",(left,right,typ)).fetchone()[0]
            found.append({"id":cid,"left_uid":left,"right_uid":right,"candidate_type":typ,"score":round(float(score),4),"reason":reason})
        return found

    def scan(self,project=None,limit=250):
        self.semantic.sync()

        # A full scan is a recomputation of the inbox.  Keep explicit human
        # decisions (dismissed/resolved), but drop stale *open* candidates so
        # improved heuristics do not leave old false positives behind.
        with self._connect() as c:
            if project is None:
                c.execute("DELETE FROM consolidation_candidates WHERE status='open'")
            else:
                c.execute(
                    "DELETE FROM consolidation_candidates WHERE status='open' AND id IN ("
                    "SELECT cc.id FROM consolidation_candidates cc "
                    "JOIN memories a ON a.uid=cc.left_uid "
                    "JOIN memories b ON b.uid=cc.right_uid "
                    "WHERE a.project=? OR b.project=?"
                    ")",
                    (project,project),
                )

        sql="SELECT uid FROM memories WHERE status='active'";args=[]
        if project is not None:
            sql+=" AND (project=? OR project IS NULL OR project='')";args.append(project)
        sql+=" ORDER BY importance DESC,updated_at DESC LIMIT ?";args.append(limit)
        with self._connect() as c:
            uids=[r[0] for r in c.execute(sql,args)]
        for uid in uids:
            self.scan_for(uid,min(80,limit))
        return self.list(project=project,status="open",limit=100)

    def list(self,project=None,status="open",limit=100):
        sql='''SELECT cc.*,a.title left_title,b.title right_title,a.project left_project,b.project right_project
               FROM consolidation_candidates cc JOIN memories a ON a.uid=cc.left_uid JOIN memories b ON b.uid=cc.right_uid
               WHERE 1=1''';args=[]
        if status:sql+=" AND cc.status=?";args.append(status)
        if project is not None:sql+=" AND (a.project=? OR b.project=?)";args.extend([project,project])
        sql+=" ORDER BY CASE cc.candidate_type WHEN 'conflict' THEN 0 WHEN 'possible_conflict' THEN 1 ELSE 2 END,cc.score DESC LIMIT ?";args.append(limit)
        with self._connect() as c:rows=c.execute(sql,args).fetchall()
        return [dict(r) for r in rows]

    def dismiss(self,candidate_id):
        with self._connect() as c:
            if not c.execute("SELECT 1 FROM consolidation_candidates WHERE id=?",(candidate_id,)).fetchone():raise KeyError(candidate_id)
            c.execute("UPDATE consolidation_candidates SET status='dismissed',updated_at=? WHERE id=?",(_now(),candidate_id))
        return {"id":candidate_id,"status":"dismissed"}

    def mark_resolved(self,candidate_id):
        with self._connect() as c:
            c.execute("UPDATE consolidation_candidates SET status='resolved',updated_at=? WHERE id=?",(_now(),candidate_id))
        return {"id":candidate_id,"status":"resolved"}

    def stats(self):
        with self._connect() as c:
            return {r["candidate_type"]:r["n"] for r in c.execute("SELECT candidate_type,count(*) n FROM consolidation_candidates WHERE status='open' GROUP BY candidate_type")}
