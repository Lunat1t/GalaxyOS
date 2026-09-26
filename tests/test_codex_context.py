import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from codex_context import main


class CodexContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'auth.py').write_text('def login():\n    return "initial"\n')

    def call(self, *args, project='chat-a'):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(['--state-root', str(self.root / 'state'), '--project', project, *args])
        return json.loads(out.getvalue())

    def build(self, project='chat-a'):
        return self.call('build', 'login authentication context', '--root', str(self.repo),
                         project=project)['context']

    def test_memory_survives_calls_and_is_scoped(self):
        note = self.call('remember', 'login policy', 'login must use OAuth', '--source', 'chat/1')
        self.assertIn(note['uid'], [m['uid'] for m in self.build()['memories']])
        self.assertEqual([], self.build(project='chat-b')['memories'])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('forget', note['uid'], project='chat-b')

    def test_correction_and_retraction(self):
        note = self.call('remember', 'login policy', 'login uses passwords', '--source', 'chat/1')
        replacement = self.call('correct', note['uid'], 'login must use OAuth')
        ids = [m['uid'] for m in self.build()['memories']]
        self.assertNotIn(note['uid'], ids)
        self.assertIn(replacement['uid'], ids)
        self.call('forget', replacement['uid'])
        self.assertEqual([], self.build()['memories'])

    def test_build_refreshes_repository(self):
        self.build()
        (self.repo / 'auth.py').write_text('def login():\n    return "updated OAuth"\n')
        files = self.build()['files']
        self.assertIn('updated OAuth', next(f['excerpt'] for f in files if f['path'] == 'auth.py'))

    def test_invalid_root_does_not_create_state(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.call('build', 'login', '--root', str(self.root / 'missing'))
        self.assertFalse((self.root / 'state').exists())
