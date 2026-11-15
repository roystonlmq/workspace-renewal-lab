"""Approved CLI policy checks real store behavior against API boundary fixtures."""
import copy
import datetime as dt
import unittest

from renewal.notion import NotionStore, NtnTransport, PrivacyError
from test_notion import FakeTransport, NOW, OWNER


CONFIG = dict(enabled=True, privacy_policy='cli_pilot', account_email=OWNER,
              workspace_id='workspace', setup_page_id='setup', database_id='database',
              data_source_id='source', pilot_access={
                  'approved': True, 'approved_at': NOW.isoformat(),
                  'expires_at': (NOW+dt.timedelta(days=14)).isoformat()})


class ApiTransport(NtnTransport, FakeTransport):
    def __init__(self, config):
        NtnTransport.__init__(self, config)
        FakeTransport.__init__(self)
        self.api = {
            'v1/users/me': {'object': 'user', 'type': 'bot', 'bot': {
                'workspace_id': 'workspace', 'owner': {'type': 'user', 'user': {
                    'person': {'email': OWNER}}}}},
            'v1/pages/setup': {'object': 'page', 'id': 'setup', 'public_url': None,
                'in_trash': False, 'parent': {'type': 'workspace', 'workspace': True}},
            'v1/databases/database': {'object': 'database', 'id': 'database',
                'public_url': None, 'in_trash': False,
                'parent': {'type': 'page_id', 'page_id': 'setup'},
                'data_sources': [{'id': 'source', 'name': 'Memory'}]},
            'v1/data_sources/source': {'object': 'data_source', 'id': 'source',
                'public_url': None, 'in_trash': False,
                'parent': {'type': 'database_id', 'database_id': 'database'}},
            'v1/pages/synthetic-row': {'object': 'page', 'id': 'synthetic-row',
                'public_url': None, 'in_trash': False,
                'parent': {'type': 'data_source_id', 'data_source_id': 'source',
                           'database_id': 'database'}}}

    def _api(self, method, path, body=None):
        if method != 'GET':
            raise AssertionError('Policy checks must be read-only')
        return copy.deepcopy(self.api[path])

    query = FakeTransport.query
    put = FakeTransport.put


class CliPolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = copy.deepcopy(CONFIG)
        self.remote = ApiTransport(self.config)
        self.store = NotionStore(self.config, self.remote, now=lambda: NOW)

    def test_approved_cli_policy_supports_scoped_write_and_read_without_acl_provider(self):
        row = dict(id='one', namespace='personal', scope='synthetic', body='Synthetic', status='active')
        self.assertTrue(self.store.verified_private)
        self.store.put('personal', 'synthetic', 'one', row)
        self.assertEqual(self.store.get('personal', 'synthetic', 'one'), row)
        self.assertIsNone(self.store.get('personal', 'other', 'one'))

    def test_wrong_account_or_workspace_is_rejected_before_content_query(self):
        for field in ('account_email', 'workspace_id'):
            with self.subTest(field=field):
                self.config[field] = 'wrong'
                with self.assertRaises(PrivacyError):
                    self.store.query('personal', 'synthetic')
                self.assertEqual(self.remote.calls, 0)
                self.config[field] = CONFIG[field]

    def test_changed_ancestry_publication_or_source_is_rejected(self):
        changes = [
            ('v1/pages/setup', 'parent', {'type': 'page_id', 'page_id': 'elsewhere'}),
            ('v1/databases/database', 'public_url', 'https://example.invalid/published'),
            ('v1/databases/database', 'data_sources', [{'id': 'other'}]),
            ('v1/data_sources/source', 'parent', {'type': 'database_id', 'database_id': 'other'}),
            ('v1/data_sources/source', 'in_trash', True)]
        for path, key, value in changes:
            with self.subTest(path=path, key=key):
                before = copy.deepcopy(self.remote.api[path])
                self.remote.api[path][key] = value
                self.assertFalse(self.store.verified_private)
                self.remote.api[path] = before

    def test_existing_record_outside_destination_cannot_be_read_or_updated(self):
        self.remote.rows['personal', 'synthetic', 'one'] = dict(
            id='one', namespace='personal', scope='synthetic', body='Before', status='active')
        self.remote.api['v1/pages/synthetic-row']['parent']['data_source_id'] = 'other'
        with self.assertRaises(PrivacyError):
            self.store.get('personal', 'synthetic', 'one')
        with self.assertRaises(PrivacyError):
            self.store.put('personal', 'synthetic', 'one', dict(
                id='one', namespace='personal', scope='synthetic', body='After', status='active'))
        self.assertEqual(self.remote.rows['personal', 'synthetic', 'one']['body'], 'Before')

    def test_missing_approval_expired_or_overlong_pilot_remains_disabled(self):
        for variant in ({'approved': False}, {'expires_at': NOW.isoformat()},
                        {'expires_at': (NOW+dt.timedelta(days=15)).isoformat()},
                        {'approved_at': (NOW+dt.timedelta(seconds=1)).isoformat()}):
            with self.subTest(variant=variant):
                self.config['pilot_access'] = dict(CONFIG['pilot_access'], **variant)
                self.assertFalse(self.store.verified_private)

    def test_later_account_change_is_checked_again(self):
        self.assertTrue(self.store.verified_private)
        self.remote.api['v1/users/me']['bot']['workspace_id'] = 'other'
        self.assertFalse(self.store.verified_private)


if __name__ == '__main__':
    unittest.main()
