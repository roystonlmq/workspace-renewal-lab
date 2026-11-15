"""Bounded local scheduler runtime. No model calls or implicit remote writes."""
from __future__ import annotations

import argparse
import fcntl
import importlib
import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

if __package__:
    from .ingestion import Ingestor, atomic_json, load_json
else:  # Direct executable entrypoint used by launchd, independent of cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from renewal.ingestion import Ingestor, atomic_json, load_json

# Metadata callbacks must share Activity's identity under both executable forms.
if __name__ == '__main__':
    sys.modules['renewal.nightly'] = sys.modules[__name__]


SGT = ZoneInfo('Asia/Singapore')


@dataclass(frozen=True)
class Activity:
    verified: bool = False
    idle: bool = False
    active_sessions: frozenset[str] = field(default_factory=frozenset)


class LocalActivityCheck:
    """Read-only conservative macOS check. Never collect process arguments.

    Any Claude/Codex process, including desktop apps and this implementation
    session, defers work. Unsupported/denied checks return unverified, not idle.
    """
    def __init__(self, *, idle_seconds=600, command_runner=subprocess.run):
        if idle_seconds < 300:
            raise ValueError('idle window must be at least five minutes')
        self.idle_seconds = idle_seconds
        self.command_runner = command_runner

    def __call__(self):
        if sys.platform != 'darwin':
            return Activity()
        try:
            idle = self.command_runner(['/usr/sbin/ioreg', '-c', 'IOHIDSystem'],
                capture_output=True, text=True, timeout=10)
            processes = self.command_runner(['/bin/ps', '-axo', 'comm='],
                capture_output=True, text=True, timeout=10)
            if idle.returncode or processes.returncode:
                return Activity()
            matches = re.findall(r'HIDIdleTime[\"\s]*=\s*(\d+)', idle.stdout)
            if not matches:
                return Activity()
            idle_for = min(int(value) for value in matches) / 1_000_000_000
            names = {Path(line.strip()).name.lower() for line in processes.stdout.splitlines()}
            active = any(name in {'claude', 'codex', 'claude-code', 'codex-cli'} for name in names)
            return Activity(verified=True, idle=idle_for >= self.idle_seconds and not active)
        except (OSError, subprocess.SubprocessError, ValueError):
            return Activity()


class NightlyRunner:
    """Inject verified storage, activity detection and a protected improvement pipeline.

    The improvement hook must own protected tests, paid-call ledger reservations,
    bounded retries, application and rollback. No generation is implemented here.
    Its daily attempt is durably reserved before invoking it, including crashes.
    """
    def __init__(self, state_dir, ingestor: Ingestor, sink, activity_check, *,
                 improvement=None, memory_maintenance=None, report_sink=None, readiness=None, cost_summary=None,
                 policy_expires_at=None, observation_report_due_at=None):
        self.state_dir = Path(state_dir)
        self.ingestor = ingestor
        self.sink = sink
        self.activity_check = activity_check
        self.improvement = improvement
        self.memory_maintenance = memory_maintenance
        self.report_sink = report_sink
        self.readiness = readiness or {}
        self.cost_summary = cost_summary
        self.policy_expires_at = policy_expires_at
        self.observation_report_due_at = observation_report_due_at

    def tick(self, paths, *, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError('aware time required')
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.state_dir / 'nightly.lock'
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {'status': 'locked', 'processed_sessions': 0}
            try:
                return self._tick(paths, now)
            except (OSError, ValueError, TypeError, KeyError):
                # Corrupt/unsupported operational state must never reset quotas.
                return {'status': 'blocked_operational_state', 'processed_sessions': 0}

    def _tick(self, paths, now):
        result = dict(processed_sessions=0, pilot_status='not_started',
                      report_status='not_due', improvement_status='not_attempted',
                      maintenance_status='not_attempted', blockers=[])
        if getattr(self.sink, 'verified_private', False) is not True:
            return dict(result, status='blocked_private_sink')
        try:
            activity = self.activity_check()
        except Exception:
            activity = Activity()
        if not isinstance(activity, Activity) or activity.verified is not True:
            return dict(result, status='blocked_active_work_check')
        if activity.idle is not True:
            return dict(result, status='deferred_active_work')
        if (self.readiness.get('ingestion_tests') is not True or self.readiness.get('instrumentation') is not True
                or self.readiness.get('operational_bindings', True) is not True):
            return dict(result, status='blocked_readiness')
        partial = self.observation_report_due_at is not None
        pilot_path = self.state_dir / ('observation.json' if partial else 'pilot.json')
        other_path = self.state_dir / ('pilot.json' if partial else 'observation.json')
        if other_path.exists():
            return dict(result, status='blocked_measurement_mode_change')
        pilot = load_json(pilot_path)
        local = now.astimezone(SGT)
        day_key = local.date().isoformat()
        if local.hour < 2:
            yesterday = (local.date() - timedelta(days=1)).isoformat()
            if not pilot or yesterday in pilot.get('days', {}):
                return dict(result, status='not_due')
        if not pilot:
            due = datetime.fromisoformat(self.observation_report_due_at) if partial else now + timedelta(days=14)
            if (due.tzinfo is None or due <= now or due > now + timedelta(days=14)
                    or (partial and not self.policy_expires_at)
                    or (self.policy_expires_at and due >= datetime.fromisoformat(self.policy_expires_at))):
                return dict(result, status='blocked_policy_window', blockers=['policy_expires_before_full_report'])
            pilot = dict(version=1, started_at=now.isoformat(),
                         report_due_at=due.isoformat(),
                         measurement_kind='partial_observation' if partial else 'full_pilot',
                         status='running_partial', days={}, report_delivered_at=None)
            atomic_json(pilot_path, pilot)
        if pilot.get('version') != 1 or not isinstance(pilot.get('days'), dict):
            raise ValueError('unsupported pilot state')
        result['pilot_status'] = pilot['status']
        result['report_due_at'] = pilot['report_due_at']
        due = datetime.fromisoformat(pilot['report_due_at'])
        if pilot['status'] == 'completed':
            return dict(result, status='pilot_completed', report_status='delivered')
        if now >= due:
            if self.report_sink is None:
                result['report_status'] = 'blocked_report_sink'
            elif getattr(self.report_sink, 'verified_private', False) is not True:
                result['report_status'] = 'blocked_report_privacy'
            elif day_key in pilot.get('report_attempt_days', {}):
                result['report_status'] = 'deferred_daily_report_retry'
            else:
                attempts = pilot.setdefault('report_attempt_days', {})
                attempts[day_key] = 'reserved'
                atomic_json(pilot_path, pilot)
                report = self._report(pilot, now)
                try:
                    delivered = self.report_sink(report) is True
                except Exception:
                    delivered = False
                result['report_status'] = 'delivered' if delivered else 'write_failed'
                attempts[day_key] = result['report_status']
                if delivered:
                    pilot['report_delivered_at'] = now.isoformat()
                    pilot['status'] = 'completed'
                    result['pilot_status'] = 'completed'
            atomic_json(pilot_path, pilot)
            return dict(result, status='report_due')
        fully_configured = self.improvement is not None and self.memory_maintenance is not None
        pilot['status'] = 'observing_partial' if partial else ('running' if fully_configured else 'running_partial')
        result['pilot_status'] = pilot['status']
        if self.memory_maintenance is None:
            result['blockers'].append('blocked_maintenance')
        if self.improvement is None:
            result['blockers'].append('blocked_improvement_generation')
        day = pilot['days'].setdefault(day_key, dict(attempted_sessions=[],
            processed_sessions=0, batches=0, failures=0, improvement_attempted=False,
            improvement_status='not_attempted', backlog=False, observed_at=now.isoformat(),
            maintenance_attempted=False, maintenance_status='not_attempted', fully_configured=fully_configured))
        if not day.get('maintenance_attempted', False):
            if self.memory_maintenance is None:
                day['maintenance_status'] = 'blocked_maintenance'
            else:
                day['maintenance_attempted'] = True
                day['maintenance_status'] = 'reserved'
                atomic_json(pilot_path, pilot)
                try:
                    outcome = self.memory_maintenance()
                    status = outcome.get('status') if isinstance(outcome, dict) else None
                    allowed = {'completed', 'no_change', 'blocked', 'failed'}
                    day['maintenance_status'] = status if status in allowed else 'invalid_result'
                except Exception:
                    day['maintenance_status'] = 'failed'
            atomic_json(pilot_path, pilot)
        def reserve(session_id):
            if session_id in day['attempted_sessions'] or len(day['attempted_sessions']) >= 20:
                return False
            day['attempted_sessions'].append(session_id)
            atomic_json(pilot_path, pilot)
            return True
        remaining = 20 - len(day['attempted_sessions'])
        if remaining:
            ingested = self.ingestor.process(paths, self.sink, now=now,
                active_sessions=set(activity.active_sessions), limit=remaining, on_session=reserve)
            result['processed_sessions'] = ingested['processed_sessions']
            result['ingestion'] = ingested
            day['processed_sessions'] += ingested['processed_sessions']
            day['batches'] += ingested['batches']
            day['failures'] += len(ingested['failures'])
            day['backlog'] = ingested['backlog']
            atomic_json(pilot_path, pilot)
        if not day['improvement_attempted']:
            if self.improvement is None:
                day['improvement_status'] = 'blocked_unconfigured'
            else:
                day['improvement_attempted'] = True
                day['improvement_status'] = 'reserved'
                atomic_json(pilot_path, pilot)
                try:
                    outcome = self.improvement()
                    status = outcome.get('status') if isinstance(outcome, dict) else None
                    allowed = {'applied', 'rejected', 'rolled_back', 'no_candidate', 'blocked'}
                    day['improvement_status'] = status if status in allowed else 'invalid_result'
                except Exception:
                    day['improvement_status'] = 'failed'
            atomic_json(pilot_path, pilot)
        result['improvement_status'] = day['improvement_status']
        result['maintenance_status'] = day['maintenance_status']
        result['daily_sessions_reserved'] = len(day['attempted_sessions'])
        return dict(result, status='ran')

    def _report(self, pilot, now):
        days = list(pilot['days'].values())
        costs = dict(paid_calls_recorded=None, actual_charges=None,
                     pricing_estimate=None, currency=None, status='not_connected')
        if self.cost_summary is not None:
            try:
                costs = self.cost_summary()
            except Exception:
                costs['status'] = 'unavailable'
        total = costs.get('actual_charges') if isinstance(costs, dict) else None
        statuses = [d['improvement_status'] for d in days]
        return dict(kind='workspace-renewal-pilot-report',
            measurement_kind=pilot.get('measurement_kind', 'full_pilot'),
            idempotency_key='pilot-report:' + pilot['started_at'],
            started_at=pilot['started_at'], due_at=pilot['report_due_at'], generated_at=now.isoformat(),
            observed_days=len(days), measured_14_days=pilot.get('measurement_kind') != 'partial_observation' and len(days) >= 14,
            fully_configured_days=sum(bool(d.get('fully_configured')) for d in days),
            full_measurement_14_days=pilot.get('measurement_kind') != 'partial_observation' and sum(bool(d.get('fully_configured')) for d in days) >= 14,
            completed_outcomes=sum(d['processed_sessions'] for d in days),
            attempted_sessions=sum(len(d['attempted_sessions']) for d in days),
            failed_batches=sum(d['failures'] for d in days),
            useful_changes=statuses.count('applied'), regressions=statuses.count('rolled_back'),
            rollbacks=statuses.count('rolled_back'), backlog_days=sum(bool(d['backlog']) for d in days),
            memory_maintenance_completed=sum(d.get('maintenance_status') in {'completed', 'no_change'} for d in days),
            maintenance_backlog_days=sum(d.get('maintenance_status') not in {'completed', 'no_change'} for d in days),
            improvement_backlog_days=sum(d.get('improvement_status') in {'blocked_unconfigured', 'blocked', 'failed', 'reserved', 'invalid_result'} for d in days),
            costs=costs, average_observed_day_cost=total / len(days) if isinstance(total, (int, float)) and days else None,
            recommended_ongoing_budget=None,
            budget_status='OPEN - requires comparable accepted outcomes and complete cost evidence',
            limitation='Generation is pluggable. Calendar elapsed time is not proof of daily execution.')


def launchd_template(python_executable: str, entrypoint: str, arguments=()) -> bytes:
    """Return a registration template only. Interval permits catch-up while awake.

    The runner owns the 02:00 Singapore due check. Launchd calendar triggers use
    host timezone, so a 300-second interval avoids silently scheduling local 02:00.
    No WakeSystem, privileged daemon, terminal output or OS settings are added.
    """
    if not Path(python_executable).is_absolute() or not Path(entrypoint).is_absolute():
        raise ValueError('absolute executable and entrypoint required')
    return plistlib.dumps(dict(Label='local.workspace-renewal.nightly',
        ProgramArguments=[python_executable, entrypoint, *arguments],
        EnvironmentVariables={'PATH': os.environ.get('PATH', os.defpath)},
        StartInterval=300, RunAtLoad=True, ProcessType='Background',
        ThrottleInterval=300))


def register_scheduler(plist_path: Path, *, command_runner=subprocess.run) -> dict:
    """Register only when explicitly invoked by the operator.

    Callers under a managed sandbox must use its normal reviewed escalation for
    launchctl bootstrap. This helper grants no permission and edits no security.
    """
    if sys.platform != 'darwin':
        return {'status': 'blocked_unsupported_scheduler'}
    plist_path = Path(plist_path).resolve()
    try:
        template = plistlib.loads(plist_path.read_bytes())
        if template['Label'] != 'local.workspace-renewal.nightly':
            raise ValueError('unexpected job')
        domain = f'gui/{os.getuid()}'
        result = command_runner(['/bin/launchctl', 'bootstrap', domain, str(plist_path)],
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            return {'status': 'blocked_scheduler_registration', 'exit_code': result.returncode}
        observed = command_runner(['/bin/launchctl', 'print', domain + '/' + template['Label']],
                                  capture_output=True, text=True, timeout=20)
        return {'status': 'registered' if observed.returncode == 0 else 'registration_unverified',
                'future_execution_verified': False}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return {'status': 'blocked_scheduler_registration'}


def build_configured_runner(config: dict, *, activity_check=None) -> NightlyRunner:
    """Load an explicitly configured trusted connector factory, never credentials.

    Factory(config) supplies sink, report_sink, optional protected improvement and
    cost_summary. Adapters must verify actual access before exposing private=True.
    Configuration flags alone cannot turn on the sink or OS activity gates.
    """
    state_dir = Path(config['state_dir'])
    if not state_dir.is_absolute():
        raise ValueError('absolute operational state directory required')
    connectors = {}
    factory = config.get('connector_factory')
    if factory:
        module, name = factory.split(':', 1)
        connectors = getattr(importlib.import_module(module), name)(config)
        if not isinstance(connectors, dict):
            raise ValueError('invalid connector factory')
    cost_summary = connectors.get('cost_summary')
    if cost_summary is None and config.get('accounting'):
        if __package__:
            from .accounting import create
        else:
            from renewal.accounting import create
        cost_summary = create(config)
    if activity_check is None and config.get('activity_mode') == 'harness_metadata':
        from renewal.activity import probe
        activity_check = probe
    return NightlyRunner(state_dir, Ingestor(state_dir / 'ingestion.json'),
        connectors.get('sink'), activity_check or LocalActivityCheck(),
        improvement=connectors.get('improvement'), report_sink=connectors.get('report_sink'),
        memory_maintenance=connectors.get('memory_maintenance'),
        cost_summary=cost_summary, readiness=config.get('readiness'),
        policy_expires_at=config.get('notion', {}).get('pilot_access', {}).get('expires_at'),
        observation_report_due_at=config.get('observation', {}).get('report_due_at')
            if config.get('observation', {}).get('enabled') is True else None)


def _source_paths(config):
    if 'sources' in config:
        from renewal.runtime import scoped_paths
        yield from scoped_paths(config)
        return
    roots = config.get('source_roots', [str(Path.home() / '.claude' / 'projects'),
                                       str(Path.home() / '.codex' / 'sessions')])
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_absolute():
            raise ValueError('absolute source roots required')
        if root.is_file() and root.suffix == '.jsonl':
            yield root
        elif root.is_dir():
            yield from root.rglob('*.jsonl')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Bounded private nightly runner. No routine terminal output.')
    parser.add_argument('command', choices=['tick', 'prepare-scheduler', 'activate-scheduler'], nargs='?', default='tick')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--plist', type=Path)
    args = parser.parse_args(argv)
    config = load_json(args.config) if args.config else {}
    if args.state_dir:
        config['state_dir'] = str(args.state_dir.resolve())
    if 'state_dir' not in config:
        parser.error('a state directory in config or --state-dir is required')
    state_dir = Path(config['state_dir'])
    if not state_dir.is_absolute():
        parser.error('operational state directory must be absolute')
    if args.command == 'prepare-scheduler':
        if args.config is None or args.plist is None:
            parser.error('prepare-scheduler requires --config and --plist')
        # Generating a template does not instantiate remote connectors or enable a job.
        data = launchd_template(sys.executable, str(Path(__file__).resolve()),
                                ['tick', '--config', str(args.config.resolve())])
        args.plist.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(args.plist, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        atomic_json(state_dir / 'scheduler.json', dict(status='template_only', enabled=False,
                    future_execution_verified=False))
        return 0
    try:
        runner = build_configured_runner(config)
    except Exception:
        atomic_json(state_dir / 'last-run.json', dict(status='blocked_connector_initialization', processed_sessions=0))
        return 2
    if args.command == 'activate-scheduler':
        if args.plist is None:
            parser.error('activate-scheduler requires --plist')
        activity = runner.activity_check()
        if (getattr(runner.sink, 'verified_private', False) is not True or
            not activity.verified or runner.readiness.get('ingestion_tests') is not True or
            runner.readiness.get('instrumentation') is not True):
            result = dict(status='blocked_scheduler_prerequisites', enabled=False,
                          future_execution_verified=False)
        else:
            result = register_scheduler(args.plist)
            result['enabled'] = result['status'] == 'registered'
        atomic_json(state_dir / 'scheduler.json', result)
        return 0 if result['enabled'] else 2
    invoked_at = datetime.now(timezone.utc).isoformat()
    result = runner.tick(_source_paths(config))
    result.update(invoked_at=invoked_at, finished_at=datetime.now(timezone.utc).isoformat())
    atomic_json(state_dir / 'last-run.json', result)
    return 0 if result['status'] in {'ran', 'not_due', 'report_due', 'pilot_completed', 'locked', 'deferred_active_work'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
