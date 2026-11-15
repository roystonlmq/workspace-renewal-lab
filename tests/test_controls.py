import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from renewal.controls import ChangeGate, Rejected, atomic_write


class ControlsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'skills').mkdir()
        (self.root / 'skills/example.md').write_text('original')
        self.gate = ChangeGate(self.root, self.root / 'state', active=lambda: False)

    def test_apply_requires_independent_passing_checks(self):
        with self.assertRaises(Rejected):
            self.gate.apply('skills/example.md', 'candidate', lambda p: False)
        self.assertEqual((self.root / 'skills/example.md').read_text(), 'original')
        receipt = self.gate.apply('skills/example.md', 'candidate', lambda p: p.read_text() == 'candidate')
        self.assertEqual((self.root / 'skills/example.md').read_text(), 'candidate')
        self.gate.rollback(receipt)
        self.assertEqual((self.root / 'skills/example.md').read_text(), 'original')

    def test_protected_and_traversal_rejected(self):
        for path in ('../escape', '/tmp/escape', 'evals/heldout.json', 'policy.json', 'skills/../../policy.json', 'skills/spending.json'):
            with self.subTest(path=path), self.assertRaises(Rejected):
                self.gate.apply(path, 'changed', lambda p: True)

    def test_concurrent_edits_survive_rollback_and_apply(self):
        receipt = self.gate.apply('skills/example.md', 'candidate', lambda p: True)
        (self.root / 'skills/example.md').write_text('someone else')
        with self.assertRaises(Rejected):
            self.gate.rollback(receipt)
        self.assertEqual((self.root / 'skills/example.md').read_text(), 'someone else')

    def test_active_runs_and_symlinks_rejected(self):
        gate = ChangeGate(self.root, self.root / 'state', active=lambda: True)
        with self.assertRaises(Rejected):
            gate.apply('skills/example.md', 'candidate', lambda p: True)
        (self.root / 'skills/link.md').symlink_to(self.root / 'skills/example.md')
        with self.assertRaises(Rejected):
            self.gate.apply('skills/link.md', 'candidate', lambda p: True)

    def test_callback_cannot_mutate_original(self):
        def checks(candidate):
            (self.root / 'skills/example.md').write_text('concurrent')
            return True
        with self.assertRaises(Rejected):
            self.gate.apply('skills/example.md', 'candidate', checks)
        self.assertEqual((self.root / 'skills/example.md').read_text(), 'concurrent')

    def test_unknown_activity_and_empty_path_are_rejected(self):
        with self.assertRaises(Rejected):
            ChangeGate(self.root,self.root/'state').apply('skills/example.md','candidate',lambda p:True)
        with self.assertRaises(Rejected):
            self.gate.apply('','candidate',lambda p:True)

    def test_executable_mode_survives_change_and_rollback(self):
        target=self.root/'skills/example.md'
        target.chmod(0o755)
        receipt=self.gate.apply('skills/example.md','candidate',lambda p:True)
        self.assertEqual(target.stat().st_mode & 0o777,0o755)
        self.gate.rollback(receipt)
        self.assertEqual(target.stat().st_mode & 0o777,0o755)

    def test_failed_applied_receipt_restores_original_and_mode(self):
        target = self.root / 'skills/example.md'
        target.chmod(0o755)
        def failing_result(path, data):
            if Path(path).name == 'result.json':
                raise OSError('synthetic receipt failure')
            atomic_write(path, data)
        with patch('renewal.controls.atomic_write', side_effect=failing_result):
            with self.assertRaises(Rejected):
                self.gate.apply('skills/example.md','candidate',lambda p:True)
        self.assertEqual(target.read_text(),'original')
        self.assertEqual(target.stat().st_mode & 0o777,0o755)

    def test_failed_new_file_install_removes_only_own_change(self):
        def failing_result(path, data):
            if Path(path).name == 'result.json':
                raise OSError('synthetic receipt failure')
            atomic_write(path, data)
        with patch('renewal.controls.atomic_write', side_effect=failing_result):
            with self.assertRaises(Rejected):
                self.gate.apply('skills/new.md','candidate',lambda p:True)
        self.assertFalse((self.root / 'skills/new.md').exists())

    def test_install_error_preserves_a_newer_concurrent_edit(self):
        target = self.root / 'skills/example.md'
        def concurrent_failure(path, data):
            if Path(path).name == 'result.json':
                target.write_text('newer edit')
                raise OSError('synthetic receipt failure')
            atomic_write(path, data)
        with patch('renewal.controls.atomic_write', side_effect=concurrent_failure):
            with self.assertRaises(Rejected):
                self.gate.apply('skills/example.md','candidate',lambda p:True)
        self.assertEqual(target.read_text(),'newer edit')
