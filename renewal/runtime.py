"""Trusted internal pilot wiring. Only reviewed facts leave a source session.

Source receipts pin original bytes. Changed, unknown and mixed inputs require
another scope review and stay quarantined, independently of eligible sources.
No transcript or unrestricted model output is copied into remote memory.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .ingestion import atomic_json, digest
from .notion import NotionStore, NtnTransport, FindingSink, ReportSink
from .controls import ChangeGate
from .improvement import ImprovementPipeline
from .experiment import Ledger


def scoped_paths(config):
    for source in config.get('sources', []):
        path = Path(source['path'])
        if (path.is_absolute() and path.suffix == '.jsonl' and not path.is_symlink()
                and path.is_file() and path.stat().st_size <= 64*1024*1024
                and hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256']):
            yield path


def assistant_text(record):
    if record.get('type') == 'response_item':
        message = record.get('payload', {})
        if message.get('type') != 'message':
            return ''
    elif record.get('type') == 'assistant':
        message = record.get('message', {})
    else:
        return ''
    if message.get('role', 'assistant') != 'assistant':
        return ''
    content = message.get('content', [])
    if not isinstance(content, list):
        return ''
    return '\n'.join(c.get('text','') for c in content
                     if c.get('type') in {'text','output_text'})


class EvidenceExtractor:
    def __call__(self, batch, route):
        if batch.source_id != route.get('source_id') or route.get('isolated') is not True:
            return None
        found = []
        provenance = []
        for record in batch.records:
            text = assistant_text(record.data)
            for fact in route.get('facts', []):
                if fact['match'].casefold() in text.casefold() and fact['body'] not in found:
                    found.append(fact['body'])
                    provenance.append('record:'+record.key+':'+record.content_hash)
        if not found:
            return None
        return dict(namespace=route['namespace'],scope=route['scope'],mixed_scope=False,
                    body='\n'.join(found),provenance=sorted(set(provenance)))


def maintenance(store, scopes):
    def run():
        if not store.verified_private:
            return {'status':'blocked','reason':'destination_unverified'}
        examined = trashed = 0
        now = datetime.now(timezone.utc)
        for namespace, scope in scopes:
            rows = store.query(namespace,scope,limit=100,include_recovery=True)
            for row in rows:
                examined += 1
                if (row.get('status') == 'recovery' and row.get('kind') != 'protected_preference'
                        and datetime.fromisoformat(row['recovery_until']) <= now):
                    store.delete(namespace,scope,row['id'])
                    trashed += 1
        return dict(status='completed' if trashed else 'no_change',examined=examined,
                    trashed=trashed,permanent_deletion_verified=False)
    return run


def create(config):
    notion = config['notion']
    store = NotionStore(notion,NtnTransport(notion))
    eligible = {digest(str(p.resolve())) for p in scoped_paths(config)}
    routes = {k:v for k,v in notion.get('session_routes',{}).items()
              if v.get('source_id') in eligible}
    state = Path(config['state_dir'])
    quarantined = [digest(s['path']) for s in config.get('sources',[])
                   if digest(str(Path(s['path']).resolve())) not in eligible]
    atomic_json(state/'routing.json',dict(eligible=len(eligible),quarantined=quarantined))
    scopes = sorted({(v['namespace'],v['scope']) for v in routes.values()}
                    | {(notion['pilot_namespace'],notion['pilot_scope'])})
    def improve():
        from .activity import probe
        from .eval import run
        settings = config['improvement']
        root = Path(settings['root'])
        target = settings['target']
        content = settings['content']
        path = root/target
        before = digest(path.read_bytes()) if path.exists() else None
        proposal = dict(schema=1,status='no_candidate') if before == digest(content.encode()) else dict(
            schema=1,status='candidate',path=target,before_sha256=before,content=content)
        def checks(candidate):
            if candidate.read_text() != content:
                return False
            result, receipt = run(state/'protected')
            atomic_json(state/'protected-latest.json',dict(status=result['status'],receipt=str(receipt)))
            return result['status']=='passed'
        pipeline = ImprovementPipeline(gate=ChangeGate(root,state/'rollback',
            active=lambda: not probe().idle),state_dir=state/'improvements',
            targets=[target],protected_evals=(Path(__file__).resolve().parents[1]/'evals',
                Path(__file__).resolve().parents[1]/'evals/freeze.json'),
            checks=checks,postcheck=lambda p:p.read_text()==content,
            ledger=Ledger(state/'calls.jsonl'),proposal=proposal)
        return pipeline()
    return dict(store=store,sink=FindingSink(store,routes,EvidenceExtractor()),
                report_sink=ReportSink(store,notion['pilot_namespace'],notion['pilot_scope']),
                memory_maintenance=maintenance(store,scopes),improvement=improve)
