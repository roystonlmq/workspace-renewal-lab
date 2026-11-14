"""One guarded operational candidate, with no implicit model or price defaults.

Wiring: a NightlyRunner connector factory can return
``improvement=ImprovementPipeline(...)``. Or call ``create(config)`` with an
``improvement`` object and an explicit trusted callbacks_factory returning
checks(candidate_path), postcheck(target_path), and active() callbacks.

The configured harness receives a private working directory, approved input
copies, request.json and the explicit prompt on stdin. It must write one UTF-8
candidate.json object: {schema:1,status:'candidate',path:'skills/example.md',
before_sha256:'...',content:'...'}, or {schema:1,status:'no_candidate'}.
Only that one allowlisted patch can be applied. Raw inputs and harness output
are private evidence, excluded from receipts and public packages. A private cwd
is context isolation, not an operating-system sandbox for a hostile executable.
"""
from __future__ import annotations

import importlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .controls import ChangeGate, Rejected, atomic_write, digest
from .experiment import Ledger, validate_manifest
from .harness import execute


class ImprovementPipeline:
    def __init__(self, *, gate: ChangeGate, state_dir, targets, protected_evals,
                 checks, postcheck, ledger: Ledger, proposal=None,
                 harness_command=None, metadata=None, prompt=None, timeout=90,
                 max_candidate_bytes=65536):
        if not callable(checks) or not callable(postcheck):
            raise ValueError('trusted precheck and postcheck are required')
        if not 0 < timeout <= 300 or not 0 < max_candidate_bytes <= 65536:
            raise ValueError('candidate execution bounds exceeded')
        if not isinstance(targets, (list, tuple)) or not 1 <= len(targets) <= 10:
            raise ValueError('one to ten explicit allowed targets required')
        requested_state = Path(state_dir).absolute()
        if requested_state.is_symlink():
            raise ValueError('private state cannot be a symlink')
        self.gate, self.state_dir = gate, requested_state.resolve()
        self.targets = tuple(targets)
        self.evals, self.freeze = map(lambda p: Path(p).resolve(), protected_evals)
        self.checks, self.postcheck, self.ledger = checks, postcheck, ledger
        self.proposal, self.command = proposal, harness_command
        self.metadata, self.prompt = metadata or {}, prompt
        self.timeout, self.max_bytes = timeout, max_candidate_bytes

    def __call__(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.state_dir.is_symlink():
            raise ValueError('private state cannot be a symlink')
        self.state_dir.chmod(0o700)
        attempt = self.state_dir / uuid.uuid4().hex
        attempt.mkdir(mode=0o700)
        workspace = attempt / 'workspace'
        workspace.mkdir(mode=0o700)
        receipt = dict(schema=1, attempt=attempt.name, status='started', reason='started',
                       started_at=datetime.now(timezone.utc).isoformat(),
                       paid_call_attempts=0, automatic_retries=0, patches_applied=0)
        self._record(attempt, receipt)
        change = None
        applied = False
        def finish(status, reason, **extra):
            receipt.update(status=status, reason=reason,
                           finished_at=datetime.now(timezone.utc).isoformat(), **extra)
            self._record(attempt, receipt)
            return dict(status=status, reason=reason, receipt=str(attempt / 'receipt.json'))
        def failure(status, reason):
            nonlocal applied
            if applied:
                try:
                    self.gate.rollback(change)
                    applied = False
                except (Rejected, OSError):
                    return finish('blocked', 'rollback_conflict')
                return finish('rolled_back', reason, patches_applied=0)
            return finish(status, reason)
        try:
            if self.gate.active() is not False:
                return finish('blocked', 'active_work')
            freeze_hash = digest(self.freeze.read_bytes())
            self._validate_evals(freeze_hash)
            snapshots = {}
            for relative in self.targets:
                target = self.gate.target(relative)
                before = target.read_bytes() if target.exists() else None
                if before is not None and (len(before) > self.max_bytes or self.gate.protected.search(before.decode(errors='replace'))):
                    return finish('rejected', 'protected_or_oversized_input')
                snapshots[relative] = digest(before) if before is not None else None
                if before is not None:
                    atomic_write(workspace / 'inputs' / relative, before)
            atomic_write(workspace / 'request.json', json.dumps(dict(schema=1,
                producer='workspace-renewal-nightly', max_patches=1,
                max_candidate_bytes=self.max_bytes, targets=snapshots)).encode())
            proposal = self.proposal
            if proposal is None:
                if not self._configured_harness():
                    return finish('blocked', 'harness_unconfigured')
                if self.gate.active() is not False:
                    return finish('blocked', 'active_work')
                self._validate_evals(freeze_hash)
                self.ledger.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor = os.open(self.ledger.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
                os.close(descriptor)
                self.ledger.path.chmod(0o600)
                receipt['paid_call_attempts'] = 1
                self._record(attempt, receipt)
                # execute durably begins the ledger entry before Popen, even if missing.
                call = execute(self.command, cwd=workspace, prompt=self.prompt,
                    metadata=dict(self.metadata, phase='operational-improvement',
                                  producer='workspace-renewal-nightly', attempt=attempt.name),
                    ledger=self.ledger, output=attempt / 'private-harness-output', timeout=self.timeout)
                receipt['call_id'] = call['id']
                if call['status'] != 'completed':
                    return finish('blocked', 'harness_unavailable_or_failed')
                candidate = workspace / 'candidate.json'
                if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > self.max_bytes:
                    return finish('rejected', 'candidate_missing_or_oversized')
                proposal = json.loads(candidate.read_text())
            if not isinstance(proposal, dict) or proposal.get('schema') != 1:
                return finish('rejected', 'invalid_candidate_contract')
            if proposal.get('status') == 'no_candidate':
                return finish('no_candidate', 'no_candidate')
            required = {'schema', 'status', 'path', 'before_sha256', 'content'}
            if set(proposal) != required or proposal['status'] != 'candidate':
                return finish('rejected', 'invalid_candidate_contract')
            relative, content = proposal['path'], proposal['content']
            if relative not in snapshots or not isinstance(content, str) or len(content.encode()) > self.max_bytes:
                return finish('rejected', 'candidate_outside_bounds')
            target = self.gate.target(relative)
            if proposal['before_sha256'] != snapshots[relative]:
                return finish('rejected', 'base_hash_conflict')
            receipt.update(path=relative, before_sha256=snapshots[relative], after_sha256=digest(content.encode()))
            self._record(attempt, receipt)
            def protected_checks(candidate):
                self._validate_evals(freeze_hash)
                current = digest(target.read_bytes()) if target.exists() else None
                if current != snapshots[relative]:
                    return False
                passed = self.checks(candidate) is True
                self._validate_evals(freeze_hash)
                current = digest(target.read_bytes()) if target.exists() else None
                return passed and current == snapshots[relative]
            change = self.gate.apply(relative, content, protected_checks)
            applied = True
            receipt.update(change_id=change['id'], patches_applied=1)
            self._record(attempt, receipt)
            try:
                self._validate_evals(freeze_hash)
                passed = self.postcheck(target) is True
                self._validate_evals(freeze_hash)
                passed = passed and target.exists() and digest(target.read_bytes()) == change['after']
            except Exception:
                passed = False
            if not passed:
                try:
                    self.gate.rollback(change)
                    applied = False
                except (Rejected, OSError):
                    return finish('blocked', 'rollback_conflict')
                return finish('rolled_back', 'postcheck_failed', patches_applied=0)
            return finish('applied', 'protected_checks_passed')
        except Rejected:
            return failure('rejected', 'change_gate_rejected')
        except (OSError, ValueError, TypeError, KeyError):
            return failure('rejected', 'invalid_input_or_protected_checks')
        except Exception:
            return failure('blocked', 'trusted_dependency_error')

    def _validate_evals(self, freeze_hash):
        if digest(self.freeze.read_bytes()) != freeze_hash:
            raise Rejected('Protected acceptance freeze changed')
        validate_manifest(self.evals, self.freeze)

    def _configured_harness(self):
        required = ('harness', 'model', 'effort', 'harness_version')
        return (isinstance(self.command, list) and bool(self.command)
            and all(isinstance(part, str) and part for part in self.command)
            and Path(self.command[0]).is_absolute()
            and all(isinstance(self.metadata.get(k), str) and self.metadata[k] for k in required)
            and isinstance(self.prompt, str) and bool(self.prompt.strip()))

    @staticmethod
    def _record(folder, value):
        atomic_write(folder / 'receipt.json', json.dumps(value, sort_keys=True, indent=2).encode())
        lines = ['# Operational improvement receipt', '',
                 'Status: ' + value['status'] + '.',
                 'Reason: ' + value['reason'].replace('_', ' ') + '.',
                 'Attempt: ' + value['attempt'] + '.',
                 'Paid-call attempts: ' + str(value['paid_call_attempts']) + '.',
                 'Automatic retries: 0.',
                 'Patches currently applied: ' + str(value['patches_applied']) + '.',
                 'Candidate content is retained only in private recovery evidence.']
        atomic_write(folder / 'receipt.md', ('\n'.join(lines) + '\n').encode())


def create(config):
    """Build the callback for a trusted NightlyRunner connector factory.

    Required config.improvement: root, state_dir, ledger, targets, eval_dir,
    freeze_manifest, callbacks_factory='operator_module:function'. That factory
    returns checks, postcheck and active. Supply proposal_file for deterministic
    replay, or harness_command, explicit metadata and prompt_file for generation.
    No candidate-provided callback, model default or price assumption is loaded.
    """
    settings = config['improvement']
    module, name = settings['callbacks_factory'].split(':', 1)
    callbacks = getattr(importlib.import_module(module), name)(config)
    if not all(callable(callbacks.get(k)) for k in ('checks', 'postcheck', 'active')):
        raise ValueError('trusted protected checks and activity callback required')
    state = Path(settings['state_dir']).resolve()
    gate = ChangeGate(settings['root'], state / 'changes', active=callbacks['active'])
    proposal = json.loads(Path(settings['proposal_file']).read_text()) if settings.get('proposal_file') else None
    prompt = Path(settings['prompt_file']).read_text() if settings.get('prompt_file') else None
    return ImprovementPipeline(gate=gate, state_dir=state / 'attempts', targets=settings['targets'],
        protected_evals=(settings['eval_dir'], settings['freeze_manifest']),
        checks=callbacks['checks'], postcheck=callbacks['postcheck'], ledger=Ledger(settings['ledger']),
        proposal=proposal, harness_command=settings.get('harness_command'),
        metadata=settings.get('metadata'), prompt=prompt, timeout=settings.get('timeout_seconds', 90))
