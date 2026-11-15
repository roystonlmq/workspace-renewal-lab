import json
import fcntl
import plistlib
import tempfile
import unittest
import subprocess
import sys
import types
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path

from renewal.ingestion import Ingestor
from renewal.accounting import LedgerCostSummary
from renewal.experiment import Ledger
from renewal.nightly import Activity, LocalActivityCheck, NightlyRunner, build_configured_runner, launchd_template, register_scheduler
from test_ingestion import NOW, claude, write_lines


class PrivateSink:
    verified_private = True
    def __init__(self):
        self.receipts = []
    def __call__(self, batch):
        self.receipts.append(batch.idempotency_key)
        return True


class PrivateReport:
    verified_private = True
    def __init__(self, callback):
        self.callback = callback
    def __call__(self, report):
        return self.callback(report)


class NightlyTests(unittest.TestCase):
    def test_unfinished_operational_bindings_prevent_observation_start(self):
        runner = self.runner(observation_report_due_at=(NOW + timedelta(days=2)).isoformat(),
            policy_expires_at=(NOW + timedelta(days=3)).isoformat())
        runner.readiness['operational_bindings'] = False
        self.assertEqual(runner.tick([], now=NOW)['status'], 'blocked_readiness')
        self.assertFalse((self.root/'runtime/observation.json').exists())

    def test_direct_scheduler_entrypoint_accepts_metadata_activity_type(self):
        import os
        factory=self.root/'direct_factory.py'
        factory.write_text('from renewal.nightly import Activity\n'
            'import renewal.activity\n'
            'class Sink:\n    verified_private=True\n'
            'def create(config):\n'
            '    renewal.activity.probe=lambda:Activity(verified=True,idle=False)\n'
            '    return dict(sink=Sink())\n')
        config=self.root/'config.json'
        config.write_text(json.dumps(dict(state_dir=str(self.root/'state'),source_roots=[],
            connector_factory='direct_factory:create',activity_mode='harness_metadata')))
        entry=Path(__file__).resolve().parents[1]/'renewal/nightly.py'
        for executable in ([sys.executable,str(entry)], [sys.executable,'-m','renewal.nightly']):
            with self.subTest(executable=executable):
                result=subprocess.run([*executable,'tick','--config',str(config)],
                    capture_output=True,text=True,cwd=entry.parents[1],
                    env=dict(os.environ,PYTHONPATH=os.pathsep.join([str(self.root),str(entry.parents[1])])))
                self.assertEqual(result.returncode,0,result.stderr)
                row=json.loads((self.root/'state/last-run.json').read_text())
                self.assertEqual(row['status'],'deferred_active_work')
                self.assertIn('invoked_at',row)

    def test_policy_window_must_cover_due_report_before_start(self):
        runner = self.runner(policy_expires_at=(NOW + timedelta(days=14)).isoformat())
        result = runner.tick([], now=NOW)
        self.assertEqual(result['status'], 'blocked_policy_window')
        self.assertFalse((self.root/'runtime/pilot.json').exists())

    def test_explicit_partial_observation_has_separate_state_and_report(self):
        reports = []
        due = NOW + timedelta(days=2)
        runner = self.runner(policy_expires_at=(due + timedelta(hours=1)).isoformat(),
            observation_report_due_at=due.isoformat(),
            memory_maintenance=lambda: {'status': 'no_change'},
            improvement=lambda: {'status': 'no_candidate'},
            report_sink=PrivateReport(lambda r: reports.append(r) or True))
        result = runner.tick(self.paths[:1], now=NOW)
        self.assertEqual(result['pilot_status'], 'observing_partial')
        self.assertEqual(result['processed_sessions'], 1)
        self.assertFalse((self.root/'runtime/pilot.json').exists())
        self.assertTrue((self.root/'runtime/observation.json').exists())
        self.assertEqual(runner.tick([], now=due)['report_status'], 'delivered')
        self.assertEqual(reports[0]['measurement_kind'], 'partial_observation')
        self.assertFalse(reports[0]['full_measurement_14_days'])

    def test_partial_observation_cannot_outlive_policy(self):
        due = NOW + timedelta(days=2)
        runner = self.runner(policy_expires_at=due.isoformat(),
            observation_report_due_at=due.isoformat())
        self.assertEqual(runner.tick([], now=NOW)['status'], 'blocked_policy_window')
        self.assertFalse((self.root/'runtime/observation.json').exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sink = PrivateSink()
        self.paths = []
        for number in range(25):
            path = self.root / f'{number}.jsonl'
            write_lines(path, [dict(claude(str(number)), sessionId=f'session-{number}')])
            self.paths.append(path)
        self.activity = lambda: Activity(verified=True, idle=True)

    def runner(self, **kwargs):
        return NightlyRunner(self.root / 'runtime', Ingestor(self.root / 'ingest.json'),
                             self.sink, self.activity,
                             readiness={'ingestion_tests': True, 'instrumentation': True}, **kwargs)

    def test_day_cap_persists_and_pilot_report_retries_without_claiming_delivery(self):
        reports = []
        runner = self.runner(report_sink=PrivateReport(lambda r: reports.append(r) or False))
        first = runner.tick(self.paths, now=NOW)
        self.assertEqual(first['processed_sessions'], 20)
        self.assertEqual(first['pilot_status'], 'running_partial')
        self.assertIn('blocked_maintenance', first['blockers'])
        again = self.runner().tick(self.paths, now=NOW + timedelta(minutes=20))
        self.assertEqual(again['processed_sessions'], 0)
        self.assertEqual(len(self.sink.receipts), 20)
        due = NOW + timedelta(days=14)
        result = runner.tick(self.paths, now=due)
        self.assertEqual(result['report_status'], 'write_failed')
        self.assertFalse(reports[0]['measured_14_days'])
        result = self.runner(report_sink=PrivateReport(lambda r: True)).tick(self.paths, now=due)
        self.assertEqual(result['report_status'], 'deferred_daily_report_retry')
        result = self.runner(report_sink=PrivateReport(lambda r: True)).tick(self.paths, now=due + timedelta(days=1))
        self.assertEqual(result['report_status'], 'delivered')
        self.assertEqual(result['pilot_status'], 'completed')

    def test_closed_gates_and_busy_lock_do_not_start_pilot(self):
        runner = self.runner()
        self.sink.verified_private = False
        self.assertEqual(runner.tick(self.paths, now=NOW)['status'], 'blocked_private_sink')
        self.sink.verified_private = True
        runner.activity_check = lambda: Activity(verified=False, idle=True)
        self.assertEqual(runner.tick(self.paths, now=NOW)['status'], 'blocked_active_work_check')
        runner.activity_check = lambda: Activity(verified=True, idle=False)
        self.assertEqual(runner.tick(self.paths, now=NOW)['status'], 'deferred_active_work')
        self.assertFalse((self.root / 'runtime' / 'pilot.json').exists())

    def test_lock_and_unverified_report_sink_fail_closed(self):
        runtime = self.root / 'runtime'
        runtime.mkdir()
        with (runtime / 'nightly.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.runner().tick(self.paths, now=NOW)['status'], 'locked')
        self.runner().tick(self.paths[:1], now=NOW)
        result = self.runner(report_sink=lambda r: True).tick([], now=NOW + timedelta(days=14))
        self.assertEqual(result['report_status'], 'blocked_report_privacy')

    def test_instrumentation_readiness_is_required_to_start_pilot(self):
        runner = self.runner()
        runner.readiness = {}
        self.assertEqual(runner.tick(self.paths, now=NOW)['status'], 'blocked_readiness')
        self.assertFalse((self.root / 'runtime' / 'pilot.json').exists())

    def test_due_report_includes_failed_and_unfinished_calls_with_unknown_billing(self):
        ledger=Ledger(self.root/'pilot-calls.jsonl')
        key=ledger.begin({'harness':'synthetic'})
        ledger.finish(key,status='failed',usage={'input_tokens':12})
        ledger.begin({'harness':'synthetic'})
        reports=[]
        runner=self.runner(cost_summary=LedgerCostSummary([ledger.path]),
                           report_sink=PrivateReport(lambda r: reports.append(r) or True))
        runner.tick([],now=NOW)
        self.assertEqual(runner.tick([],now=NOW+timedelta(days=14))['report_status'],'delivered')
        self.assertEqual(reports[0]['costs']['paid_calls_recorded'],2)
        self.assertEqual(reports[0]['costs']['unfinished_calls'],1)
        self.assertIsNone(reports[0]['costs']['actual_charges'])
        self.assertIsNone(reports[0]['average_observed_day_cost'])

    def test_improvement_attempt_is_reserved_before_crash_and_never_retried_same_day(self):
        attempts = []
        def improvement():
            attempts.append(1)
            raise RuntimeError('synthetic crash')
        result = self.runner(improvement=improvement).tick(self.paths[:1], now=NOW)
        self.assertEqual(result['improvement_status'], 'failed')
        self.runner(improvement=improvement).tick(self.paths[:1], now=NOW + timedelta(minutes=1))
        self.assertEqual(attempts, [1])

    def test_before_two_am_defers_but_missed_run_catches_up_when_idle(self):
        before = NOW.replace(hour=17)  # 01:00 Singapore next day.
        result = self.runner().tick(self.paths, now=before)
        self.assertEqual(result['status'], 'not_due')
        result = self.runner().tick(self.paths[:1], now=NOW)
        self.assertEqual(result['processed_sessions'], 1)

    def test_scheduler_template_checks_often_and_rejects_relative_executable(self):
        data = plistlib.loads(launchd_template('/usr/bin/python3', '/tmp/synthetic/run.py'))
        self.assertEqual(data['StartInterval'], 300)
        self.assertFalse(data.get('WakeSystem', False))
        with self.assertRaises(ValueError):
            launchd_template('python3', '/tmp/synthetic/run.py')

    def test_activity_detector_requires_idle_and_absence_of_both_harnesses(self):
        outputs = iter(['HIDIdleTime = 900000000000', '/usr/bin/login\n'])
        def command(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, next(outputs), '')
        with patch('renewal.nightly.sys.platform', 'darwin'):
            self.assertEqual(LocalActivityCheck(command_runner=command)(), Activity(verified=True, idle=True))
        outputs = iter(['HIDIdleTime = 900000000000', '/Applications/Codex.app/Contents/MacOS/Codex\n'])
        with patch('renewal.nightly.sys.platform', 'darwin'):
            self.assertFalse(LocalActivityCheck(command_runner=command)().idle)
        def denied(args, **kwargs):
            return subprocess.CompletedProcess(args, 1, '', 'denied')
        self.assertFalse(LocalActivityCheck(command_runner=denied)().verified)

    def test_scheduler_registration_checks_command_success(self):
        plist = self.root / 'nightly.plist'
        plist.write_bytes(launchd_template('/usr/bin/python3', '/tmp/synthetic/run.py'))
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')
        with patch('renewal.nightly.sys.platform', 'darwin'):
            result = register_scheduler(plist, command_runner=command)
        self.assertEqual(result['status'], 'registered')
        self.assertEqual(calls[0][:2], ['/bin/launchctl', 'bootstrap'])
        self.assertEqual(calls[1][:2], ['/bin/launchctl', 'print'])

    def test_configured_runtime_uses_connector_and_real_ingestion_end_to_end(self):
        module = types.ModuleType('synthetic_connectors')
        module.create = lambda config: dict(sink=self.sink, report_sink=PrivateReport(lambda r: True))
        config = dict(state_dir=str(self.root / 'configured'),
                      connector_factory='synthetic_connectors:create',
                      readiness={'ingestion_tests': True, 'instrumentation': True})
        with patch.dict(sys.modules, {'synthetic_connectors': module}):
            runner = build_configured_runner(config, activity_check=self.activity)
            result = runner.tick(self.paths[:1], now=NOW)
        self.assertEqual(result['processed_sessions'], 1)
        self.assertEqual(len(self.sink.receipts), 1)
        state = json.loads((self.root / 'configured' / 'ingestion.json').read_text())
        self.assertEqual(len(state['segments']), 1)

    def test_maintenance_is_reserved_once_and_failed_attempt_waits_until_next_day(self):
        calls = []
        def maintenance():
            calls.append(1)
            raise OSError('synthetic unavailable store')
        first = self.runner(memory_maintenance=maintenance).tick(self.paths[:1], now=NOW)
        self.assertEqual(first['maintenance_status'], 'failed')
        self.runner(memory_maintenance=maintenance).tick(self.paths[:1], now=NOW + timedelta(minutes=10))
        self.assertEqual(calls, [1])
        self.runner(memory_maintenance=maintenance).tick(self.paths[:1], now=NOW + timedelta(days=1))
        self.assertEqual(calls, [1, 1])

    def test_full_pilot_status_requires_maintenance_and_protected_improvement_pipeline(self):
        runner = self.runner(memory_maintenance=lambda: {'status': 'completed'},
                             improvement=lambda: {'status': 'no_candidate'})
        first = runner.tick(self.paths[:1], now=NOW)
        self.assertEqual(first['pilot_status'], 'running')
        self.assertEqual(first['maintenance_status'], 'completed')
        self.assertEqual(first['blockers'], [])


if __name__ == '__main__':
    unittest.main()
