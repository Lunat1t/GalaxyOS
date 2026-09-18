"""Repository copy isolation, not a security boundary against native processes."""
from pathlib import Path
import shutil
import tempfile

CANONICAL_WRITE_AGENTS = {'Earth','Neptun'}
CANONICAL_COMMIT_AGENTS = set()

class AgentSandbox:
    def __init__(self,root,agent):
        self.root = Path(root)
        self.agent = agent
        self._tmp = None
        self.execution_root = self.root
        self.mode = 'canonical-write'
    def __enter__(self):
        if self.agent in CANONICAL_WRITE_AGENTS:
            return self
        self._tmp = tempfile.TemporaryDirectory(prefix=f'galaxy-{self.agent.lower()}-')
        target = Path(self._tmp.name)/'repo'
        def ignore(directory,names):
            excluded = {'__pycache__','memory','traces','.obsidian'}
            if Path(directory).relative_to(self.root).parts[:1] == ('tasks',):
                excluded.update(n for n in names if (Path(directory)/n).is_dir())
            return [n for n in names if n in excluded or n.startswith('.env') or n.endswith(('.pyc','.secret')) or (Path(directory)/n).is_symlink() or (n == '.git' and (Path(directory)/n).is_file())]
        try:
            # Standalone .git directories are copied for read-only Git inspection;
            # linked-worktree .git pointer files are omitted to avoid parent access.
            shutil.copytree(self.root,target,ignore=ignore)
        except BaseException:
            self._tmp.cleanup(); raise
        self.execution_root,self.mode = target,'disposable-copy'
        return self
    def __exit__(self,*args):
        if self._tmp: self._tmp.cleanup()
