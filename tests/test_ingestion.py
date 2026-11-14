import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from renewal.ingestion import Ingestor


NOW = datetime(2026, 11, 15, 19, tzinfo=timezone.utc)


def write_lines(path, records, tail=b''):
    path.write_bytes(b''.join(json.dumps(r).encode() + b'\n' for r in records) + tail)
    os.utime(path, (NOW.timestamp() - 3600,) * 2)


def claude(key, text='synthetic', **extra):
    return dict(type='user', sessionId='synthetic-session', uuid=key,
                timestamp='2026-11-14T10:00:00Z', message={'content': text}, **extra)


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'claude.jsonl'
        self.ingestor = Ingestor(self.root / 'state.json')
        self.batches = []

    def accept(self, batch):
        self.batches.append(batch)
        return True

    def test_append_tail_and_restart_deliver_only_unacknowledged_records(self):
        write_lines(self.path, [claude('a')], b'{"partial":')
        self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW)['processed_sessions'], 1)
        first = self.batches[0]
        self.assertEqual(first.records[0].start_offset, 0)
        self.assertEqual(first.records[0].end_offset, self.path.read_bytes().index(b'\n') + 1)
        write_lines(self.path, [claude('a'), claude('b')])
        self.ingestor = Ingestor(self.root / 'state.json')
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual([r.data['uuid'] for b in self.batches for r in b.records], ['a', 'b'])
        self.assertEqual(first.segment_id, self.batches[1].segment_id)
        self.assertNotIn('synthetic', (self.root / 'state.json').read_text())

    def test_failed_sink_never_advances_checkpoint(self):
        write_lines(self.path, [claude('a')])
        result = self.ingestor.process([self.path], lambda b: False, now=NOW)
        self.assertEqual(result['processed_sessions'], 0)
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual(len(self.batches), 1)

    def test_truncate_rewrite_and_same_mtime_changes_are_detected(self):
        write_lines(self.path, [claude('a'), claude('b')])
        self.ingestor.process([self.path], self.accept, now=NOW)
        write_lines(self.path, [claude('a', 'changed')])
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual(self.batches[-1].records[0].data['message']['content'], 'changed')
        self.assertEqual(self.batches[0].session_id, self.batches[-1].session_id)
        self.assertNotEqual(self.batches[0].content_hash, self.batches[-1].content_hash)

    def test_clear_splits_scope_and_codex_resumes_stable_session(self):
        records = [dict(type='session_meta', payload={'id': 'codex-synthetic'}, timestamp='2026-11-14T10:00:00Z'),
                   dict(type='response_item', payload={'id': 'a', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hello'}]}),
                   dict(type='event_msg', payload={'type': 'context_reset'}),
                   dict(type='response_item', payload={'id': 'b', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'resumed'}]})]
        write_lines(self.path, records)
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual(len(self.batches), 2)
        self.assertEqual({b.harness for b in self.batches}, {'codex'})
        self.assertNotEqual(self.batches[0].segment_id, self.batches[1].segment_id)
        self.assertEqual(self.batches[0].session_id, self.batches[1].session_id)
        self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW)['processed_sessions'], 0)

    def test_active_recent_recursive_old_and_malformed_are_excluded(self):
        cases = [([claude('a')], {'active_sessions': {'synthetic-session'}}),
                 ([claude('a', dream_generated=True)], {}),
                 ([dict(claude('a'), timestamp='2026-08-01T00:00:00Z')], {})]
        for records, options in cases:
            write_lines(self.path, records)
            self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW, **options)['processed_sessions'], 0)
        write_lines(self.path, [claude('a')])
        os.utime(self.path, (NOW.timestamp(),) * 2)
        self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW)['processed_sessions'], 0)
        write_lines(self.path, [])
        self.path.write_bytes(b'not-json\n')
        os.utime(self.path, (NOW.timestamp() - 3600,) * 2)
        self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW)['failures'], ['invalid_jsonl'])

    def test_initial_window_excludes_old_records_inside_recent_session(self):
        old = dict(claude('old'), timestamp='2026-08-01T00:00:00Z')
        write_lines(self.path, [old, claude('new')])
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual([r.data['uuid'] for b in self.batches for r in b.records], ['new'])
        self.assertEqual(self.ingestor.process([self.path], self.accept, now=NOW)['processed_sessions'], 0)

    def test_sink_exception_replays_and_zero_limit_has_no_side_effects(self):
        write_lines(self.path, [claude('a')])
        def fail(batch):
            raise OSError('synthetic transport failure')
        self.assertEqual(self.ingestor.process([self.path], fail, now=NOW)['failures'], ['sink_write_failed'])
        self.ingestor.process([self.path], self.accept, now=NOW, limit=0)
        self.assertEqual(self.batches, [])
        self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual(len(self.batches), 1)

    def test_mixed_explicit_session_ids_are_rejected_before_any_sink_write(self):
        write_lines(self.path, [claude('a'), dict(claude('b'), sessionId='other-synthetic')])
        result = self.ingestor.process([self.path], self.accept, now=NOW)
        self.assertEqual(result['failures'], ['mixed_session_ids'])
        self.assertEqual(self.batches, [])


if __name__ == '__main__':
    unittest.main()
