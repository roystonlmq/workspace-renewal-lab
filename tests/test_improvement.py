import hashlib
import json
import sys
import tempfile
import unittest
import types
from unittest.mock import patch
from pathlib import Path

from renewal.controls import ChangeGate
from renewal.experiment import Ledger
from renewal.improvement import ImprovementPipeline, create


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.target = self.workspace / 'skills' / 'example.md'
        self.target.parent.mkdir(parents=True)
        self.target.write_text('original synthetic instructions')
        self.evals = self.root / 'protected'
        self.evals.mkdir()
        (self.evals / 'cases.json').write_text('{"synthetic": true}')
        self.freeze = self.evals / 'freeze.json'
        self.freeze.write_text(json.dumps({'cases.json': hashlib.sha256((self.evals / 'cases.json').read_bytes()).hexdigest()}))
        self.state = self.root / 'private-state'
        self.gate = ChangeGate(self.workspace, self.state / 'changes', active=lambda: False)
        self.ledger = Ledger(self.state / 'calls.jsonl')

    def proposal(self, **overrides):
        value = dict(schema=1, status='candidate', path='skills/example.md',
                     before_sha256=hashlib.sha256(self.target.read_bytes()).hexdigest(),
                     content='improved synthetic instructions')
        return dict(value, **overrides)

    def pipeline(self, **overrides):
        arguments = dict(gate=self.gate, state_dir=self.state / 'attempts',
            targets=['skills/example.md'], protected_evals=(self.evals, self.freeze),
            checks=lambda candidate: candidate.read_text() == 'improved synthetic instructions',
            postcheck=lambda target: target.read_text() == 'improved synthetic instructions',
            ledger=self.ledger, proposal=self.proposal())
        arguments.update(overrides)
        return ImprovementPipeline(**arguments)

    def test_accepted_candidate_applies_after_checks_and_has_content_free_receipt(self):
        result = self.pipeline()()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.target.read_text(), 'improved synthetic instructions')
        receipt = Path(result['receipt'])
        text = receipt.read_text() + receipt.with_suffix('.md').read_text()
        self.assertNotIn('improved synthetic instructions', text)
        self.assertNotIn('original synthetic instructions', text)
        self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.ledger.rows(), [])

    def test_protected_mutation_and_unapproved_paths_are_rejected(self):
        for proposal in [self.proposal(path='evals/cases.json'),
                         self.proposal(content='Change the spending budget'),
                         self.proposal(path='hooks/unapproved.py')]:
            with self.subTest(path=proposal['path']):
                result = self.pipeline(proposal=proposal)()
                self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')

    def test_eval_mutation_during_checks_prevents_application(self):
        def checks(candidate):
            (self.evals / 'cases.json').write_text('changed acceptance')
            return True
        result = self.pipeline(checks=checks)()
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')

    def test_failed_postcheck_rolls_back_only_own_change(self):
        unrelated = self.workspace / 'skills' / 'unrelated.md'
        unrelated.write_text('before')
        def postcheck(target):
            unrelated.write_text('other person edit')
            return False
        result = self.pipeline(postcheck=postcheck)()
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')
        self.assertEqual(unrelated.read_text(), 'other person edit')

    def test_rollback_conflict_preserves_newer_target_edit(self):
        def postcheck(target):
            target.write_text('newer other person edit')
            return False
        result = self.pipeline(postcheck=postcheck)()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'rollback_conflict')
        self.assertEqual(self.target.read_text(), 'newer other person edit')

    def test_no_candidate_never_invokes_checks_or_paid_harness(self):
        def forbidden(candidate):
            self.fail('no candidate must not run checks')
        result = self.pipeline(proposal={'schema': 1, 'status': 'no_candidate'}, checks=forbidden)()
        self.assertEqual(result['status'], 'no_candidate')
        self.assertEqual(self.ledger.rows(), [])

    def test_missing_harness_records_exactly_one_failed_call_without_apply(self):
        result = self.pipeline(proposal=None, harness_command=['/missing/synthetic-harness'],
            metadata={'harness': 'synthetic', 'model': 'fixture-model', 'effort': 'explicit', 'harness_version': 'fixture'},
            prompt='synthetic isolated request')()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(len(self.ledger.rows()), 1)
        self.assertEqual(self.ledger.rows()[0]['status'], 'blocked')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')

    def test_model_contract_runs_once_in_private_workspace_with_pre_call_ledger(self):
        code = ('import json,pathlib; '
                'events=[json.loads(x) for x in pathlib.Path(' + repr(str(self.ledger.path)) + ').read_text().splitlines()]; '
                'assert len(events)==1 and events[0]["status"]=="running"; '
                'assert pathlib.Path.cwd().name=="workspace"; '
                'assert pathlib.Path("inputs/skills/example.md").read_text()=="original synthetic instructions"; '
                'pathlib.Path("candidate.json").write_text(' + repr(json.dumps(self.proposal())) + ')')
        result = self.pipeline(proposal=None, harness_command=[sys.executable, '-c', code],
            metadata={'harness': 'synthetic', 'model': 'fixture-model', 'effort': 'explicit', 'harness_version': 'fixture'},
            prompt='synthetic isolated request')()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(len(self.ledger.rows()), 1)
        self.assertIsNone(self.ledger.rows()[0]['actual_charge_usd'])

    def test_active_work_and_stale_base_hash_prevent_apply(self):
        self.gate.active = lambda: True
        self.assertEqual(self.pipeline()()['status'], 'blocked')
        self.gate.active = lambda: False
        self.assertEqual(self.pipeline(proposal=self.proposal(before_sha256='0' * 64))()['status'], 'rejected')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')

    def test_candidate_checks_cannot_hide_concurrent_root_edit(self):
        def checks(candidate):
            self.target.write_text('other active edit')
            return True
        self.assertEqual(self.pipeline(checks=checks)()['status'], 'rejected')
        self.assertEqual(self.target.read_text(), 'other active edit')

    def test_postcheck_exception_rolls_back_and_missing_model_metadata_never_launches(self):
        def postcheck(target):
            raise RuntimeError('synthetic check error')
        self.assertEqual(self.pipeline(postcheck=postcheck)()['status'], 'rolled_back')
        result = self.pipeline(proposal=None, harness_command=[sys.executable],
                               metadata={'harness': 'synthetic'}, prompt='synthetic')()
        self.assertEqual(result['reason'], 'harness_unconfigured')
        self.assertEqual(self.ledger.rows(), [])

    def test_factory_returns_runnable_callback_without_modifying_shared_files(self):
        proposal_file = self.root / 'proposal.json'
        proposal_file.write_text(json.dumps(self.proposal()))
        module = types.ModuleType('synthetic_improvement_callbacks')
        module.create = lambda config: dict(checks=lambda p: True, postcheck=lambda p: True, active=lambda: False)
        config = {'improvement': dict(root=str(self.workspace), state_dir=str(self.state),
            ledger=str(self.ledger.path), targets=['skills/example.md'], eval_dir=str(self.evals),
            freeze_manifest=str(self.freeze), callbacks_factory='synthetic_improvement_callbacks:create',
            proposal_file=str(proposal_file))}
        with patch.dict(sys.modules, {'synthetic_improvement_callbacks': module}):
            result = create(config)()
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(self.target.read_text(), 'improved synthetic instructions')

    def test_receipt_error_after_apply_still_recovers_own_patch(self):
        original = ImprovementPipeline._record
        failed = []
        def record(folder, value):
            if value.get('change_id') and value['status'] == 'started' and not failed:
                failed.append(True)
                raise OSError('synthetic transient receipt error')
            return original(folder, value)
        with patch.object(ImprovementPipeline, '_record', side_effect=record):
            result = self.pipeline()()
        self.assertEqual(result['status'], 'rolled_back')
        self.assertEqual(self.target.read_text(), 'original synthetic instructions')


if __name__ == '__main__':
    unittest.main()
