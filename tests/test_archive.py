import datetime as dt
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renewal.archive import create, restore, verify, record_use, deletion_eligibility


class ArchiveTests(unittest.TestCase):
    def test_lossless_restore_and_reset(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = root / "component"
            source.mkdir()
            (source / "original.bin").write_bytes(bytes(range(256)))
            (source / "empty").mkdir()
            (source / "link").symlink_to("original.bin")
            archive = root / "archives/component.tar.gz"
            manifest = create(source, archive, "2026-01-01T00:00:00+00:00")
            self.assertTrue(source.exists())
            self.assertEqual(verify(archive)["archive_sha256"], manifest["archive_sha256"])
            target = restore(archive, root / "restored", "2026-02-01T00:00:00+00:00")
            self.assertEqual((target / "original.bin").read_bytes(), bytes(range(256)))
            self.assertTrue((target / "link").is_symlink())
            decision = deletion_eligibility(verify(archive), dt.datetime(2026, 2, 2, tzinfo=dt.timezone.utc), storage_verified=True, dependencies_clear=True, exceptions_clear=True, use_evidence_complete=True)
            self.assertFalse(decision["eligible"])
            self.assertIn("30 day recovery window not complete", decision["reasons"])
            used = record_use(archive, "2026-03-01T00:00:00+00:00")
            self.assertEqual(used["last_use"], "2026-03-01T00:00:00+00:00")

    def test_tamper_and_unsafe_source(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            source = root / "a"
            source.mkdir()
            (source / "outside").symlink_to("/etc/passwd")
            with self.assertRaises(ValueError):
                create(source, root / "a.tar.gz")
            (source / "outside").unlink()
            (source / "a").write_text("a")
            archive = root / "a.tar.gz"
            create(source, archive)
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaises(ValueError):
                verify(archive)

    def test_unknown_use_and_dirty_work_block_eligibility(self):
        manifest = {"created_at": "2026-01-01T00:00:00+00:00"}
        r = deletion_eligibility(manifest, dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc), dirty=True, unpushed=True)
        self.assertFalse(r["eligible"])
        self.assertIn("use evidence incomplete", r["reasons"])
        self.assertFalse(r["deletion_performed"])
