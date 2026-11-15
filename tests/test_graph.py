import copy
import datetime as dt
import json
import unittest

from renewal.graph import GraphStore
from renewal.notion import NotionStore, PrivacyError, RemoteError


CONFIG = {'notion': {'enabled': True, 'privacy_policy': 'cli_pilot', 'pilot_access': {
    'approved': True, 'approved_at': '2026-01-01T00:00:00+00:00',
    'expires_at': '2026-01-08T00:00:00+00:00'}}, 'graph': {
    'nodes_database_id': 'ndb', 'nodes_data_source_id': 'nodes',
    'edges_database_id': 'edb', 'edges_data_source_id': 'edges'}}


def text(value, kind='rich_text'):
    return {kind: [{'plain_text': value}]}


def page(identity, kind='nodes', status='current', scope='demo', endpoints=None):
    p = {'Name': text(identity, 'title'), 'Stable ID': text(identity),
         'Namespace': {'select': {'name': 'work'}}, 'Scope': text(scope),
         'Status': {'select': {'name': status}}, 'Source': text('synthetic/source')}
    if kind == 'nodes':
        p.update(Type={'select': {'name': 'project'}}, Summary=text('Synthetic graph example'),
                 Verified={'date': None}, Owner=text('fixture'))
    else:
        p.update(From={'relation': [{'id': endpoints[0]}]}, To={'relation': [{'id': endpoints[1]}]},
                 Relation={'select': {'name': 'supports'}})
    return {'id': identity, 'object': 'page', 'url': 'https://notion.so/'+identity,
            'public_url': None, 'in_trash': False, 'properties': p,
            'parent': {'type': 'data_source_id', 'data_source_id': kind,
                       'database_id': 'ndb' if kind == 'nodes' else 'edb'}}


class Transport:
    def __init__(self, config, fixtures, calls, valid=True):
        self.config, self.fixtures, self.calls, self.valid = config, fixtures, calls, valid

    def verify_destination(self, resource):
        self.calls.append(('guard', self.config['data_source_id'], resource))
        return self.valid

    def _api(self, method, path, body=None):
        self.calls.append((method, path, copy.deepcopy(body)))
        batches = self.fixtures[self.config['data_source_id']]
        if path.startswith('v1/pages'):
            if method == 'PATCH':
                row = next(r for batch in batches for r in batch if r['id'] == path.split('/')[-1])
            else:
                row = page('created-'+str(sum(len(b) for b in batches)), self.config['data_source_id'],
                           endpoints=('placeholder', 'placeholder'))
                batches[-1].append(row)
            row['properties'].update(copy.deepcopy(body['properties']))
            return copy.deepcopy(row)
        index = int(body.get('start_cursor', '0'))
        return {'results': copy.deepcopy(batches[index]), 'has_more': index+1 < len(batches),
                'next_cursor': str(index+1) if index+1 < len(batches) else None}


class GraphTests(unittest.TestCase):
    def store(self, nodes=None, edges=None, valid=True, now=None, **kwargs):
        self.calls = []
        fixtures = {'nodes': nodes if nodes is not None else [[page('a'), page('b'), page('c')]],
                    'edges': edges if edges is not None else [[page('ab', 'edges', endpoints=('a', 'b')),
                                                              page('bc', 'edges', status='historical', endpoints=('b', 'c'))]]}
        return GraphStore(CONFIG, lambda c: Transport(c, fixtures, self.calls, valid),
                          lambda c, t: NotionStore(c, t, now=lambda: now or dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)), **kwargs)

    def test_bidirectional_paths_status_and_evidence(self):
        result = self.store().inspect('work', 'demo', 'c', budget_chars=20000)
        self.assertEqual([r['node']['id'] for r in result['results']], ['c', 'b', 'a'])
        path = result['results'][2]['path']
        self.assertEqual([e['direction'] for e in path], ['incoming', 'incoming'])
        self.assertEqual(path[0]['edge']['status'], 'historical')
        self.assertEqual(path[0]['edge']['source'], 'synthetic/source')
        self.assertTrue(path[0]['edge']['url'].startswith('https://notion.so/'))
        self.assertEqual(len([c for c in self.calls if c[0] == 'guard']), 2)
        for method, path, body in self.calls:
            if method == 'POST':
                self.assertEqual(body['filter']['and'], [
                    {'property': 'Namespace', 'select': {'equals': 'work'}},
                    {'property': 'Scope', 'rich_text': {'equals': 'demo'}}])
                self.assertNotIn('blocks', path)

    def test_scopes_rejected_before_remote_calls(self):
        store = self.store()
        for scope in ('', '*', 'all', 'demo,other', 'demo/other', ['demo', 'other'], ' demo'):
            with self.subTest(scope=scope), self.assertRaises(PrivacyError):
                store.inspect('work', scope, 'a')
        self.assertEqual(self.calls, [])

    def test_returned_node_cross_scope_fails_closed(self):
        with self.assertRaises(PrivacyError):
            self.store(nodes=[[page('a'), page('b', scope='other')]]).inspect('work', 'demo', 'a')

    def test_returned_edge_cross_scope_fails_closed(self):
        with self.assertRaises(PrivacyError):
            self.store(edges=[[page('edge', 'edges', scope='other', endpoints=('a', 'b'))]]).inspect('work', 'demo', 'a')

    def test_foreign_endpoint_and_membership_fail_closed(self):
        with self.assertRaises(PrivacyError):
            self.store(edges=[[page('edge', 'edges', endpoints=('a', 'foreign'))]]).inspect('work', 'demo', 'a')
        row = page('a')
        row['parent']['database_id'] = 'foreign'
        with self.assertRaises(PrivacyError):
            self.store(nodes=[[row]], edges=[[]]).inspect('work', 'demo', 'a')

    def test_incomplete_relation_refused(self):
        edge = page('edge', 'edges', endpoints=('a', 'b'))
        edge['properties']['From']['has_more'] = True
        with self.assertRaises(PrivacyError):
            self.store(edges=[[edge]]).inspect('work', 'demo', 'a')

    def test_pagination_and_bounds(self):
        store = self.store(nodes=[[page('a')], [page('b')], [page('c')]])
        self.assertEqual(store.inspect('work', 'demo', 'a', budget_chars=20000)['matched_nodes'], 3)
        for kwargs in ({'max_pages': 1}, {'max_scan_nodes': 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(RemoteError):
                self.store(nodes=[[page('a')], [page('b')], [page('c')]], **kwargs).inspect('work', 'demo', 'a')
        with self.assertRaises(RemoteError):
            self.store().inspect('work', 'demo', 'a', max_nodes=2)

    def test_duplicate_identity_refused(self):
        duplicate = page('b')
        duplicate['properties']['Stable ID'] = text('a')
        with self.assertRaises(RemoteError):
            self.store(nodes=[[page('a'), duplicate]], edges=[[]]).inspect('work', 'demo', 'a')

    def test_lifecycle_and_relation_filters(self):
        nodes = [[page('a'), page('b', status='recovery'), page('c', status='superseded')]]
        store = self.store(nodes=nodes)
        self.assertEqual(store.recall('work', 'demo', query='synthetic')['matched_nodes'], 1)
        self.assertEqual(store.recall('work', 'demo', query='synthetic', include_recovery=True,
                                     include_superseded=True, budget_chars=20000)['matched_nodes'], 3)
        self.assertEqual(self.store().inspect('work', 'demo', 'a', relation='owns')['matched_nodes'], 1)
        self.assertEqual(self.store().inspect('work', 'demo', 'a', hops=0)['matched_nodes'], 1)

    def test_budget_is_explicit_and_total(self):
        result = self.store().inspect('work', 'demo', 'a', budget_chars=600)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 600)
        self.assertGreater(result['omitted_for_budget'], 0)
        self.assertEqual(result['matched_nodes'], 3)

    def test_privacy_failure_and_expired_policy_no_fallback(self):
        for store in (self.store(valid=False), self.store(now=dt.datetime(2026, 1, 9, tzinfo=dt.timezone.utc))):
            with self.subTest(), self.assertRaises(PrivacyError):
                store.inspect('work', 'demo', 'a')

    def test_node_and_hop_bounds(self):
        store = self.store()
        for kwargs in ({'hops': 3}, {'max_nodes': 41}, {'budget_chars': 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                store.inspect('work', 'demo', 'a', **kwargs)

    def remember(self, store, identity='decision-demo', **changes):
        args = dict(name='Reviewed synthetic decision', node_type='decision',
                    summary='Reviewed synthetic summary', source='synthetic/decision.md',
                    owner='fixture', reviewed=True)
        args.update(changes)
        return store.remember('work', 'demo', identity, **args)

    def test_reviewed_remember_create_update_and_readback(self):
        store = self.store()
        receipt = self.remember(store, verified='2026-01-02')
        self.assertTrue(receipt['durable'])
        self.assertEqual(receipt['record']['status'], 'assumed')
        self.assertEqual(receipt['record']['verified']['start'], '2026-01-02')
        second = self.remember(store, summary='Updated reviewed synthetic summary', status='current')
        self.assertEqual(second['record']['page_id'], receipt['record']['page_id'])
        self.assertEqual(second['record']['summary'], 'Updated reviewed synthetic summary')
        writes = [c for c in self.calls if c[1].startswith('v1/pages') and c[0] in {'POST', 'PATCH'}]
        self.assertEqual([c[0] for c in writes], ['POST', 'PATCH'])

    def test_remember_requires_review_evidence_and_type_immutability(self):
        for changes in ({'reviewed': False}, {'source': ''}, {'owner': ''}, {'summary': ''},
                        {'node_type': 'unknown'}, {'verified': 'yesterday'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.remember(self.store(), **changes)
        with self.assertRaises(PrivacyError):
            self.remember(self.store(), identity='a')

    def test_reviewed_link_create_update_and_immutable_direction(self):
        store = self.store()
        kwargs = dict(name='Synthetic reviewed relation', from_node='a', to_node='c',
                      relation='documents', source='synthetic/relation.md', reviewed=True)
        receipt = store.link('work', 'demo', 'ac', **kwargs)
        self.assertTrue(receipt['durable'])
        self.assertEqual(receipt['record']['from_page'], 'a')
        updated = store.link('work', 'demo', 'ac', **dict(kwargs, status='current'))
        self.assertEqual(updated['record']['status'], 'current')
        for changes in ({'from_node': 'c', 'to_node': 'a'}, {'relation': 'supports'}):
            with self.subTest(changes=changes), self.assertRaises(PrivacyError):
                store.link('work', 'demo', 'ac', **dict(kwargs, **changes))

    def test_link_foreign_and_ineligible_endpoints_refused(self):
        for nodes, endpoint in (([[page('a')]], 'foreign'),
                                ([[page('a'), page('b', status='recovery')]], 'b'),
                                ([[page('a'), page('b', status='superseded')]], 'b')):
            with self.subTest(endpoint=endpoint), self.assertRaises(PrivacyError):
                self.store(nodes=nodes, edges=[[]]).link('work', 'demo', 'ab', name='Synthetic edge',
                    from_node='a', to_node=endpoint, relation='supports', source='synthetic', reviewed=True)

    def test_write_duplicate_and_privacy_failure(self):
        duplicate = page('second-page')
        duplicate['properties']['Stable ID'] = text('a')
        with self.assertRaises(RemoteError):
            self.remember(self.store(nodes=[[page('a'), duplicate]], edges=[[]]))
        for store in (self.store(valid=False), self.store(now=dt.datetime(2026, 1, 9, tzinfo=dt.timezone.utc))):
            with self.subTest(), self.assertRaises(PrivacyError):
                self.remember(store)

    def test_write_exact_readback_mismatch_refused(self):
        store = self.store()
        original = store.stores['nodes'].transport._api
        def tamper(method, path, body=None):
            response = original(method, path, body)
            if path.startswith('v1/data_sources'):
                for row in response['results']:
                    if row['id'].startswith('created-'):
                        row['properties']['Summary'] = text('Unexpected concurrent value')
            return response
        store.stores['nodes'].transport._api = tamper
        with self.assertRaises(RemoteError):
            self.remember(store)

    def delayed_visibility(self, missing=1, duplicate=False):
        sleeps = []
        store = self.store(sleep=sleeps.append)
        original = store.stores['nodes'].transport._api
        observations = {'readbacks': 0, 'mutations': 0}
        def delayed(method, path, body=None):
            response = original(method, path, body)
            if path == 'v1/pages' and method == 'POST':
                observations['mutations'] += 1
            elif path.startswith('v1/data_sources') and observations['mutations']:
                observations['readbacks'] += 1
                if observations['readbacks'] <= missing:
                    response['results'] = [r for r in response['results'] if not r['id'].startswith('created-')]
                elif duplicate:
                    extra = copy.deepcopy(next(r for r in response['results'] if r['id'].startswith('created-')))
                    extra['id'] = 'concurrent-page'
                    response['results'].append(extra)
            return response
        store.stores['nodes'].transport._api = delayed
        return store, sleeps, observations

    def test_missing_then_present_retries_readback_only(self):
        store, sleeps, observations = self.delayed_visibility(missing=1)
        self.assertTrue(self.remember(store)['durable'])
        self.assertEqual(sleeps, [1])
        self.assertEqual(observations, {'readbacks': 2, 'mutations': 1})

    def test_persistent_missing_is_bounded_without_resend(self):
        store, sleeps, observations = self.delayed_visibility(missing=10)
        with self.assertRaises(RemoteError):
            self.remember(store)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(observations, {'readbacks': 3, 'mutations': 1})

    def test_duplicate_readback_fails_without_retry(self):
        store, sleeps, observations = self.delayed_visibility(missing=0, duplicate=True)
        with self.assertRaises(RemoteError):
            self.remember(store)
        self.assertEqual(sleeps, [])
        self.assertEqual(observations, {'readbacks': 1, 'mutations': 1})

    def test_mismatch_readback_fails_without_retry(self):
        sleeps = []
        store = self.store(sleep=sleeps.append)
        original = store.stores['nodes'].transport._api
        def changed(method, path, body=None):
            response = original(method, path, body)
            if path.startswith('v1/data_sources'):
                for row in response['results']:
                    if row['id'].startswith('created-'):
                        row['properties']['Summary'] = text('Unexpected concurrent value')
            return response
        store.stores['nodes'].transport._api = changed
        with self.assertRaises(RemoteError):
            self.remember(store)
        self.assertEqual(sleeps, [])


if __name__ == '__main__':
    unittest.main()
