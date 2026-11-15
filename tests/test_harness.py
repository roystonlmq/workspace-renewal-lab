import json
import sys
import tempfile
import unittest
from pathlib import Path

from renewal.harness import execute
from renewal.experiment import Ledger


class HarnessTest(unittest.TestCase):
    def test_claude_single_observed_model_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            event = {'type':'result','usage':{'input_tokens':2}, 'modelUsage':{'fixture-model':{'inputTokens':2}}, 'total_cost_usd':0.01}
            row = execute([sys.executable, '-c', 'print('+repr(json.dumps(event))+')'], cwd=folder,
                          prompt='synthetic', metadata={}, ledger=Ledger(Path(folder)/'calls'), output=Path(folder)/'logs')
            self.assertEqual(row['observed_model'], 'fixture-model')
    def test_usage_and_failures_are_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            ledger=Ledger(root/'calls.jsonl')
            command=[sys.executable,'-c','import json; print(json.dumps({"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":3,"output_tokens":2}}))']
            row=execute(command,cwd=root,prompt='synthetic',metadata={'harness':'synthetic','model':'fixture'},ledger=ledger,output=root/'logs',timeout=3)
            self.assertEqual(row['usage']['input_tokens'],10)
            self.assertIsNone(row['actual_charge_usd'])
            self.assertEqual(ledger.rows()[0]['status'],'completed')
            failed=execute([sys.executable,'-c','raise SystemExit(3)'],cwd=root,prompt='synthetic',metadata={},ledger=ledger,output=root/'logs',timeout=3)
            self.assertEqual(failed['status'],'failed')
            self.assertEqual(len(ledger.rows()),2)

    def test_timeout_is_retained_without_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            ledger=Ledger(root/'calls.jsonl')
            row=execute([sys.executable,'-c','import time; time.sleep(5)'],cwd=root,prompt='synthetic',metadata={},ledger=ledger,output=root/'logs',timeout=.05)
            self.assertEqual(row['status'],'timeout')
            self.assertEqual(len(ledger.rows()),1)
