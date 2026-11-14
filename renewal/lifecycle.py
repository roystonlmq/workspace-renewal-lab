"""Scoped remote memory lifecycle. No on-disk memory or read cache."""
import datetime as dt
import hashlib
import json


class MemoryError(ValueError):
    pass


class MemoryService:
    def __init__(self, store, clock=lambda: dt.datetime.now(dt.timezone.utc)):
        self.store, self.clock = store, clock

    def guard(self, namespace, scope):
        if namespace not in {'work', 'personal'} or not scope or '/' in scope or scope in {'*','all'}:
            raise MemoryError('Explicit namespace and customer or project scope required')
        if not self.store.verified_private:
            raise MemoryError('Verified private remote required')

    def remember(self, namespace, scope, key, body, *, kind, provenance, theme='', confidence=1):
        self.guard(namespace, scope)
        if not provenance or not body or kind not in {'preference','project','evidence','lesson'} or key.startswith('__'):
            raise MemoryError('Body, supported kind, provenance and ordinary key required')
        if not 0 <= confidence <= 1:
            raise MemoryError('Invalid confidence')
        if self.store.get(namespace, scope, key, include_recovery=True) is not None:
            raise MemoryError('Existing evidence requires an explicit correction or recovery')
        now = self.clock().isoformat()
        row = {'id':key, 'namespace':namespace, 'scope':scope, 'kind':kind, 'body':body,
               'provenance':list(provenance), 'theme':theme, 'confidence':confidence,
               'created_at':now, 'updated_at':now, 'status':'active', 'recovery_until':None,
               'freshness':'supported', 'ask_before_forgetting':False}
        self.store.put(namespace, scope, key, row)
        return row

    def recall(self, namespace, scope, query, budget_chars=4000):
        self.guard(namespace, scope)
        if budget_chars < 0 or budget_chars > 16000 or not query.strip():
            return []
        terms = query.casefold().split()
        rows = self.store.query(namespace, scope, limit=100, include_recovery=False)
        result, used = [], 0
        # Defence in depth: do not trust a backend's scope or lifecycle filtering.
        for row in sorted(rows, key=lambda r:r.get('updated_at',''), reverse=True):
            if row.get('status') != 'active' or row.get('namespace') != namespace or row.get('scope') != scope or row.get('kind') == 'protected_preference':
                continue
            if not any(t in (row.get('body','')+' '+row.get('theme','')).casefold() for t in terms):
                continue
            size = len(json.dumps(row, ensure_ascii=False))
            if used + size <= budget_chars:
                result.append(row)
                used += size
        return result

    def require(self, namespace, scope, key, recovery=False):
        self.guard(namespace, scope)
        row = self.store.get(namespace, scope, key, include_recovery=recovery)
        if not row or row.get('namespace') != namespace or row.get('scope') != scope:
            raise MemoryError('Scoped memory missing')
        return row

    def correct(self, namespace, scope, key, body, provenance):
        row = self.require(namespace, scope, key)
        if not body or not provenance:
            raise MemoryError('Correction requires evidence')
        now = self.clock()
        revision = dict(row, id=key + '.revision.' + hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()[:16],
                        status='recovery', recovery_until=(now+dt.timedelta(days=90)).isoformat())
        self.store.put(namespace,scope,revision['id'],revision)
        updated = dict(row, body=body, provenance=list(provenance), updated_at=now.isoformat())
        self.store.put(namespace,scope,key,updated)
        return updated

    @staticmethod
    def theme_key(theme):
        return '__theme__' + hashlib.sha256(theme.encode()).hexdigest()[:24]

    def forget(self, namespace, scope, key, *, evidence, confidence, confirmed=False):
        row = self.require(namespace, scope, key)
        theme_rule = self.store.get(namespace,scope,self.theme_key(row.get('theme','')),include_recovery=False)
        if row.get('kind') == 'protected_preference' or not evidence or confidence < .95:
            raise MemoryError('Obsolescence requires high-confidence evidence')
        if any('not retrieved' in e.casefold() or 'lack of retrieval' in e.casefold() for e in evidence):
            raise MemoryError('Non-use alone is not obsolescence evidence')
        if (row.get('ask_before_forgetting') or theme_rule) and not confirmed:
            raise MemoryError('Ask before forgetting a returned theme')
        now = self.clock()
        row.update(status='recovery', updated_at=now.isoformat(),
                   recovery_until=(now+dt.timedelta(days=90)).isoformat(), obsolete_evidence=list(evidence))
        self.store.put(namespace,scope,key,row)
        return row

    def recover(self, namespace, scope, key):
        row = self.require(namespace,scope,key,recovery=True)
        if row.get('status') != 'recovery' or dt.datetime.fromisoformat(row['recovery_until']) <= self.clock():
            raise MemoryError('Recovery window expired or item is active')
        now = self.clock().isoformat()
        # The theme rule is separate and cannot expire with the recovered memory.
        rule = dict(row, id=self.theme_key(row.get('theme','')),kind='protected_preference',
                    body='Ask before forgetting this returned theme',status='active',
                    recovery_until=None,ask_before_forgetting=True,updated_at=now)
        self.store.put(namespace,scope,rule['id'],rule)
        row.update(status='active',recovery_until=None,ask_before_forgetting=True,updated_at=now)
        self.store.put(namespace,scope,key,row)
        return row

    def purge_expired(self, namespace, scope):
        self.guard(namespace,scope)
        count = 0
        for row in self.store.query(namespace,scope,limit=100,include_recovery=True):
            if row.get('namespace') != namespace or row.get('scope') != scope or row.get('kind') == 'protected_preference':
                continue
            if row.get('status') == 'recovery' and dt.datetime.fromisoformat(row['recovery_until']) <= self.clock():
                self.store.delete(namespace,scope,row['id'],recovery_only=True)
                count += 1
        return count
