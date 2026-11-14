"""Remote authoritative Notion storage. No ACL API is assumed or inferred.

An independently implemented live ACL verifier is required for real operation.
The public Pages API and a null public_url cannot supply that proof.
"""
import datetime as dt
import hashlib
import json
import subprocess


class PrivacyError(RuntimeError):
    pass


class RemoteError(RuntimeError):
    pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


class NotionStore:
    def __init__(self, config, transport, acl_verifier=None, now=utcnow):
        self.config, self.transport = config, transport
        self.acl_verifier, self.now = acl_verifier, now

    def _privacy(self, resource=None):
        c = self.config
        resource = resource or c.get('data_source_id')
        if c.get('enabled') is not True or not resource or not callable(self.acl_verifier):
            raise PrivacyError('Private Notion integration or live ACL verifier unavailable')
        try:
            proof = self.acl_verifier(resource)
            age = (self.now() - dt.datetime.fromisoformat(proof['verified_at'])).total_seconds()
            allowed = set(c['allowed_principals'])
            complete = (
                proof.get('resource_id') == resource and proof.get('complete') is True
                and proof.get('account_email') == c['account_email']
                and proof.get('owner_email') == c['account_email']
                and proof.get('workspace_id') == c['workspace_id']
                and proof.get('general_access') == 'invited_only'
                and proof.get('public_access') is False
                and proof.get('teamspace_access') is False
                and proof.get('ancestry_restricted') is True
                and proof.get('inherited_access_reviewed') is True
                and set(proof['principals']) == allowed and bool(allowed)
                and 0 <= age <= min(c.get('acl_max_age_seconds', 60), 60)
            )
        except (KeyError, ValueError, TypeError):
            complete = False
        if not complete:
            raise PrivacyError('Fresh complete account, ancestry and sharing proof required')
        return True

    @property
    def verified_private(self):
        try:
            return self._privacy()
        except Exception:
            return False

    def _guard(self, namespace, scope):
        self._privacy()
        if namespace not in {'work', 'personal'} or not scope or scope in {'*', 'all'} or '/' in scope:
            raise PrivacyError('Explicit namespace and isolated scope required')

    def get(self, namespace, scope, key, include_recovery=False):
        rows = self.query(namespace, scope, limit=100, include_recovery=include_recovery, key=key)
        if len(rows) > 1:
            raise RemoteError('Duplicate remote key requires reconciliation')
        return rows[0] if rows else None

    def query(self, namespace, scope, limit=100, include_recovery=False, key=None):
        self._guard(namespace, scope)
        if not 0 < limit <= 100:
            raise ValueError('Bounded query requires limit 1 to 100')
        rows = self.transport.query(namespace, scope, key=key, limit=limit, include_recovery=include_recovery)
        out = []
        for record in rows:
            self._privacy(record['resource_id'])
            row = record['value']
            if row.get('namespace') != namespace or row.get('scope') != scope:
                raise PrivacyError('Remote scope mismatch')
            if key is not None and row.get('id') != key:
                raise RemoteError('Remote key mismatch')
            if row.get('status') == 'active' or (include_recovery and row.get('status') == 'recovery'):
                out.append(row)
        return out[:limit]

    def put(self, namespace, scope, key, value):
        self._guard(namespace, scope)
        if (value.get('namespace'), value.get('scope'), value.get('id')) != (namespace, scope, key):
            raise PrivacyError('Document identity and route disagree')
        if value.get('status') not in {'active', 'recovery'}:
            raise ValueError('Lifecycle status required')
        if value.get('status') == 'recovery':
            end = dt.datetime.fromisoformat(value['recovery_until'])
            if end.tzinfo is None:
                raise ValueError('Timezone required')
        if len(json.dumps(value, ensure_ascii=False)) > self.config.get('max_document_chars', 16000):
            raise ValueError('Document exceeds retrieval size bound')
        # Verify existing resources independently before updating them.
        existing = self.transport.query(namespace, scope, key=key, limit=2, include_recovery=True)
        if len(existing) > 1:
            raise RemoteError('Duplicate remote key requires reconciliation')
        for row in existing:
            self._privacy(row['resource_id'])
            old = row['value']
            if (old.get('namespace'), old.get('scope'), old.get('id')) != (namespace, scope, key):
                raise PrivacyError('Existing remote document has a different route')
        self._privacy()
        receipt = self.transport.put(namespace, scope, key, value)
        self._privacy(receipt['resource_id'])
        stored = self.get(namespace, scope, key, include_recovery=True)
        if stored != value:
            raise RemoteError('Remote write readback failed')
        return receipt

    def archive(self, namespace, scope, key):
        row = self.get(namespace, scope, key)
        if row is None:
            raise KeyError(key)
        row.update(status='recovery', recovery_until=(self.now()+dt.timedelta(days=90)).isoformat())
        return self.put(namespace, scope, key, row)

    def recover(self, namespace, scope, key):
        row = self.get(namespace, scope, key, include_recovery=True)
        if not row or row['status'] != 'recovery' or dt.datetime.fromisoformat(row['recovery_until']) <= self.now():
            raise ValueError('Live recovery window required')
        row.update(status='active', recovery_until=None, ask_before_forgetting=True)
        return self.put(namespace, scope, key, row)

    def delete(self, namespace, scope, key, recovery_only=True):
        self._guard(namespace, scope)
        if recovery_only is not True:
            raise PrivacyError('Only expired recovery copies are eligible')
        row = self.get(namespace, scope, key, include_recovery=True)
        if not row or row.get('kind') == 'protected_preference' or row.get('status') != 'recovery':
            raise PrivacyError('Active and protected memories cannot be deleted')
        if dt.datetime.fromisoformat(row['recovery_until']) > self.now():
            raise PrivacyError('Recovery window still protected')
        self._privacy()
        return self.transport.delete(namespace, scope, key)


class NtnTransport:
    """Existing ntn auth. Schema: Name title, Payload rich_text, Status select.

    Requests never log arguments, response bodies, credentials or content.
    This transport is unusable without NotionStore's independent ACL verifier.
    """
    def __init__(self, config):
        self.config = config

    def _api(self, method, path, body=None):
        command = ['ntn', 'api', path, '-X', method]
        if body is not None:
            command += ['-d', '@-']
        try:
            result = subprocess.run(command, input=json.dumps(body) if body is not None else None,
                                    capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteError('Notion CLI unavailable or timed out') from exc
        if result.returncode:
            raise RemoteError('Notion CLI request failed; checkpoint must not advance')
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise RemoteError('Notion CLI response was not JSON') from exc

    @staticmethod
    def _prefix(namespace, scope):
        return hashlib.sha256((namespace+'\0'+scope).encode()).hexdigest() + ':'

    def _name(self, namespace, scope, key):
        return self._prefix(namespace, scope) + hashlib.sha256(key.encode()).hexdigest()

    def query(self, namespace, scope, key=None, limit=100, include_recovery=False):
        title = {'equals': self._name(namespace, scope, key)} if key is not None else {'starts_with': self._prefix(namespace, scope)}
        filters = [{'property': 'Name', 'title': title}]
        if not include_recovery:
            filters.append({'property': 'Status', 'select': {'equals': 'active'}})
        response = self._api('POST', 'v1/data_sources/'+self.config['data_source_id']+'/query',
                             {'filter': {'and': filters}, 'page_size': limit})
        if response.get('has_more'):
            raise RemoteError('Query exceeded bounded result budget; narrow the scope')
        return [{'resource_id': page['id'], 'value': json.loads(''.join(
            part['plain_text'] for part in page['properties']['Payload']['rich_text']))}
                for page in response['results']]

    def put(self, namespace, scope, key, value):
        rows = self.query(namespace, scope, key, 2, include_recovery=True)
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True)
        properties = {'Name': {'title': [{'text': {'content': self._name(namespace,scope,key)}}]},
                      'Status': {'select': {'name': value['status']}},
                      'Payload': {'rich_text': [{'text': {'content': payload[i:i+1800]}} for i in range(0,len(payload),1800)]}}
        if rows:
            result = self._api('PATCH', 'v1/pages/'+rows[0]['resource_id'], {'properties': properties})
        else:
            result = self._api('POST', 'v1/pages', {'parent': {'data_source_id': self.config['data_source_id']}, 'properties': properties})
        return {'resource_id': result['id'], 'durable': True}

    def delete(self, namespace, scope, key):
        rows = self.query(namespace,scope,key,2,include_recovery=True)
        if len(rows) != 1:
            raise RemoteError('Exactly one recovery record required')
        result = self._api('PATCH', 'v1/pages/'+rows[0]['resource_id'], {'in_trash': True})
        if result.get('in_trash') is not True:
            raise RemoteError('Remote trash readback failed')
        return {'resource_id': result['id'], 'in_trash': True, 'permanent_deletion': False}


class FindingSink:
    """Only explicitly routed useful findings, never raw session records."""
    def __init__(self, store, routes, extractor=None):
        self.store, self.routes, self.extractor = store, routes, extractor

    @property
    def verified_private(self):
        return self.store.verified_private

    def __call__(self, batch):
        # A reviewed extractor is responsible for per-customer isolation. No default
        # extraction of raw records and no fallback to a common workspace scope.
        if not self.verified_private or not callable(self.extractor):
            return False
        route = self.routes.get(batch.session_id)
        if not route or route.get('isolated') is not True:
            return False
        finding = self.extractor(batch, route)
        if (not finding or finding.get('mixed_scope') is not False
                or finding.get('scope') != route['scope']
                or finding.get('namespace') != route['namespace']
                or not isinstance(finding.get('body'), str) or not finding['body']
                or not isinstance(finding.get('provenance'), list) or not finding['provenance']):
            return False
        namespace, scope = route['namespace'], route['scope']
        key = 'finding-' + batch.idempotency_key
        # Acknowledged writes can replay if a process exits before checkpointing.
        existing = self.store.get(namespace, scope, key, include_recovery=True)
        if existing:
            return existing.get('body') == finding['body'] and existing.get('provenance') == finding['provenance']
        row = {'id':key,'namespace':namespace,'scope':scope,'kind':'lesson',
               'body':finding['body'],'provenance':finding['provenance'],
               'created_at':utcnow().isoformat(),'updated_at':utcnow().isoformat(),
               'status':'active','recovery_until':None}
        return self.store.put(namespace, scope, key, row).get('durable') is True


class ReportSink:
    """Deliver the private internal pilot report, with explicit routing and readback."""
    def __init__(self, store, namespace, scope):
        self.store, self.namespace, self.scope = store, namespace, scope

    @property
    def verified_private(self):
        return self.store.verified_private

    def __call__(self, report):
        if (not self.verified_private or self.namespace not in {'work','personal'}
                or not self.scope or not isinstance(report,dict)
                or report.get('kind') != 'workspace-renewal-pilot-report'
                or not report.get('idempotency_key') or not report.get('started_at')):
            return False
        costs=report.get('costs',{})
        actual=costs.get('actual_charges')
        estimate=costs.get('pricing_estimate')
        def shown(value):
            return 'unavailable' if value is None else str(value)
        lines=['# Workspace renewal pilot report','',
               'Started: '+report['started_at'],
               'Due: '+str(report.get('due_at','unavailable')),
               'Observed days: '+str(report.get('observed_days',0)),
               'Measured 14 days: '+str(report.get('measured_14_days',False)).lower(),
               'Full measurement 14 days: '+str(report.get('full_measurement_14_days',False)).lower(),
               'Completed outcomes: '+str(report.get('completed_outcomes',0)),
               'Useful changes: '+str(report.get('useful_changes',0)),
               'Regressions: '+str(report.get('regressions',0)),
               'Rollbacks: '+str(report.get('rollbacks',0)),
               'Backlog days: '+str(report.get('backlog_days',0)),
               'Actual charges: '+shown(actual),
               'Price estimate: '+shown(estimate),
               'Average observed day cost: '+shown(report.get('average_observed_day_cost')),
               'Recommended ongoing budget: '+str(report.get('recommended_ongoing_budget') or 'OPEN'),
               '', '## Measurement detail','',json.dumps(report,indent=2,ensure_ascii=False)]
        key='pilot-report-'+hashlib.sha256(report['idempotency_key'].encode()).hexdigest()
        now=utcnow().isoformat()
        row={'id':key,'namespace':self.namespace,'scope':self.scope,'kind':'project',
             'body':'\n'.join(lines),'provenance':['pilot:'+report['started_at']],
             'created_at':report['started_at'],'updated_at':now,
             'status':'active','recovery_until':None}
        try:
            return self.store.put(self.namespace,self.scope,key,row).get('durable') is True
        except (PrivacyError,RemoteError,OSError,ValueError,KeyError):
            return False


def create(config):
    """Safe runtime factory until a supported live ACL provider is installed.

    Config cannot assert verified_private or load arbitrary verifier code.
    External code may construct NotionStore with an independently verified API.
    """
    notion = config.get('notion', config)
    store = NotionStore(notion, NtnTransport(notion))
    return {'sink': FindingSink(store, notion.get('session_routes', {})),
            'report_sink': ReportSink(store,notion.get('pilot_namespace'),notion.get('pilot_scope')),
            'store': store}
