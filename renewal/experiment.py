"""Protected acceptance and honest matched-run accounting."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import time
import uuid
from pathlib import Path


CONDITIONS = ('current-local','clean-local','clean-notion')
MATCH = ('task','repeat','cache','model','effort','permissions','fixture_hash','tools_hash','harness_version')


def validate_manifest(folder, freeze):
    for name, expected in json.loads(Path(freeze).read_text()).items():
        path = Path(folder)/name
        if not path.resolve().is_relative_to(Path(folder).resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Acceptance manifest changed: '+name)


def accepted(check, root, result):
    kind = check['kind']
    if kind == 'report':
        root = Path(root).resolve()
        path = root / check['location']
        if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
            return False
        text = path.read_text()
        # Sections require their own body; following headings are not content.
        sections = check['required_sections']
        bodies = {name:[] for name in sections}
        current = None
        for line in text.splitlines():
            normalized = line.strip().strip('#*: ').strip()
            if normalized in bodies:
                current = normalized
            elif current:
                bodies[current].append(line)
        return all(''.join(bodies[name]).strip() for name in sections)
    if kind == 'playbook':
        return bool(check['tests']) and all(result.get('tests',{}).get(name) is True for name in check['tests'])
    if kind == 'investigation':
        return result.get('root_cause') == check['root_cause'] and all(result.get(k) for k in ('evidence','reproduction','next_action'))
    raise ValueError('Unknown acceptance type')


class Ledger:
    """Append-only events, including starts without finishes and failed attempts."""
    def __init__(self, path):
        self.path=Path(path)
    def event(self, value):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.path.open('a',encoding='utf-8') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            handle.write(json.dumps(dict(value,recorded_at=dt.datetime.now(dt.timezone.utc).isoformat()))+'\n')
            handle.flush()
            import os
            os.fsync(handle.fileno())
    def begin(self, metadata):
        key=uuid.uuid4().hex
        self.event(dict(metadata,id=key,status='running',started_monotonic=time.monotonic(),
                        usage={},actual_charge_usd=None,estimated_usd=None))
        return key
    def finish(self,key,**result):
        self.event(dict(result,id=key))
    def rows(self):
        values={}
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                row=json.loads(line)
                values.setdefault(row['id'],{}).update(row)
        return list(values.values())


def compare(rows):
    groups={}
    summary={}
    for condition in CONDITIONS:
        selected=[r for r in rows if r.get('condition')==condition]
        passing=[r for r in selected if r.get('status')=='completed' and r.get('accepted') is True]
        summary[condition]={'attempts':len(selected),'accepted':len(passing),
                            'completion_rate':len(passing)/len(selected) if selected else None,
                            'total_estimated_usd':sum(r['estimated_usd'] for r in selected) if selected and all(r.get('estimated_usd') is not None for r in selected) else None,
                            'known_estimate_subtotal_usd':sum(r['estimated_usd'] for r in selected if r.get('estimated_usd') is not None),
                            'unknown_estimated_attempts':sum(r.get('estimated_usd') is None for r in selected),
                            'actual_charge_usd':None if any(r.get('actual_charge_usd') is None for r in selected) or not selected else sum(r['actual_charge_usd'] for r in selected)}
    for row in rows:
        if row.get('condition') not in CONDITIONS or row.get('status')!='completed' or row.get('accepted') is not True:
            continue
        if any(row.get(k) is None for k in MATCH):
            continue
        if row.get('cache_verified') is not True or row.get('observed_model') != row['model']:
            continue
        key=tuple(row[k] for k in MATCH)
        group=groups.setdefault(key,{})
        condition=row['condition']
        if condition in group:
            raise ValueError('Duplicate matched run; use distinct repeat identifiers')
        group[condition]=row
    matched=[g for g in groups.values() if set(g)==set(CONDITIONS)]
    costs={c:sum(g[c]['estimated_usd'] for g in matched) for c in CONDITIONS} if matched and all(g[c].get('estimated_usd') is not None for g in matched for c in CONDITIONS) else None
    return {'schema':1,'conditions':summary,'matched_groups':len(matched),'matched_estimated_usd':costs,
            'adopt_notion':False,'adoption_status':'requires reliability acceptance and actual matched benchmark review',
            'warning':'Price estimates are not invoices or subscription consumption. Missing costs are unknown.'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('ledger',type=Path)
    args=parser.parse_args()
    print(json.dumps(compare(Ledger(args.ledger).rows()),indent=2))


if __name__=='__main__':
    main()
