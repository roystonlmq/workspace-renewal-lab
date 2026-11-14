import copy
import datetime as dt
import unittest

from renewal.lifecycle import MemoryService, MemoryError

NOW = dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc)


class FakeRemote:
    """Synthetic test service, never a production local cache."""
    verified_private = True
    def __init__(self):
        self.rows = {}
        self.fail = False
    def put(self, namespace, scope, key, value):
        if self.fail:
            raise OSError('remote unavailable')
        self.rows[namespace, scope, key] = copy.deepcopy(value)
        return {'id': key}
    def get(self, namespace, scope, key, include_recovery=False):
        row = self.rows.get((namespace, scope, key))
        return copy.deepcopy(row) if row and (include_recovery or row['status'] == 'active') else None
    def query(self, namespace, scope, limit=100, include_recovery=False):
        return [copy.deepcopy(v) for (n,s,k),v in self.rows.items() if n == namespace and s == scope and (include_recovery or v['status'] == 'active')][:limit]
    def delete(self, namespace, scope, key, recovery_only=True):
        row = self.rows[namespace, scope, key]
        if recovery_only and row['status'] != 'recovery':
            raise ValueError('active deletion forbidden')
        del self.rows[namespace, scope, key]


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.remote = FakeRemote()
        self.service = MemoryService(self.remote, clock=lambda: NOW)
    def add(self, key='m1', scope='synthetic-a', body='Use concise reports', theme='reports'):
        return self.service.remember('work', scope, key, body, kind='preference', provenance=['synthetic/session-1:4'], theme=theme)
    def test_scoped_recall_provenance_and_budget(self):
        self.add()
        self.add('m2', 'synthetic-b', 'Other secret')
        result = self.service.recall('work', 'synthetic-a', 'reports', budget_chars=1000)
        self.assertEqual([x['id'] for x in result], ['m1'])
        self.assertEqual(result[0]['provenance'], ['synthetic/session-1:4'])
        self.assertEqual(self.service.recall('personal','synthetic-a','reports'), [])
        self.assertEqual(self.service.recall('work','synthetic-a','reports',budget_chars=2), [])
    def test_correction_keeps_evidence_and_recovery_out_of_search(self):
        self.add()
        self.service.correct('work','synthetic-a','m1','Use detailed reports',['synthetic/session-2:2'])
        result = self.service.recall('work','synthetic-a','reports')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['body'], 'Use detailed reports')
        self.assertTrue(any(v['status'] == 'recovery' for v in self.remote.rows.values()))
    def test_forget_requires_evidence_then_recover_requires_future_ask(self):
        self.add()
        with self.assertRaises(MemoryError):
            self.service.forget('work','synthetic-a','m1', evidence=[], confidence=1)
        self.service.forget('work','synthetic-a','m1',evidence=['synthetic/obsolete'],confidence=.99)
        self.assertEqual(self.service.recall('work','synthetic-a','reports'), [])
        self.assertEqual(self.service.recover('work','synthetic-a','m1')['body'], 'Use concise reports')
        with self.assertRaises(MemoryError):
            self.service.forget('work','synthetic-a','m1',evidence=['synthetic/obsolete'],confidence=.99)
        self.service.forget('work','synthetic-a','m1',evidence=['explicit user request'],confidence=1,confirmed=True)
        row = self.remote.rows['work','synthetic-a','m1']
        self.assertEqual((dt.datetime.fromisoformat(row['recovery_until'])-NOW).days, 90)
    def test_failed_remote_write_is_not_successful(self):
        self.remote.fail = True
        with self.assertRaises(OSError):
            self.add()
        self.assertEqual(self.remote.rows,{})
    def test_missing_provenance_or_privacy_blocks(self):
        with self.assertRaises(MemoryError):
            self.service.remember('work','synthetic-a','m1','text',kind='preference',provenance=[])
        self.remote.verified_private=False
        with self.assertRaises(MemoryError):
            self.add()
    def test_new_memory_cannot_silently_overwrite_evidence(self):
        self.add()
        with self.assertRaises(MemoryError):
            self.add(body='Conflicting reports preference')
        self.assertEqual(self.service.require('work','synthetic-a','m1')['body'], 'Use concise reports')
    def test_expired_recovery_is_deleted_but_returned_theme_rule_survives(self):
        self.add()
        self.service.forget('work','synthetic-a','m1',evidence=['explicit obsolete source'],confidence=1)
        self.service.recover('work','synthetic-a','m1')
        self.service.forget('work','synthetic-a','m1',evidence=['explicit user request'],confidence=1,confirmed=True)
        self.service.clock = lambda: NOW + dt.timedelta(days=90)
        with self.assertRaises(MemoryError):
            self.service.recover('work','synthetic-a','m1')
        self.assertEqual(self.service.recall('work','synthetic-a','reports'), [])
        self.assertEqual(self.service.purge_expired('work','synthetic-a'), 1)
        self.assertEqual(self.service.purge_expired('work','synthetic-a'), 0)
        self.assertIsNotNone(self.remote.get('work','synthetic-a',self.service.theme_key('reports')))
        self.add(key='m2')
        with self.assertRaises(MemoryError):
            self.service.forget('work','synthetic-a','m2',evidence=['obsolete source'],confidence=1)
    def test_non_use_is_not_obsolescence(self):
        self.add()
        with self.assertRaises(MemoryError):
            self.service.forget('work','synthetic-a','m1',evidence=['not retrieved in 90 days'],confidence=1)
