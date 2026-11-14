"""Held-out negative outcomes are independently labelled, never tuned away."""
import json
import tempfile
import unittest
from pathlib import Path

from renewal.experiment import accepted, validate_manifest


class ProtectedCasesTest(unittest.TestCase):
    def test_declared_heldout_failures_are_rejected(self):
        package=Path(__file__).resolve().parents[1]
        validate_manifest(package/'evals',package/'evals/freeze.json')
        cases=json.loads((package/'evals/heldout.json').read_text())['cases']
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'reports').mkdir()
            for case in cases:
                if 'check' not in case:
                    continue
                with self.subTest(case=case['id']):
                    result={}
                    if case['id']=='report-wrong-location':
                        (root/'reports/wrong.md').write_text('Conclusion\ncomplete\nSource evidence\nfixture\nAction\nverify')
                    elif case['id']=='report-half-written':
                        (root/'reports/delivered.md').write_text('Conclusion\npartial')
                    elif case['id']=='playbook-regression-missing':
                        result={'tests':{'new-path':True,'existing-path':True}}
                    elif case['id']=='investigation-no-evidence':
                        result={'root_cause':'synthetic idempotency collision','reproduction':'synthetic','next_action':'repair'}
                    self.assertFalse(accepted(case['check'],root,result))
