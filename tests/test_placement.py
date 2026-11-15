import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renewal.placement import check, paths_from_payload, workspace_check


class PlacementTests(unittest.TestCase):
    def test_workspace_check_preserves_code_and_existing_root_files(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root/'AGENTS.md').write_text('existing')
            for path in ['tools/demo/main.py', '.claude/settings.json', 'AGENTS.md', 'docs/guides/a.md', 'customers/sample/demo/a.md']:
                self.assertTrue(workspace_check(root, path, {'sample'})['allowed'], path)
            for path in ['loose-report.md', 'docs/sample/a.md', '../escape']:
                self.assertFalse(workspace_check(root, path, {'sample'})['allowed'], path)
            self.assertEqual(workspace_check(root, 'docs/sample/a.md', {'sample'})['suggestion'], 'customers/sample/<project>/a.md')
    def test_customer_scope_and_escape(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            self.assertTrue(check(root, "customers/synthetic/demo/report.json", "customer", "synthetic", "demo")["allowed"])
            self.assertFalse(check(root, "customers/other/demo/report.json", "customer", "synthetic", "demo")["allowed"])
            self.assertFalse(check(root, "docs/customer/report.json", "customer", "synthetic", "demo")["allowed"])
            self.assertFalse(check(root, "docs/../../escape")["allowed"])
            (root / "docs").symlink_to(root.parent)
            self.assertFalse(check(root, "docs/escape")["allowed"])

    def test_categories(self):
        root = Path("/tmp/synthetic-placement")
        self.assertTrue(check(root, "tmp/workspace-renewal/a", "scratch")["allowed"])
        self.assertFalse(check(root, "tmp/a", "scratch")["allowed"])
        self.assertTrue(check(root, "tools/demo/a.py", "code", owning_repo="tools/demo")["allowed"])
        self.assertFalse(check(root, "a.py", "code")["allowed"])

    def test_post_write_uses_frozen_legacy_exceptions(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t)
            (root/'new-report.md').write_text('already written')
            self.assertFalse(workspace_check(root, 'new-report.md', set(), {'AGENTS.md'})['allowed'])
            self.assertTrue(workspace_check(root, 'AGENTS.md', set(), {'AGENTS.md'})['allowed'])

    def test_patch_paths(self):
        self.assertEqual(paths_from_payload({"tool_input": {"patch": "*** Add File: docs/a\n+x\n*** Update File: other\n"}}), ["docs/a", "other"])

    def test_quiet_cli_and_rejection(self):
        script = Path(__file__).resolve().parents[1] / "renewal/placement.py"
        ok = subprocess.run([sys.executable, str(script), "--root", "/tmp/demo", "docs/a"], capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout), (0, ""))
        bad = subprocess.run([sys.executable, str(script), "--root", "/tmp/demo", "a"], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("docs", bad.stdout)
        workspace = subprocess.run([sys.executable, str(script), '--root', '/tmp/demo',
                                    '--kind', 'workspace', 'tools/demo/main.py'], capture_output=True, text=True)
        self.assertEqual((workspace.returncode, workspace.stdout), (0, ''))
