"""Live, isolated Notion graph metadata. No page bodies or disk cache."""
import json
import re
import datetime as dt
import time
from collections import deque

from .notion import NotionStore, NtnTransport, PrivacyError, RemoteError

STATUSES = {'current', 'historical', 'assumed', 'superseded', 'recovery'}
RELATIONS = {'owns', 'contains', 'supports', 'depends_on', 'supersedes', 'applies_to', 'documents'}
TYPES = {'customer', 'project', 'decision', 'lesson', 'artifact', 'workflow'}


def route(namespace, scope):
    if namespace not in {'work', 'personal'} or not isinstance(scope, str) or not re.fullmatch(r'[a-z0-9]+(?:[-_.:][a-z0-9]+)*', scope) or scope in {'all', '*'}:
        raise PrivacyError('One explicit namespace and isolated scope required')


def rich(prop, kind='rich_text'):
    return ''.join(x.get('plain_text', x.get('text', {}).get('content', '')) for x in prop[kind])


class GraphStore:
    """Queries always carry exact namespace and scope filters.

    Each operation checks live policy and destination independently for both data
    sources. Returned metadata must independently match that destination and route.
    A transport factory may supply a strict ACL verifier through guard_factory.
    """
    def __init__(self, config, transport_factory=NtnTransport, guard_factory=NotionStore,
                 max_pages=10, max_scan_nodes=200, max_scan_edges=400,
                 readback_delays=(1, 2), sleep=time.sleep):
        self.config = config
        self.graph = config['graph']
        if not 1 <= max_pages <= 20 or not 1 <= max_scan_nodes <= 1000 or not 1 <= max_scan_edges <= 2000:
            raise ValueError('Invalid graph scan bounds')
        self.bounds = {'nodes': max_scan_nodes, 'edges': max_scan_edges}
        self.max_pages = max_pages
        if (not isinstance(readback_delays, (tuple, list)) or len(readback_delays) > 3
                or any(not isinstance(delay, (int, float)) or not 0 <= delay <= 3 for delay in readback_delays)
                or not callable(sleep)):
            raise ValueError('Readback retries require at most three bounded delays')
        self.readback_delays, self.sleep = tuple(readback_delays), sleep
        self.stores = {}
        for kind in ('nodes', 'edges'):
            c = dict(config['notion'], database_id=self.graph[kind+'_database_id'],
                     data_source_id=self.graph[kind+'_data_source_id'])
            self.stores[kind] = guard_factory(c, transport_factory(c))

    def _page(self, page, kind, namespace, scope):
        c = self.stores[kind].config
        try:
            parent = page['parent']
            if (page['object'] != 'page' or not page['id'] or '/' in page['id']
                    or page.get('in_trash') is not False or page.get('archived', False)
                    or page.get('is_archived', False) or 'public_url' not in page
                    or page['public_url'] is not None
                    or parent.get('type') != 'data_source_id'
                    or parent.get('data_source_id') != c['data_source_id']
                    or parent.get('database_id') != c['database_id']):
                raise PrivacyError('Graph page outside private data source')
            p = page['properties']
            row = {'id': rich(p['Stable ID']), 'name': rich(p['Name'], 'title'),
                   'namespace': p['Namespace']['select']['name'], 'scope': rich(p['Scope']),
                   'status': p['Status']['select']['name'], 'source': rich(p['Source']),
                   'page_id': page['id'], 'url': page['url']}
            if (row['namespace'], row['scope']) != (namespace, scope):
                raise PrivacyError('Graph row crossed the requested scope')
            if not row['id'] or row['status'] not in STATUSES:
                raise RemoteError('Invalid graph identity or lifecycle')
            if kind == 'nodes':
                row.update(type=p['Type']['select']['name'], summary=rich(p['Summary']),
                           verified=p['Verified']['date'], owner=rich(p['Owner']))
                if row['type'] not in TYPES:
                    raise RemoteError('Unknown node type')
            else:
                if any(p[key].get('has_more') or len(p[key]['relation']) != 1 for key in ('From', 'To')):
                    raise PrivacyError('Edge requires exactly two complete endpoint identities')
                row.update(from_page=p['From']['relation'][0]['id'], to_page=p['To']['relation'][0]['id'],
                           relation=p['Relation']['select']['name'])
                if row['relation'] not in RELATIONS:
                    raise RemoteError('Unknown edge relation')
            return row
        except (KeyError, TypeError, AttributeError) as exc:
            raise RemoteError('Incomplete graph metadata response') from exc

    def _scan(self, kind, namespace, scope):
        store = self.stores[kind]
        store._privacy()  # Same fixed policy, live identity and destination guard.
        rows, cursor, seen = [], None, set()
        for _ in range(self.max_pages):
            body = {'page_size': 100, 'filter': {'and': [
                {'property': 'Namespace', 'select': {'equals': namespace}},
                {'property': 'Scope', 'rich_text': {'equals': scope}}]}}
            if cursor:
                body['start_cursor'] = cursor
            response = store.transport._api('POST', 'v1/data_sources/'+store.config['data_source_id']+'/query', body)
            if not isinstance(response.get('results'), list) or len(response['results']) > 100 or not isinstance(response.get('has_more'), bool):
                raise RemoteError('Invalid graph pagination response')
            for page in response['results']:
                row = self._page(page, kind, namespace, scope)
                if row['page_id'] in seen:
                    raise RemoteError('Repeated graph page in pagination')
                seen.add(row['page_id'])
                rows.append(row)
                if len(rows) > self.bounds[kind]:
                    raise RemoteError('Graph scan exceeded bound; narrow the scope')
            if response['has_more'] is False:
                if len({r['id'] for r in rows}) != len(rows):
                    raise RemoteError('Duplicate stable graph identity')
                return rows
            next_cursor = response.get('next_cursor')
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise RemoteError('Invalid graph continuation cursor')
            cursor = next_cursor
        raise RemoteError('Graph pagination exceeded bound; no partial result returned')

    def recall(self, namespace, scope, query='', node=None, hops=2, max_nodes=40,
               relation=None, budget_chars=4000, include_recovery=False, include_superseded=False):
        route(namespace, scope)
        if not 0 <= hops <= 2 or not 1 <= max_nodes <= 40 or not 128 <= budget_chars <= 100000:
            raise ValueError('Recall requires 0 to 2 hops, 1 to 40 nodes and a bounded character budget')
        if relation is not None and relation not in RELATIONS:
            raise ValueError('Unknown relation')
        if not isinstance(query, str) or (not query.strip() and not node):
            raise ValueError('A keyword query or stable node identity is required')
        nodes = self._scan('nodes', namespace, scope)
        edges = self._scan('edges', namespace, scope)
        by_page = {n['page_id']: n for n in nodes}
        # Check endpoints before lifecycle filtering, even for hidden edges.
        for edge in edges:
            if edge['from_page'] not in by_page or edge['to_page'] not in by_page:
                raise PrivacyError('Edge endpoint absent from the isolated node data source')
        excluded = set()
        if not include_recovery:
            excluded.add('recovery')
        if not include_superseded:
            excluded.add('superseded')
        eligible = {p: n for p, n in by_page.items() if n['status'] not in excluded}
        words = query.casefold().split()
        seeds = [n for n in eligible.values() if n['id'] == node] if node else [
            n for n in eligible.values() if all(w in (n['name']+' '+n['summary']).casefold() for w in words)]
        seeds.sort(key=lambda n: n['id'])
        adjacent = {p: [] for p in eligible}
        for edge in sorted(edges, key=lambda e: e['id']):
            a, b = edge['from_page'], edge['to_page']
            if edge['status'] in excluded or (relation and edge['relation'] != relation) or a not in eligible or b not in eligible:
                continue
            adjacent[a].append((b, edge, 'outgoing'))
            adjacent[b].append((a, edge, 'incoming'))
        queue = deque((n['page_id'], []) for n in seeds)
        visited, results = set(), []
        while queue:
            page, path = queue.popleft()
            if page in visited:
                continue
            if len(visited) >= max_nodes:
                raise RemoteError('Graph traversal exceeded node bound; narrow the query')
            visited.add(page)
            results.append({'node': eligible[page], 'path': path})
            if len(path) < hops:
                for target, edge, direction in adjacent[page]:
                    if target not in visited:
                        queue.append((target, path+[{'edge': edge, 'direction': direction,
                                                   'from': eligible[page]['id'], 'to': eligible[target]['id']}]))
        result = {'namespace': namespace, 'scope': scope, 'results': results,
                  'matched_nodes': len(results), 'omitted_for_budget': 0}
        while len(json.dumps(result, ensure_ascii=False)) > budget_chars and result['results']:
            result['results'].pop()
            result['omitted_for_budget'] += 1
        if len(json.dumps(result, ensure_ascii=False)) > budget_chars:
            raise ValueError('Character budget too small for result envelope')
        return result

    def inspect(self, namespace, scope, node, **kwargs):
        return self.recall(namespace, scope, node=node, **kwargs)

    @staticmethod
    def _reviewed(namespace, scope, stable_id, name, source, status, reviewed):
        route(namespace, scope)
        if reviewed is not True:
            raise ValueError('Explicit review is required; automatic extraction is unsupported')
        if not isinstance(stable_id, str) or not re.fullmatch(r'[a-z0-9]+(?:[-_.:][a-z0-9]+)*', stable_id) or len(stable_id) > 200:
            raise ValueError('Explicit stable identity must be a bounded slug')
        for key, value, bound in (('name', name, 300), ('source', source, 2000)):
            if not isinstance(value, str) or not value.strip() or len(value) > bound:
                raise ValueError('Explicit bounded '+key+' required')
        if status not in STATUSES:
            raise ValueError('Explicit lifecycle status required')

    def _write(self, kind, namespace, scope, value, existing):
        """Guard immediately before mutation, then verify exact scoped readback.

        Notion has no compare-and-swap or unique constraints. Callers must serialize
        writes per scope. A conflicting concurrent create is detected at readback,
        never automatically deleted or overwritten as reconciliation.
        """
        store = self.stores[kind]
        if existing:
            store._privacy(existing['page_id'])
        else:
            store._privacy()
        def rt(s, title=False):
            return {'title' if title else 'rich_text': [{'text': {'content': s[i:i+1800]}}
                    for i in range(0, len(s), 1800)]}
        properties = {'Name': rt(value['name'], True), 'Stable ID': rt(value['id']),
                      'Namespace': {'select': {'name': namespace}}, 'Scope': rt(scope),
                      'Status': {'select': {'name': value['status']}}, 'Source': rt(value['source'])}
        if kind == 'nodes':
            properties.update(Type={'select': {'name': value['type']}}, Summary=rt(value['summary']),
                              Owner=rt(value['owner']), Verified={'date': value['verified']})
        else:
            properties.update(From={'relation': [{'id': value['from_page']}]},
                              To={'relation': [{'id': value['to_page']}]},
                              Relation={'select': {'name': value['relation']}})
        if existing:
            response = store.transport._api('PATCH', 'v1/pages/'+existing['page_id'], {'properties': properties})
        else:
            response = store.transport._api('POST', 'v1/pages', {
                'parent': {'data_source_id': store.config['data_source_id']}, 'properties': properties})
        stored_response = self._page(response, kind, namespace, scope)
        if existing and stored_response['page_id'] != existing['page_id']:
            raise PrivacyError('Write response changed the target page identity')
        if any(stored_response.get(k) != v for k, v in value.items()):
            raise RemoteError('Graph write response differs from requested reviewed values')
        for attempt in range(len(self.readback_delays)+1):
            matches = [r for r in self._scan(kind, namespace, scope) if r['id'] == value['id']]
            if matches:
                if len(matches) != 1 or matches[0]['page_id'] != stored_response['page_id'] or any(matches[0].get(k) != v for k, v in value.items()):
                    raise RemoteError('Graph write exact scoped readback failed; review before retrying')
                return {'durable': True, 'record': matches[0]}
            if attempt < len(self.readback_delays):
                self.sleep(self.readback_delays[attempt])
        raise RemoteError('Graph write missing from bounded scoped readback; review before retrying')

    def remember(self, namespace, scope, stable_id, *, name, node_type, summary,
                 source, owner, status='assumed', verified=None, reviewed=False):
        self._reviewed(namespace, scope, stable_id, name, source, status, reviewed)
        if node_type not in TYPES:
            raise ValueError('Typed node required')
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000 or not isinstance(owner, str) or not owner.strip() or len(owner) > 300:
            raise ValueError('Bounded reviewed summary and explicit owner required')
        if verified is not None:
            try:
                if dt.date.fromisoformat(verified).isoformat() != verified:
                    raise ValueError()
            except (TypeError, ValueError) as exc:
                raise ValueError('Verified must be an ISO date or omitted') from exc
        rows = self._scan('nodes', namespace, scope)
        existing = next((r for r in rows if r['id'] == stable_id), None)
        if existing and existing['type'] != node_type:
            raise PrivacyError('Existing graph node type is immutable')
        value = {'id': stable_id, 'namespace': namespace, 'scope': scope, 'name': name,
                 'type': node_type, 'summary': summary, 'source': source, 'owner': owner,
                 'status': status, 'verified': {'start': verified, 'end': None, 'time_zone': None} if verified else None}
        return self._write('nodes', namespace, scope, value, existing)

    def link(self, namespace, scope, stable_id, *, name, from_node, to_node, relation,
             source, status='assumed', reviewed=False):
        self._reviewed(namespace, scope, stable_id, name, source, status, reviewed)
        if relation not in RELATIONS:
            raise ValueError('Typed relation required')
        nodes = {r['id']: r for r in self._scan('nodes', namespace, scope)}
        if any(identity not in nodes or nodes[identity]['status'] in {'recovery', 'superseded'} for identity in (from_node, to_node)):
            raise PrivacyError('Both endpoints must be eligible nodes in this exact scope')
        rows = self._scan('edges', namespace, scope)
        existing = next((r for r in rows if r['id'] == stable_id), None)
        value = {'id': stable_id, 'namespace': namespace, 'scope': scope, 'name': name,
                 'from_page': nodes[from_node]['page_id'], 'to_page': nodes[to_node]['page_id'],
                 'relation': relation, 'source': source, 'status': status}
        if existing and any(existing[k] != value[k] for k in ('from_page', 'to_page', 'relation')):
            raise PrivacyError('Existing edge endpoints and relation are immutable')
        for page_id in {value['from_page'], value['to_page']}:
            self.stores['nodes']._privacy(page_id)
        return self._write('edges', namespace, scope, value, existing)
