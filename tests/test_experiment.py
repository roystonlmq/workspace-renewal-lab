import json
import tempfile
import unittest
from pathlib import Path

from renewal.experiment import Ledger, accepted, compare, validate_manifest


class ExperimentTest(unittest.TestCase):
    def test_partial_report_cannot_win(self):
        check = {'kind':'report','required_sections':['Findings','Evidence','Next action'],'location':'reports/result.md'}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'reports').mkdir()
            (root/'reports/result.md').write_text('Findings\nSome text')
            self.assertFalse(accepted(check,root,{}))
            (root/'reports/result.md').write_text('Findings\nSynthetic conclusion\nEvidence\nfixture\nNext action\nverify')
            self.assertTrue(accepted(check,root,{}))
            self.assertFalse(accepted(dict(check,location='../outside'),root,{}))
            (root/'reports/result.md').write_text('Findings\nEvidence\nNext action\nverify')
            self.assertFalse(accepted(check,root,{}))

    def test_playbook_requires_all_applicable_results(self):
        check = {'kind':'playbook','tests':['normal','edge','regression']}
        self.assertFalse(accepted(check,Path('.'),{'tests':{'normal':True,'edge':True}}))
        self.assertFalse(accepted(check,Path('.'),{'tests':{'normal':True,'edge':False,'regression':True}}))
        self.assertTrue(accepted(check,Path('.'),{'tests':{'normal':True,'edge':True,'regression':True}}))

    def test_incomplete_run_keeps_spend_and_unknown_is_not_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder)/'runs.jsonl')
            key = ledger.begin({'model':'example-model','condition':'current-local','cache':'cold','task':'report','repeat':1})
            self.assertEqual(ledger.rows()[0]['status'],'running')
            ledger.finish(key,status='failed',usage={'input_tokens':100},estimated_usd=None,actual_charge_usd=None)
            self.assertIsNone(ledger.rows()[0]['estimated_usd'])
            self.assertEqual(ledger.rows()[0]['usage']['input_tokens'],100)

    def test_compare_only_matched_successes_and_reports_completion(self):
        base={'task':'r','repeat':1,'cache':'cold','cache_verified':True,'model':'model','observed_model':'model','effort':'high','permissions':'read-only','fixture_hash':'a','tools_hash':'b','harness_version':'v','status':'completed','accepted':True,'estimated_usd':1}
        rows=[dict(base,condition=c) for c in ('current-local','clean-local','clean-notion')]
        rows.append(dict(base,condition='clean-notion',repeat=2,status='failed',accepted=False,estimated_usd=3))
        result=compare(rows)
        self.assertEqual(result['matched_groups'],1)
        self.assertEqual(result['conditions']['clean-notion']['completion_rate'],.5)
        self.assertEqual(result['conditions']['clean-notion']['total_estimated_usd'],4)
        rows[2]['effort']='low'
        self.assertEqual(compare(rows)['matched_groups'],0)

    def test_unverified_cache_or_model_cannot_be_matched(self):
        base={'task':'r','repeat':1,'cache':'cold','cache_verified':True,'model':'model','observed_model':'model','effort':'high','permissions':'read-only','fixture_hash':'a','tools_hash':'b','harness_version':'v','status':'completed','accepted':True,'estimated_usd':1}
        rows=[dict(base,condition=c) for c in ('current-local','clean-local','clean-notion')]
        rows[0]['cache_verified']=False
        self.assertEqual(compare(rows)['matched_groups'],0)
        rows[0]['cache_verified']=True
        rows[1]['observed_model']='different-model'
        self.assertEqual(compare(rows)['matched_groups'],0)

    def test_acceptance_manifest_freeze_detects_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'manifest.json'
            path.write_text('{"schema":1}')
            import hashlib
            freeze=Path(folder)/'freeze.json'
            freeze.write_text(json.dumps({'manifest.json':hashlib.sha256(path.read_bytes()).hexdigest()}))
            validate_manifest(Path(folder),freeze)
            path.write_text('{"schema":2}')
            with self.assertRaises(ValueError):
                validate_manifest(Path(folder),freeze)
