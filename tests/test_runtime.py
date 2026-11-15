import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone

from renewal.ingestion import Ingestor, digest
from renewal.runtime import EvidenceExtractor, scoped_paths, maintenance
from unittest.mock import patch
from renewal.nightly import Activity
from renewal.runtime import create


class RuntimeTests(unittest.TestCase):
    def test_foreground_callback_keeps_verified_idle_requirement(self):
        with tempfile.TemporaryDirectory() as d:
            config = dict(notion=dict(session_routes={},pilot_namespace='personal',pilot_scope='internal'),
                state_dir=d,sources=[],improvement=dict(root=d,target='instructions/facts.md',content='Scoped facts.'))
            for activity, blocked in [(Activity(),True),
                    (Activity(verified=True,idle=False),True),
                    (Activity(verified=True,idle=True),False)]:
                with self.subTest(activity=activity), patch('renewal.runtime.NotionStore'), \
                        patch('renewal.runtime.ImprovementPipeline') as pipeline:
                    callbacks = create(config,activity_check=lambda: activity)
                    callbacks['improvement']()
                    self.assertEqual(pipeline.call_args.kwargs['gate'].active(),blocked)

    def test_real_harness_shapes_extract_only_catalog_facts(self):
        for harness in ('claude', 'codex'):
            with self.subTest(harness=harness), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                p = root / 'source.jsonl'
                if harness == 'codex':
                    rows = [dict(type='session_meta', payload={'id':'s'}),
                            dict(type='response_item', payload={'type':'message','role':'assistant',
                                'content':[{'type':'output_text','text':'Pilot not started. SECRET RAW TEXT'}]})]
                else:
                    rows = [dict(type='assistant', sessionId='s', message={'role':'assistant',
                                'content':[{'type':'text','text':'Pilot not started. SECRET RAW TEXT'}]})]
                p.write_text(''.join(json.dumps(r)+'\n' for r in rows))
                route = dict(namespace='work', scope='renewal', isolated=True,
                    source_id=digest(str(p.resolve())),
                    facts=[dict(match='Pilot not started', body='Operational pilot is not started.')])
                findings=[]
                extract=EvidenceExtractor()
                result=Ingestor(root/'state.json', settle_seconds=0).process([p],
                    lambda b: findings.append(extract(b,route)) or True, now=datetime.now(timezone.utc))
                self.assertEqual(result['processed_sessions'],1)
                self.assertEqual(findings[0]['body'],'Operational pilot is not started.')
                self.assertNotIn('SECRET',json.dumps(findings))
                route['source_id']='wrong'
                self.assertIsNone(extract.__call__(type('B',(),{'source_id':'other'})(),route))

    def test_unreviewed_and_changed_sources_quarantined_without_content(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'s.jsonl'; p.write_text('{}\n')
            config={'sources':[{'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}]}
            self.assertEqual(list(scoped_paths(config)),[p])
            p.write_text('{"private":"mixed"}\n')
            self.assertEqual(list(scoped_paths(config)),[])

    def test_maintenance_does_not_forget_from_age_alone(self):
        class Store:
            verified_private=True
            def query(self,*a,**k):
                return [{'id':'old','status':'active','updated_at':'2000-01-01T00:00:00+00:00'}]
            def delete(self,*a,**k):
                raise AssertionError('active memory cannot be deleted')
        result=maintenance(Store(),[('work','renewal')])()
        self.assertEqual(result['status'],'no_change')
        self.assertEqual(result['examined'],1)


if __name__=='__main__': unittest.main()
