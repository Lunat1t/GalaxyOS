"""Galaxy second-brain facade: hybrid retrieval, entity graph, goals and consolidation."""
from __future__ import annotations
import datetime as dt
import json
import sqlite3
from .memory import MemoryStore, MEMORY_KINDS
from .semantic import SemanticIndex, EntityGraph, GoalStore, Consolidator


class BrainStore(MemoryStore):
    """MemoryStore-compatible cognitive layer used by the orchestrator and CLI."""
    def __init__(self, root, *, sync_on_init=True):
        super().__init__(root)
        self.semantic = SemanticIndex(self.db)
        self.graph = EntityGraph(self.db)
        self.goals = GoalStore(self.db)
        self.consolidator = Consolidator(self.db, self.semantic)
        if sync_on_init:
            self.semantic.sync()
            self.graph.sync()

    def _row_by_uid(self, uid):
        with self._connect() as c:
            return c.execute("SELECT * FROM memories WHERE uid=?", (uid,)).fetchone()

    def remember(self, *args, **kwargs):
        mid = super().remember(*args, **kwargs)
        item = self.get(mid)
        if item and item.get("status") == "active":
            row = self._row_by_uid(item["uid"])
            if row:
                self.semantic.index_row(row)
                self.graph.auto_link(row)
                self.consolidator.scan_for(item["uid"])
        return mid

    def correct(self, *args, **kwargs):
        item = super().correct(*args, **kwargs)
        self.semantic.sync(); self.graph.sync()
        if item: self.consolidator.scan_for(item["uid"])
        return item

    def forget(self, *args, **kwargs):
        item = super().forget(*args, **kwargs)
        self.semantic.sync(); self.graph.sync()
        return item

    def mark_stale(self, *args, **kwargs):
        item = super().mark_stale(*args, **kwargs)
        self.semantic.sync(); self.graph.sync()
        return item

    def mark_contradicted(self, *args, **kwargs):
        item = super().mark_contradicted(*args, **kwargs)
        self.semantic.sync(); self.graph.sync()
        return item

    def reactivate(self, *args, **kwargs):
        item = super().reactivate(*args, **kwargs)
        self.semantic.sync(); self.graph.sync()
        return item

    def delete(self, uid, *args, **kwargs):
        out = super().delete(uid, *args, **kwargs)
        self.semantic.delete(uid); self.graph.sync()
        return out

    def ingest_vault(self):
        result = super().ingest_vault()
        result["semantic"] = self.semantic.sync()
        result["graph"] = self.graph.sync()
        return result

    def search(self, query, agent=None, limit=6, project=None, kinds=None, status='active', include_global=False):
        if status != 'active':
            return super().search(query, agent, limit, project, kinds, status, include_global)
        lexical = super().search(query, agent=agent, limit=max(limit*4, 24), project=project,
                                 kinds=kinds, status=status, include_global=include_global)
        semantic = self.semantic.search(query, project=project, agent=agent, kinds=kinds,
                                        include_global=include_global, limit=max(limit*5, 30))
        merged = {}
        for item in lexical:
            row = dict(item)
            row["lexical_score"] = float(row.get("score", 0))
            row["semantic_similarity"] = 0.0
            merged[row["uid"]] = row
        for hit in semantic:
            uid = hit["uid"]
            if uid not in merged:
                item = self.get(uid)
                if not item: continue
                if agent and item.get("agent") != agent: continue
                if project is not None and not (item.get("project") == project or (include_global and not item.get("project"))): continue
                if kinds and item.get("kind") not in kinds: continue
                row = dict(item); row["lexical_score"] = 0.0; merged[uid] = row
            merged[uid]["semantic_similarity"] = hit["similarity"]
        # Graph expansion adds context connected through shared entities, but at a lower weight.
        seeds = [x["uid"] for x in sorted(merged.values(), key=lambda r:r.get("lexical_score",0), reverse=True)[:5]]
        graph_hits = self.graph.expand_memory_uids(seeds, limit=max(6, limit*2))
        for hit in graph_hits:
            uid = hit["uid"]
            if uid not in merged:
                item = self.get(uid)
                if not item or item.get("status") != 'active': continue
                if project is not None and not (item.get("project") == project or (include_global and not item.get("project"))): continue
                if kinds and item.get("kind") not in kinds: continue
                row=dict(item); row["lexical_score"]=0.0; row["semantic_similarity"]=0.0; merged[uid]=row
            merged[uid]["graph_strength"] = hit["strength"]
        now = dt.datetime.now(dt.timezone.utc)
        ranked=[]
        for item in merged.values():
            base = float(item.get("lexical_score",0))
            sem = max(0.0,float(item.get("semantic_similarity",0)))
            graph = min(2.0,float(item.get("graph_strength",0)))
            score = base + sem*3.25 + graph*.25
            score += float(item.get("confidence") or .5)*.35 + float(item.get("importance") or .5)*.45
            if item.get("qa_pass") or item.get("verified"): score += .6
            if item.get("confirmed_at") or item.get("confirmed"): score += .45
            try:
                changed=dt.datetime.fromisoformat(item.get("updated_at") or item.get("created_at"));age=max(0,(now-changed).days)
                score+=max(0,.2*(1-age/180))
            except (ValueError,TypeError): pass
            item["score"] = round(score,4)
            ranked.append(item)
        ranked.sort(key=lambda x:(x.get("score",0),x.get("id",0)),reverse=True)
        return ranked[:limit]

    def context(self, query, *, project=None, agent=None, limit=10):
        # Reserve space for goals so long-term direction cannot be crowded out by lexical hits.
        goal_items = self.goals.context(project, limit=min(3,max(1,limit//4)))
        memory_budget = max(2, limit-len(goal_items))
        relevant = self.search(query,agent=agent,project=project,limit=max(3,memory_budget),include_global=True)
        relevant += self.search(query,project=project,limit=max(3,memory_budget),include_global=True)
        anchors = self._anchors(project,max(2,memory_budget//3)) if project is not None else []
        chosen={}
        for goal in goal_items:
            chosen[goal["uid"]]=goal
        for reason,items in (("semantic_relevant",relevant),("project_anchor",anchors)):
            for item in items:
                if item["uid"] not in chosen:
                    item=dict(item);item["selection_reason"]=reason;chosen[item["uid"]]=item
                if len(chosen)>=limit:break
            if len(chosen)>=limit:break
        out=list(chosen.values())[:limit]
        memory_uids=[x["uid"] for x in out if x["uid"].startswith("MEM-")]
        if memory_uids:
            now=self._now()
            with self._connect() as c:
                c.executemany('UPDATE memories SET access_count=access_count+1,last_accessed_at=? WHERE uid=?',[(now,uid) for uid in memory_uids])
        keys=('uid','kind','title','summary','tags','project','confidence','importance','verified','confirmed',
              'source_type','source_ref','updated_at','selection_reason','semantic_similarity','graph_strength')
        return [{k:x.get(k) for k in keys} for x in out]

    def merge(self, primary_uid, duplicate_uid, reason="user approved consolidation"):
        """Human-approved near-duplicate merge: keep primary, supersede duplicate."""
        if primary_uid == duplicate_uid: raise ValueError("cannot merge a memory with itself")
        primary=self.get(primary_uid); duplicate=self.get(duplicate_uid)
        if not primary or not duplicate: raise KeyError("memory not found")
        if primary["status"]!='active' or duplicate["status"]!='active': raise ValueError("only active memories can be merged")
        with self._connect() as c:
            c.execute("UPDATE memories SET status='superseded',supersedes_uid=?,updated_at=? WHERE uid=?",
                      (primary_uid,self._now(),duplicate_uid))
            self._record_revision(c,duplicate_uid,'merge',reason,{'kept':primary_uid})
            try:self._link_locked(c,primary_uid,duplicate_uid,'supersedes')
            except sqlite3.IntegrityError:pass
        self.semantic.sync();self.graph.sync()
        return {'kept':primary_uid,'superseded':duplicate_uid,'reason':reason}

    def resolve_consolidation(self, candidate_id, action, reason="user decision"):
        candidates=[x for x in self.consolidator.list(status=None,limit=10000) if x['id']==int(candidate_id)]
        if not candidates: raise KeyError(candidate_id)
        c=candidates[0]
        if action == 'dismiss': return self.consolidator.dismiss(candidate_id)
        if action == 'keep-left': out=self.merge(c['left_uid'],c['right_uid'],reason)
        elif action == 'keep-right': out=self.merge(c['right_uid'],c['left_uid'],reason)
        elif action == 'mark-conflict':
            self.link(c['left_uid'],c['right_uid'],'contradicts')
            self.link(c['right_uid'],c['left_uid'],'contradicts')
            out={'conflict':[c['left_uid'],c['right_uid']]}
        else: raise ValueError("action must be dismiss, keep-left, keep-right or mark-conflict")
        self.consolidator.mark_resolved(candidate_id);return out

    def cognitive_stats(self):
        base=super().stats()
        base['embeddings']=self.semantic.stats()
        base['entity_graph']=self.graph.stats()
        base['goals']=self.goals.stats()
        base['consolidation']=self.consolidator.stats()
        return base

    def brief(self,project=None,limit=6):
        data=super().brief(project,limit)
        data['long_term_goals']=self.goals.list(project,'active',limit)
        data['consolidation_candidates']=self.consolidator.list(project,'open',limit)
        data['top_entities']=self.graph.list(project,limit)
        return data

    def brief_markdown(self,project=None,limit=6):
        data=self.brief(project,limit)
        stats=self.cognitive_stats()
        lines=['# Galaxy Brain — Semantic Working Memory','',f"Project: `{project or 'all'}`  ",f"Generated: `{self._now()}`",'',
               f"Embeddings: `{stats['embeddings']['model']}` · vectors: **{stats['embeddings']['vectors']}** · dims: **{stats['embeddings']['dims']}**  ",
               f"Entity graph: **{stats['entity_graph']['entities']} entities** / **{stats['entity_graph']['relations']} explicit relations**  ",
               f"Open consolidation candidates: **{sum(stats['consolidation'].values())}**",'']
        lines += ['## Long-term goals','']
        if not data['long_term_goals']: lines += ['_None._','']
        for g in data['long_term_goals']:
            lines.append(f"- **[{g['uid']}] {g['title']}** — priority {g['priority']:.2f} · {g['horizon']}")
            lines.append('  '+g['description'][:360]+('…' if len(g['description'])>360 else ''))
        sections=[
            ('Decisions & constraints','decisions_and_constraints'),('Procedures & lessons','procedures_and_lessons'),
            ('Open questions','open_questions'),('Recent verified / confirmed','recent_verified'),('Needs review','needs_review')]
        for title,key in sections:
            lines += ['',f'## {title}','']
            items=data[key]
            if not items: lines += ['_None._'];continue
            for item in items:
                trust='verified' if item.get('qa_pass') else 'confirmed' if item.get('confirmed_at') else f"confidence={item.get('confidence',.5):.2f}"
                lines.append(f"- **[{item['uid']}] {item['title']}** — {item['kind']} · {trust}")
                compact=' '.join((item.get('summary') or '').split())
                if compact: lines.append('  '+compact[:320]+('…' if len(compact)>320 else ''))
        lines += ['', '## Consolidation / conflict inbox','']
        if not data['consolidation_candidates']: lines += ['_No open candidates._']
        for c in data['consolidation_candidates']:
            lines.append(f"- **#{c['id']} {c['candidate_type']}** ({c['score']:.2f}) — `{c['left_uid']}` ↔ `{c['right_uid']}`")
            lines.append('  '+c['reason'])
        lines += ['', '## Entity graph — top nodes','']
        if not data['top_entities']: lines += ['_No entities._']
        for e in data['top_entities'][:limit]:
            lines.append(f"- `{e['uid']}` **{e['display_name']}** · {e['entity_type']} · memories={e['memory_count']}")
        lines += ['', '---', 'Semantic search, goals and managed memory are available through `brain-*` and `goal-*` commands.','']
        return '\n'.join(lines)
