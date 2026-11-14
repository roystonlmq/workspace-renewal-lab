import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from renewal.ingestion import Ingestor


class HistoryFixtureTests(unittest.TestCase):
    def test_shipped_histories_clear_resume_without_replay(self):
        fixtures = Path(__file__).resolve().parents[1] / 'fixtures'
        now = datetime(2026, 11, 15, 19, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = []
            for source in sorted(fixtures.glob('*.jsonl')):
                path = root / source.name
                shutil.copyfile(source, path)
                os.utime(path, (now.timestamp() - 3600,) * 2)
                paths.append(path)
            batches = []
            def accept(batch):
                batches.append(batch)
                return True
            runner = Ingestor(root / 'checkpoint.json')
            result = runner.process(paths, accept, now=now)
            self.assertEqual(result['processed_sessions'], 2)
            self.assertEqual(result['batches'], 4)
            self.assertEqual(len({b.session_id for b in batches}), 2)
            self.assertEqual(len({b.segment_id for b in batches}), 4)
            result = Ingestor(root / 'checkpoint.json').process(paths, accept, now=now)
            self.assertEqual(result['processed_sessions'], 0)
            self.assertEqual(len(batches), 4)
            state = (root / 'checkpoint.json').read_text()
            self.assertNotIn('prefer concise', state)
            self.assertNotIn('synthetic-a', state)

