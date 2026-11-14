import copy
import datetime as dt
import types
import unittest
from unittest.mock import patch
import json

from renewal.notion import NotionStore, NtnTransport, PrivacyError, RemoteError, FindingSink, ReportSink, create
from renewal.lifecycle import MemoryService

NOW = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)
OWNER = 'owner'+'@'+'example.invalid'
CONFIG = {'enabled': True, 'account_email': OWNER, 'workspace_id': 'synthetic',
          'data_source_id': 'synthetic-data', 'allowed_principals': ['synthetic-owner'], 'acl_max_age_seconds': 60}


class FakeTransport:
    """Test fixture only. Runtime never constructs a local content store."""
    def __init__(self):
        self.rows = {}
        self.fail = False
        self.calls = 0
    def query(self, namespace, scope, key=None, limit=100, include_recovery=False):
        self.calls += 1
        if self.fail:
            raise RemoteError('synthetic unavailable')
        return [{'resource_id': 'synthetic-row', 'value':copy.deepcopy(v)} for (n,s,k),v in self.rows.items()
                if n == namespace and s == scope and (key is None or k == key)
                and (include_recovery or v['status'] == 'active')][:limit]
    def put(self,n,s,k,v):
        if self.fail:
            raise RemoteError('synthetic unavailable')
        self.rows[n,s,k] = copy.deepcopy(v)
        return {'resource_id':'synthetic-row','durable':True}
    def delete(self,n,s,k):
        del self.rows[n,s,k]
        return {'in_trash': True}


def proof(resource):
    return {'resource_id':resource,'complete':True,'account_email':OWNER,
            'owner_email':OWNER,'workspace_id':'synthetic',
            'general_access':'invited_only','public_access':False,'teamspace_access':False,
            'ancestry_restricted':True,'inherited_access_reviewed':True,
            'principals':['synthetic-owner'],'verified_at':NOW.isoformat()}


class NotionTest(unittest.TestCase):
    def setUp(self):
        self.remote=FakeTransport()
        self.store=NotionStore(CONFIG,self.remote,proof,now=lambda:NOW)
        self.service=MemoryService(self.store,clock=lambda:NOW)
    def add(self):
        return self.service.remember('work','synthetic-a','one','Use concise reports',
                                     kind='preference',provenance=['synthetic/1'])
    def test_production_factory_is_disabled_without_live_acl(self):
        connector=create({'notion':CONFIG})
        self.assertFalse(connector['store'].verified_private)
        self.assertFalse(connector['sink'].verified_private)
        self.assertFalse(connector['report_sink']({}))
    def test_ntn_verified_help_request_shape_and_remote_active_filter(self):
        transport=NtnTransport(CONFIG)
        with patch('renewal.notion.subprocess.run') as run:
            run.return_value=types.SimpleNamespace(returncode=0,stdout='{"results":[],"has_more":false}')
            self.assertEqual(transport.query('work','synthetic-a'),[])
            args,kwargs=run.call_args
            self.assertEqual(args[0],['ntn','api','v1/data_sources/synthetic-data/query','-X','POST','-d','@-'])
            body=json.loads(kwargs['input'])
            self.assertIn({'property':'Status','select':{'equals':'active'}},body['filter']['and'])
            self.assertEqual(kwargs['timeout'],30)
            transport.query('work','synthetic-a',include_recovery=True)
            self.assertEqual(len(json.loads(run.call_args.kwargs['input'])['filter']['and']),1)
    def test_public_url_null_is_not_privacy_proof(self):
        store=NotionStore(CONFIG,self.remote,lambda _: {'public_url':None},now=lambda:NOW)
        with self.assertRaises(PrivacyError):
            store.query('work','synthetic-a')
        self.assertEqual(self.remote.calls,0)
    def test_account_ancestry_public_and_stale_proof_fail_closed(self):
        variants=[{'account_email':'other'+'@'+'example.invalid'}, {'ancestry_restricted':False},
                  {'public_access':True}, {'teamspace_access':True}, {'complete':False},
                  {'principals':['synthetic-owner','extra']},
                  {'verified_at':(NOW-dt.timedelta(seconds=61)).isoformat()}]
        for variant in variants:
            store=NotionStore(CONFIG,self.remote,lambda r:dict(proof(r),**variant),now=lambda:NOW)
            with self.subTest(variant=variant), self.assertRaises(PrivacyError):
                store.query('work','synthetic-a')
    def test_live_acl_rechecked_each_operation_and_each_record(self):
        self.add()
        self.assertTrue(self.store.verified_private)
        self.store.acl_verifier=lambda r:dict(proof(r),public_access=r=='synthetic-row')
        with self.assertRaises(PrivacyError):
            self.store.query('work','synthetic-a')
    def test_remote_lifecycle_correction_forgetting_and_recovery(self):
        self.add()
        self.service.correct('work','synthetic-a','one','Use detailed reports',['synthetic/2'])
        self.assertEqual(len(self.store.query('work','synthetic-a')),1)
        self.assertEqual(len(self.store.query('work','synthetic-a',include_recovery=True)),2)
        self.service.forget('work','synthetic-a','one',evidence=['superseded'],confidence=1)
        self.assertIsNone(self.store.get('work','synthetic-a','one'))
        self.assertEqual(self.store.get('work','synthetic-a','one',True)['status'],'recovery')
        self.service.recover('work','synthetic-a','one')
        self.assertTrue(self.store.get('work','synthetic-a','one')['ask_before_forgetting'])
        self.assertEqual(self.store.query('work','synthetic-b'),[])
    def test_no_local_cache_after_remote_failure(self):
        self.add()
        self.remote.fail=True
        with self.assertRaises(RemoteError):
            self.store.get('work','synthetic-a','one')
    def test_active_and_early_recovery_deletion_rejected(self):
        self.add()
        with self.assertRaises(PrivacyError):
            self.store.delete('work','synthetic-a','one')
        self.store.archive('work','synthetic-a','one')
        row=self.store.get('work','synthetic-a','one',True)
        self.assertEqual((dt.datetime.fromisoformat(row['recovery_until'])-NOW).days,90)
        with self.assertRaises(PrivacyError):
            self.store.delete('work','synthetic-a','one')
        self.store.now=lambda:NOW+dt.timedelta(days=91)
        self.store.acl_verifier=lambda r:dict(proof(r),verified_at=self.store.now().isoformat())
        self.assertTrue(self.store.delete('work','synthetic-a','one')['in_trash'])
    def test_sink_has_no_default_extractor_or_implicit_customer_scope(self):
        sink=FindingSink(self.store,{})
        self.assertFalse(sink(types.SimpleNamespace(session_id='synthetic-session')))
        self.assertEqual(self.remote.rows,{})
    def test_private_pilot_report_is_delivered_and_replay_is_idempotent(self):
        report={'kind':'workspace-renewal-pilot-report','idempotency_key':'synthetic-pilot',
                'started_at':NOW.isoformat(),'due_at':(NOW+dt.timedelta(days=14)).isoformat(),
                'generated_at':NOW.isoformat(),'observed_days':2,'measured_14_days':False,
                'completed_outcomes':3,'useful_changes':1,'regressions':0,'rollbacks':0,
                'backlog_days':1,'costs':{'actual_charges':None,'pricing_estimate':None},
                'average_observed_day_cost':None,'recommended_ongoing_budget':None}
        sink=ReportSink(self.store,'work','internal-renewal')
        self.assertTrue(sink(report))
        self.assertTrue(sink(report))
        self.assertEqual(len(self.remote.rows),1)
        row=next(iter(self.remote.rows.values()))
        self.assertIn('Measured 14 days: false',row['body'])
        self.assertIn('Actual charges: unavailable',row['body'])
        self.assertIn('Recommended ongoing budget: OPEN',row['body'])
    def test_report_failure_or_missing_scope_is_not_delivery(self):
        self.assertFalse(ReportSink(self.store,None,None)({'kind':'workspace-renewal-pilot-report'}))
        self.remote.fail=True
        self.assertFalse(ReportSink(self.store,'work','internal-renewal')(
            {'kind':'workspace-renewal-pilot-report','idempotency_key':'synthetic','started_at':NOW.isoformat()}))
    def test_useful_findings_replay_without_raw_records_and_mixed_scope_rejects(self):
        batch=types.SimpleNamespace(session_id='synthetic-session', idempotency_key='stable-id',records=['raw excluded'])
        route={'namespace':'work','scope':'synthetic-a','isolated':True}
        finding={'namespace':'work','scope':'synthetic-a','body':'Useful finding',
                 'provenance':['synthetic/1'],'mixed_scope':False}
        sink=FindingSink(self.store,{'synthetic-session':route},lambda b,r:finding)
        self.assertTrue(sink(batch))
        self.assertTrue(sink(batch))
        self.assertEqual(len(self.remote.rows),1)
        self.assertNotIn('raw excluded',str(self.remote.rows))
        finding['mixed_scope']=True
        self.assertFalse(sink(batch))


if __name__ == '__main__':
    unittest.main()
