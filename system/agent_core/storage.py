"""Small filesystem primitives shared by the runtime and verifier."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

VERSION = '1.7.1'
GENERATED = {'memory', 'traces', '__pycache__', '.git', '.obsidian'}

def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def content_manifest(root):
    """Hash project inputs, excluding engine state and private local settings."""
    root = Path(root)
    out = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(directory)/name
            if name not in GENERATED and path.is_symlink():
                out[path.relative_to(root).as_posix()] = 'symlink:' + os.readlink(path)
        dirs[:] = sorted(d for d in dirs if d not in GENERATED and not (Path(directory)/d).is_symlink())
        for name in sorted(files):
            p = Path(directory)/name
            rel = p.relative_to(root)
            if name.endswith(('.pyc', '.secret')) or name.startswith('.env') or name == 'benchmark-result.json':
                continue
            if rel.parts[0] == 'tasks' and len(rel.parts) > 2:
                continue  # execution evidence, not task specifications
            if p.is_symlink():
                out[rel.as_posix()] = 'symlink:' + os.readlink(p)
            else:
                data = p.read_bytes()
                if rel.parts[0] == 'tasks' and p.suffix == '.md':
                    # Official progress fields change independently of the specification.
                    text = data.decode('utf-8')
                    if text.startswith('---\n') and '\n---' in text[4:]:
                        end = text.index('\n---', 4)
                        lines = [x for x in text[4:end].splitlines() if x.split(':',1)[0] not in {'stage','status','assignee','retries_count'}]
                        data = ('\n'.join(lines)+text[end:]).encode()
                out[rel.as_posix()] = hashlib.sha256(data).hexdigest()
    return out

def digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()

def git_head(root):
    p = subprocess.run(['git','rev-parse','HEAD'], cwd=root, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else None

class WorkspaceLock:
    """OS advisory lock; survives process crashes without stale PID files."""
    def __init__(self, root):
        self.path = Path(root)/'traces'/'workspace.lock'
    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0); self.file.write(b'0'); self.file.flush(); self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError('Workspace is busy: another Galaxy run holds the lock') from exc
        return self
    def __exit__(self, *args):
        if os.name == 'nt':
            import msvcrt
            self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()
