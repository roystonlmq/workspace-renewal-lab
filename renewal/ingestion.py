"""Incremental, content-addressed ingestion. Persist metadata, never transcripts."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix='.renewal-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError('invalid operational state')
    return value


def _timestamp(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _clear(record: dict) -> bool:
    payload = record.get('payload', {})
    message = record.get('message', {})
    if record.get('subtype') in ('clear', 'compact_boundary'):
        return True
    if isinstance(payload, dict) and payload.get('type') in ('context_reset', 'clear', 'session_clear'):
        return True
    content = message.get('content') if isinstance(message, dict) else None
    if content == '/clear':
        return True
    if isinstance(payload, dict):
        content = payload.get('content', content)
    return isinstance(content, list) and any(
        isinstance(item, dict) and item.get('text') == '/clear' for item in content)


def _recursive(record: dict) -> bool:
    # Explicit producer metadata only. User discussion of dreaming is legitimate input.
    containers = [record, record.get('payload', {}), record.get('metadata', {})]
    for value in containers:
        if not isinstance(value, dict):
            continue
        if value.get('dream_generated') is True or value.get('renewal_generated') is True:
            return True
        if value.get('origin') in ('workspace-renewal-nightly', 'renewal-nightly', 'dream'):
            return True
        if value.get('source') in ('workspace-renewal-nightly', 'renewal-nightly', 'dream'):
            return True
    return False


@dataclass(frozen=True)
class SessionRecord:
    key: str
    content_hash: str
    start_offset: int
    end_offset: int
    data: dict


@dataclass(frozen=True)
class SessionBatch:
    harness: str
    session_id: str
    segment_id: str
    content_hash: str
    source_id: str
    records: tuple[SessionRecord, ...]

    @property
    def idempotency_key(self) -> str:
        return digest(self.session_id + self.segment_id + self.content_hash)


class Ingestor:
    """Read one isolated session segment at a time into an authorized sink.

    The sink must use idempotency_key: a crash after its write and before local
    checkpointing can replay a batch. No raw paths, session IDs or content persist.
    Caller serializes writers; NightlyRunner supplies the cross-process lock.
    """
    def __init__(self, state_path: Path, *, settle_seconds: int = 300,
                 max_file_bytes: int = 64 * 1024 * 1024):
        self.state_path = Path(state_path)
        self.settle_seconds = settle_seconds
        self.max_file_bytes = max_file_bytes

    def process(self, paths: Iterable[Path], sink: Callable[[SessionBatch], bool], *,
                now: datetime, active_sessions: set[str] | None = None,
                limit: int = 20, on_session: Callable[[str], bool] | None = None) -> dict:
        if now.tzinfo is None:
            raise ValueError('aware time required')
        if not 0 <= limit <= 20:
            raise ValueError('session bound must be between zero and twenty')
        state = load_json(self.state_path)
        checkpoints = state.setdefault('segments', {})
        known = state.setdefault('sources', {})
        result = dict(processed_sessions=0, attempted_sessions=0, batches=0,
                      skipped=0, failures=[], backlog=False)
        active_sessions = active_sessions or set()
        attempted = set()
        for path in sorted(set(map(Path, paths)), key=str):
            try:
                before = path.stat()
                if now.timestamp() - before.st_mtime < self.settle_seconds:
                    result['skipped'] += 1
                    continue
                if before.st_size > self.max_file_bytes:
                    result['failures'].append('file_size_bound')
                    continue
                raw = path.read_bytes()
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                    result['skipped'] += 1
                    continue
                source_id = digest(str(path.resolve()))
                rows = []
                offset = 0
                for line in raw.splitlines(keepends=True):
                    end = offset + len(line)
                    if not line.endswith(b'\n'):
                        break  # A partial final JSON object is retried after completion.
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('invalid row')
                    rows.append((offset, end, line, value))
                    offset = end
                if not rows:
                    continue
                if any(_recursive(row[3]) for row in rows):
                    result['skipped'] += 1
                    continue
                harness = 'codex' if any(r[3].get('type') == 'session_meta' for r in rows) else 'claude'
                external_id = None
                for _, _, _, row in rows:
                    if row.get('type') == 'session_meta':
                        external_id = row.get('payload', {}).get('id')
                        break
                    if row.get('sessionId'):
                        external_id = row['sessionId']
                        break
                external_id = str(external_id or path.stem)
                explicit_ids = {str(row[3]['sessionId']) for row in rows if row[3].get('sessionId')}
                explicit_ids.update(str(row[3]['payload']['id']) for row in rows
                    if row[3].get('type') == 'session_meta' and row[3].get('payload', {}).get('id'))
                if len(explicit_ids) > 1:
                    result['failures'].append('mixed_session_ids')
                    continue
                session_id = digest(harness + ':' + external_id)
                if external_id in active_sessions or session_id in active_sessions or str(path) in active_sessions:
                    result['skipped'] += 1
                    continue
                timestamps = [_timestamp(r[3].get('timestamp')) for r in rows]
                newest = max((t for t in timestamps if t), default=datetime.fromtimestamp(before.st_mtime, timezone.utc))
                if source_id not in known and newest < now - timedelta(days=30):
                    result['skipped'] += 1
                    continue
                segments = defaultdict(list)
                segment = digest(session_id + ':initial')
                occurrences = defaultdict(int)
                cutoff_text = known.get(source_id, {}).get('initial_cutoff')
                cutoff = _timestamp(cutoff_text) or now - timedelta(days=30)
                record_time = datetime.fromtimestamp(before.st_mtime, timezone.utc)
                for start, end, line, row in rows:
                    content_hash = digest(line.rstrip(b'\r\n'))
                    payload = row.get('payload', {})
                    stable = row.get('uuid') or (payload.get('id') if isinstance(payload, dict) else None)
                    identity = str(stable) if stable else content_hash
                    occurrences[identity] += 1
                    key = digest(identity + ':' + str(occurrences[identity]))
                    if _clear(row):
                        segment = digest(session_id + ':clear:' + key)
                    record_time = _timestamp(row.get('timestamp')) or record_time
                    if record_time < cutoff and key not in checkpoints.get(segment, {}).get('records', {}):
                        continue
                    segments[segment].append(SessionRecord(key, content_hash, start, end, row))
                pending = []
                for segment_id, records in segments.items():
                    previous = checkpoints.get(segment_id, {}).get('records', {})
                    changed = tuple(r for r in records if previous.get(r.key, {}).get('hash') != r.content_hash)
                    if changed:
                        pending.append(SessionBatch(harness, session_id, segment_id,
                            digest(''.join(r.key + r.content_hash for r in changed)), source_id, changed))
                if not pending:
                    continue
                if session_id not in attempted:
                    if len(attempted) >= limit:
                        result['backlog'] = True
                        continue
                    if on_session is not None and on_session(session_id) is not True:
                        result['backlog'] = True
                        continue
                    attempted.add(session_id)
                    result['attempted_sessions'] += 1
                succeeded = True
                for batch in pending:
                    try:
                        accepted = sink(batch) is True
                    except Exception:
                        accepted = False
                    if not accepted:
                        result['failures'].append('sink_write_failed')
                        succeeded = False
                        break
                    checkpoint = checkpoints.setdefault(batch.segment_id, {'records': {}})
                    for record in batch.records:
                        checkpoint['records'][record.key] = dict(hash=record.content_hash,
                            start=record.start_offset, end=record.end_offset)
                    checkpoint['session_id'] = session_id
                    checkpoint['source_id'] = source_id
                    known[source_id] = dict(complete_offset=offset, prefix_hash=digest(raw[:offset]),
                                            initial_cutoff=cutoff.isoformat())
                    atomic_json(self.state_path, state)
                    result['batches'] += 1
                if succeeded:
                    result['processed_sessions'] += 1
            except (OSError, ValueError, TypeError, AttributeError):
                result['failures'].append('invalid_jsonl')
        return result
