"""Galaxy 2.0 cognitive-memory regression tests."""
import tempfile, unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from galaxy_core.brain.memory import MemoryStore

class CognitiveMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        (self.root/'data'/'vault'/'projects').mkdir(parents=True)
        self.m=MemoryStore(self.root)

    def test_confirm_correct_forget_lifecycle(self):
        mid=self.m.remember('decision','Database','Use SQLite',project='galaxy',confidence=.6)
        old=self.m.get(mid);self.assertFalse(old['confirmed'])
        confirmed=self.m.confirm(old['uid']);self.assertTrue(confirmed['confirmed']);self.assertGreaterEqual(confirmed['confidence'],.9)
        new=self.m.correct(old['uid'],'Use SQLite locally, Supabase only for shared remote state',reason='scope clarified')
        self.assertEqual(self.m.get(old['uid'])['status'],'superseded');self.assertEqual(new['supersedes_uid'],old['uid'])
        self.assertTrue(any(x['relation']=='supersedes' for x in self.m.links(new['uid'])))
        forgotten=self.m.forget(new['uid'],'obsolete');self.assertEqual(forgotten['status'],'retracted')
        self.assertEqual(self.m.search('Supabase',project='galaxy'),[])

    def test_permanent_delete_removes_content_but_keeps_audit_event(self):
        mid=self.m.remember('fact','Temporary secretless fact','Delete this content')
        uid=self.m.get(mid)['uid'];result=self.m.delete(uid,'privacy cleanup')
        self.assertTrue(result['deleted']);self.assertIsNone(self.m.get(uid))
        with self.m._connect() as c:
            row=c.execute('SELECT action,reason FROM memory_audit WHERE uid=?',(uid,)).fetchone()
        self.assertEqual(row['action'],'delete');self.assertEqual(row['reason'],'privacy cleanup')

    def test_project_anchor_is_in_context_without_keyword_match(self):
        anchor=self.m.remember('constraint','Production rule','Never commit from an agent',project='galaxy',importance=1,confirmed=True)
        self.m.remember('fact','Calculator','Division uses decimal arithmetic',project='galaxy')
        ctx=self.m.context('Fix calculator division',project='galaxy',agent='Earth',limit=6)
        uids={x['uid'] for x in ctx};self.assertIn(self.m.get(anchor)['uid'],uids)
        selected=next(x for x in ctx if x['uid']==self.m.get(anchor)['uid']);self.assertEqual(selected['selection_reason'],'project_anchor')

    def test_links_and_brief(self):
        a=self.m.get(self.m.remember('decision','Storage','Use SQLite',project='x',confirmed=True,importance=.9))
        b=self.m.get(self.m.remember('constraint','Offline','Must run offline',project='x',confirmed=True,importance=.8))
        self.m.link(a['uid'],b['uid'],'supports')
        brief=self.m.brief('x')
        titles={x['title'] for x in brief['decisions_and_constraints']};self.assertEqual({'Storage','Offline'},titles)
        self.assertEqual(self.m.links(a['uid'])[0]['to_uid'],b['uid'])

    def test_markdown_ingest_is_incremental_and_retracts_removed_sections(self):
        note=self.root/'data'/'vault'/'projects'/'galaxy.md'
        note.write_text('---\nproject: galaxy\ntags: memory,architecture\n---\n# Architecture\nUse SQLite for local durable state.\n\n## Rule\nNever let agents commit.\n',encoding='utf-8')
        first=self.m.ingest_vault();self.assertEqual(first['created'],2)
        second=self.m.ingest_vault();self.assertEqual(second['created'],0);self.assertGreaterEqual(second['unchanged'],2)
        hit=self.m.search('durable SQLite',project='galaxy');self.assertTrue(hit);self.assertEqual(hit[0]['source_type'],'markdown')
        note.write_text('---\nproject: galaxy\n---\n# Architecture\nUse SQLite WAL for local durable state.\n',encoding='utf-8')
        third=self.m.ingest_vault();self.assertGreaterEqual(third['updated'],1);self.assertGreaterEqual(third['retracted'],1)
        self.assertEqual(self.m.search('agents commit',project='galaxy'),[])
        self.assertTrue(self.m.search('SQLite WAL',project='galaxy'))

    def test_review_queue_excludes_confirmed(self):
        self.m.remember('fact','Needs review','Unconfirmed statement',project='p',importance=.7)
        self.m.remember('decision','Trusted','Confirmed choice',project='p',confirmed=True,importance=.9)
        queue=self.m.review_queue('p');titles={x['title'] for x in queue}
        self.assertIn('Needs review',titles);self.assertNotIn('Trusted',titles)

    def test_ingested_task_becomes_searchable(self):
        note=self.root/'data'/'vault'/'projects'/'task-123.md'
        note.write_text('---\nid: TASK-123\nproject: alpha\n---\n# Build API cache\nCache responses for five minutes and keep stale fallback.\n',encoding='utf-8')
        self.m.ingest_vault();hits=self.m.search('stale fallback cache',project='alpha')
        self.assertTrue(hits);self.assertEqual(hits[0]['source_ref'],'data/vault/projects/task-123.md')
        self.assertEqual(hits[0]['kind'],'note')

if __name__=='__main__': unittest.main(verbosity=2)
