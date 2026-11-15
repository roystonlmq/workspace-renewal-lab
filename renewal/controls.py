"""Guarded operational edits with conflict-aware recovery.

The check callback is trusted operator code, never candidate-provided code.
This gate is not a sandbox for executing untrusted programs.
"""
import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path


class Rejected(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.renewal-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ChangeGate:
    protected = re.compile(r'spend|budget|billing|acceptance|eval|privacy|sharing|retention|recovery|policy|credential|secret', re.I)

    def __init__(self, root, state, active=None):
        self.root = Path(root).resolve()
        self.state = Path(state).resolve()
        self.active = active or (lambda: True)

    def target(self, relative):
        rel = Path(relative)
        if not rel.parts or rel.is_absolute() or '..' in rel.parts or rel.parts[0] not in {'skills', 'hooks', 'instructions'}:
            raise Rejected('Only approved operational directories may change')
        if self.protected.search(str(rel)):
            raise Rejected('Protected control cannot be changed automatically')
        target = self.root / rel
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.root.parent):
            raise Rejected('Symlink target rejected')
        if not target.resolve().is_relative_to(self.root):
            raise Rejected('Target escapes root')
        return target

    def apply(self, relative, content, checks):
        if self.active():
            raise Rejected('Active work prevents shared edits')
        target = self.target(relative)
        before = target.read_bytes() if target.exists() else None
        original_mode = target.stat().st_mode & 0o777 if target.exists() else 0o600
        after = content.encode() if isinstance(content, str) else content
        # Changes touching protected instructions require an operator-owned flow.
        if self.protected.search(after.decode(errors='replace')) or (before and self.protected.search(before.decode(errors='replace'))):
            raise Rejected('Protected instruction requires manual control review')
        self.state.mkdir(parents=True, exist_ok=True)
        receipt = {'id': uuid.uuid4().hex, 'path': str(target.relative_to(self.root)),
                   'before': digest(before) if before is not None else None, 'after': digest(after), 'mode':original_mode}
        folder = self.state / receipt['id']
        folder.mkdir()
        atomic_write(folder / 'receipt.json', json.dumps(receipt).encode())
        if before is not None:
            atomic_write(folder / 'original', before)
        atomic_write(folder / 'candidate', after)
        try:
            passed = checks(folder / 'candidate')
        except Exception as error:
            atomic_write(folder / 'result.json', json.dumps({'status':'failed', 'error_type':type(error).__name__}).encode())
            raise Rejected('Protected checks errored') from error
        if passed is not True:
            atomic_write(folder / 'result.json', b'{"status":"failed"}')
            raise Rejected('Protected checks did not pass')
        current = target.read_bytes() if target.exists() else None
        if current != before or self.active():
            raise Rejected('Concurrent modification or active work')
        if (folder / 'candidate').read_bytes() != after:
            raise Rejected('Candidate changed during validation')
        try:
            atomic_write(target, after)
            target.chmod(original_mode)
            atomic_write(folder / 'result.json', b'{"status":"applied"}')
        except OSError as error:
            # Installation can succeed before chmod or its receipt fails. Restore
            # only our matching bytes, even when result storage is unavailable.
            try:
                self._restore(receipt)
            except (OSError, Rejected) as recovery_error:
                raise Rejected('Install failed; recovery requires conflict review') from recovery_error
            try:
                atomic_write(folder / 'result.json', b'{"status":"rolled_back","reason":"install_error"}')
            except OSError:
                pass  # Original, candidate and receipt still permit inspection.
            raise Rejected('Install failed; original restored') from error
        return receipt

    def rollback(self, receipt):
        self._restore(receipt)
        folder = self.state / receipt['id']
        atomic_write(folder / 'result.json', b'{"status":"rolled_back"}')

    def _restore(self, receipt):
        target = self.target(receipt['path'])
        folder = self.state / receipt['id']
        saved = json.loads((folder / 'receipt.json').read_text())
        if saved != receipt or digest(target.read_bytes()) != receipt['after']:
            raise Rejected('Rollback would discard another change')
        if receipt['before'] is None:
            target.unlink()
        else:
            original = (folder / 'original').read_bytes()
            if digest(original) != receipt['before']:
                raise Rejected('Recovery copy failed integrity check')
            atomic_write(target, original)
            target.chmod(receipt['mode'])
