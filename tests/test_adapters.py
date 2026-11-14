import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renewal.adapters import generate


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.package = Path(self.temp.name) / "package"
        real = Path(__file__).resolve().parents[1]
        shutil.copytree(real / "adapters", self.package / "adapters", symlinks=True,
                        ignore=shutil.ignore_patterns("claude", "codex", "manifest.json"))
        shutil.copytree(real / "renewal", self.package / "renewal", ignore=shutil.ignore_patterns("__pycache__"))
        self.manifest = generate(self.package)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_registered_hook_commands(self):
        for harness, event in [("claude", "PreToolUse"), ("codex", "PostToolUse")]:
            config = self.package / "adapters" / self.manifest["harnesses"][harness]["config"]
            cmd = json.loads(config.read_text())["hooks"][event][0]["hooks"][0]["command"]
            work = self.package / "adapters" / harness
            for path, allowed in [("docs/report.json", True), ("loose-report.json", False)]:
                run = subprocess.run(shlex.split(cmd), cwd=work, input=json.dumps({"tool_input": {"file_path": path}}), text=True, capture_output=True)
                self.assertEqual(run.returncode, 0, run.stderr)
                if allowed:
                    self.assertEqual(run.stdout, "")
                elif harness == "claude":
                    self.assertEqual(json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
                else:
                    self.assertIn("post-write feedback", json.loads(run.stdout)["enforcement"])

    def test_actual_skill_execution_through_both_catalogs(self):
        for harness, catalog in [("claude", ".claude"), ("codex", ".agents")]:
            skill = self.package / "adapters" / harness / catalog / "skills/renewal-fixture"
            work = Path(self.temp.name) / (harness + "-work")
            work.mkdir()
            for path, allowed in [("docs/result.json", True), ("loose-result.json", False)]:
                run = subprocess.run([sys.executable, "scripts/report.py", str(work), path], cwd=skill, capture_output=True, text=True)
                self.assertEqual(run.returncode, 0 if allowed else 1, run.stderr)
                self.assertEqual((work / path).exists(), allowed)
                if allowed:
                    self.assertTrue(json.loads((work / path).read_text())["completed"])
                    self.assertTrue(json.loads((work / path).read_text())["synthetic"])

    def test_generation_idempotent_and_instruction_source(self):
        self.assertEqual(self.manifest, generate(self.package))
        for harness, filename in [("claude", "CLAUDE.md"), ("codex", "AGENTS.md")]:
            self.assertIn((self.package / "adapters/common.md").read_text(), (self.package / "adapters" / harness / filename).read_text())

    def test_external_destination(self):
        destination = Path(self.temp.name) / "external-adapters"
        manifest = generate(self.package, destination)
        self.assertTrue((destination / manifest["harnesses"]["codex"]["config"]).is_file())
