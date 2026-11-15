"""Read-only pilot cost aggregation from explicitly selected private ledgers.

Use dedicated pilot ledgers, including evals, repairs and maintenance. This cannot
prove that a caller recorded every paid call. Missing evidence stays unknown.
Provider-reported costs never become invoices or subscription consumption.
"""
import datetime as dt
import fcntl
import math
from pathlib import Path

from .experiment import Ledger


def number(value):
    return (type(value) is int and value >= 0
            or type(value) is float and math.isfinite(value) and value >= 0)


def attributed(row, field, source, date):
    try:
        dt.date.fromisoformat(row[date])
        return number(row.get(field)) and isinstance(row.get(source), str) and bool(row[source].strip())
    except (KeyError, TypeError, ValueError):
        return False


class LedgerCostSummary:
    def __init__(self, ledger_paths):
        self.paths = list(dict.fromkeys(Path(path) for path in ledger_paths))
        if not self.paths or any(not path.is_absolute() or path.suffix != '.jsonl' for path in self.paths):
            raise ValueError('Explicit absolute dedicated JSONL ledger paths required')

    def __call__(self):
        summary = dict(status='unavailable', currency='USD', paid_calls_recorded=None,
                       actual_charges=None, pricing_estimate=None,
                       coverage='Explicit ledgers only; unrecorded calls and outer orchestration are not measured.')
        rows, missing = {}, 0
        try:
            for path in self.paths:
                if not path.exists():
                    missing += 1
                    continue
                with path.open() as handle:
                    # Coordinate with Ledger.event so a concurrent append cannot
                    # turn a partial JSON line into a fabricated complete total.
                    fcntl.flock(handle, fcntl.LOCK_SH)
                    for row in Ledger(path).rows():
                        key = row['id']
                        if not isinstance(key, str) or not key or not number(row.get('started_monotonic')):
                            raise ValueError('Unattributed ledger event')
                        if key in rows and rows[key] != row:
                            raise ValueError('Conflicting duplicate call')
                        rows[key] = row
        except (OSError, ValueError, KeyError, TypeError):
            return summary
        values = list(rows.values())
        count = len(values)
        summary.update(status='incomplete_ledgers' if missing else ('recorded' if count else 'no_recorded_calls'),
                       paid_calls_recorded=count, missing_ledgers=missing,
                       failed_calls=sum(r.get('status') in {'failed','blocked','timeout'} for r in values),
                       unfinished_calls=sum(r.get('status') not in {'failed','blocked','timeout','completed'} for r in values))
        for field, output, source, date in (
            ('actual_charge_usd','actual_charges','actual_charge_source','actual_charge_date'),
            ('estimated_usd','pricing_estimate','pricing_source','pricing_date'),
        ):
            known = [r for r in values if attributed(r, field, source, date)]
            summary[output] = sum(r[field] for r in known) if count and not missing and len(known) == count else None
            prefix = 'actual_charge' if output == 'actual_charges' else 'estimate'
            summary['known_'+prefix+'_subtotal'] = sum(r[field] for r in known)
            summary['unknown_'+prefix+'_calls'] = count - len(known)
            summary[prefix+'_evidence'] = sorted({(r[source],r[date]) for r in known})
        summary['provider_reported_known_subtotal'] = sum(r['provider_reported_cost_usd'] for r in values
            if number(r.get('provider_reported_cost_usd')))
        summary['usage_by_harness'] = {}
        # Keep provider fields separate. Claude uncached input and Codex total
        # input have different semantics; do not invent a normalized token total.
        groups = {}
        for row in values:
            harness = row.get('harness')
            harness = harness if isinstance(harness,str) and harness else 'unreported'
            groups.setdefault(harness,[]).append(row)
        for harness, calls in groups.items():
            usage = summary['usage_by_harness'][harness] = {}
            for field in ('input_tokens','output_tokens','cached_input_tokens',
                          'cache_read_input_tokens','cache_creation_input_tokens','reasoning_output_tokens'):
                exposed = [r['usage'][field] for r in calls if isinstance(r.get('usage'),dict)
                           and number(r['usage'].get(field))]
                usage[field] = dict(known_subtotal=sum(exposed), unknown_calls=len(calls)-len(exposed))
        return summary


def create(config):
    return LedgerCostSummary(config['accounting']['ledger_paths'])
