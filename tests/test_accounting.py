import json
import tempfile
import unittest
from pathlib import Path

from renewal.accounting import LedgerCostSummary, create
from renewal.experiment import Ledger
from renewal.nightly import build_configured_runner


class AccountingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'calls.jsonl'
        self.ledger = Ledger(self.path)

    def call(self, status='completed', **values):
        key = self.ledger.begin({'harness':'synthetic', 'phase':'pilot'})
        if status != 'running':
            self.ledger.finish(key, status=status, **values)
        return key

    def test_failures_and_interrupted_calls_count_and_unknown_cost_is_not_zero(self):
        self.call('failed', usage={'input_tokens':12})
        self.call('running')
        summary = LedgerCostSummary([self.path])()
        self.assertEqual(summary['paid_calls_recorded'],2)
        self.assertEqual(summary['failed_calls'],1)
        self.assertEqual(summary['unfinished_calls'],1)
        self.assertIsNone(summary['actual_charges'])
        self.assertIsNone(summary['pricing_estimate'])
        self.assertEqual(summary['unknown_actual_charge_calls'],2)
        self.assertEqual(summary['usage_by_harness']['synthetic']['input_tokens']['known_subtotal'],12)
        self.assertEqual(summary['usage_by_harness']['synthetic']['input_tokens']['unknown_calls'],1)

    def test_actual_estimated_and_provider_reported_values_stay_separate(self):
        self.call(actual_charge_usd=.1, actual_charge_source='synthetic invoice',
                  actual_charge_date='2026-11-15', estimated_usd=.2,
                  pricing_source='synthetic pricing', pricing_date='2026-11-15',
                  provider_reported_cost_usd=9)
        self.call('failed', actual_charge_usd=.3, actual_charge_source='synthetic invoice',
                  actual_charge_date='2026-11-15', estimated_usd=.4,
                  pricing_source='synthetic pricing', pricing_date='2026-11-15')
        summary = LedgerCostSummary([self.path])()
        self.assertAlmostEqual(summary['actual_charges'],.4)
        self.assertAlmostEqual(summary['pricing_estimate'],.6)
        self.assertEqual(summary['provider_reported_known_subtotal'],9)
        self.assertEqual(summary['currency'],'USD')

    def test_unattributed_invalid_or_nonfinite_costs_are_unknown(self):
        for value in (.1, float('nan'), float('inf'), -1, True, '1'):
            self.call(actual_charge_usd=value, estimated_usd=value)
        summary = LedgerCostSummary([self.path])()
        self.assertIsNone(summary['actual_charges'])
        self.assertIsNone(summary['pricing_estimate'])
        self.assertEqual(summary['unknown_actual_charge_calls'],6)

    def test_missing_corrupt_and_empty_ledgers_do_not_claim_zero_spend(self):
        summary = LedgerCostSummary([self.path])()
        self.assertEqual(summary['status'],'incomplete_ledgers')
        self.assertIsNone(summary['actual_charges'])
        self.path.write_text('')
        self.assertEqual(LedgerCostSummary([self.path])()['status'],'no_recorded_calls')
        self.path.write_text('{broken')
        self.assertEqual(LedgerCostSummary([self.path])()['status'],'unavailable')

    def test_duplicate_copied_ledgers_count_once_but_conflicts_fail_closed(self):
        self.call()
        other = self.root / 'copy.jsonl'
        other.write_bytes(self.path.read_bytes())
        self.assertEqual(LedgerCostSummary([self.path,other])()['paid_calls_recorded'],1)
        rows = other.read_text().splitlines()
        event = json.loads(rows[-1])
        event['status']='failed'
        rows[-1]=json.dumps(event)
        other.write_text('\n'.join(rows)+'\n')
        self.assertEqual(LedgerCostSummary([self.path,other])()['status'],'unavailable')

    def test_nightly_configuration_connects_explicit_private_ledgers(self):
        self.call('running')
        config={'state_dir':str(self.root/'state'),
                'accounting':{'ledger_paths':[str(self.path)]}}
        self.assertEqual(create(config)()['paid_calls_recorded'],1)
        runner=build_configured_runner(config)
        self.assertEqual(runner.cost_summary()['unfinished_calls'],1)
        self.assertIsNone(runner.sink)

    def test_requires_explicit_absolute_dedicated_ledger_paths(self):
        for paths in ([], ['relative.jsonl'], [self.root]):
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                LedgerCostSummary(paths)

    def test_usage_with_different_provider_semantics_is_not_combined(self):
        self.call(usage={'input_tokens':100,'cached_input_tokens':50})
        self.call(harness='synthetic-other',usage={'input_tokens':80,'cache_read_input_tokens':20})
        summary=LedgerCostSummary([self.path])()
        self.assertNotIn('usage',summary)
        self.assertEqual(summary['usage_by_harness']['synthetic']['input_tokens']['known_subtotal'],100)
        self.assertEqual(summary['usage_by_harness']['synthetic-other']['input_tokens']['known_subtotal'],80)


if __name__ == '__main__':
    unittest.main()
