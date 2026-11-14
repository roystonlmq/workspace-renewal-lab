import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from renewal.audit import config_metadata, inventory


class AuditTests(unittest.TestCase):
    def test_no_secret_config_values(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config.json"
            p.write_text(json.dumps({"mcpServers": {"synthetic": {"env": {"SECRET": "never-return-me"}}}, "hooks": {"Stop": [{"command": "secret-embedded"}]}}))
            result = config_metadata(p)
            text = json.dumps(result)
            self.assertNotIn("never-return-me", text)
            self.assertNotIn("secret-embedded", text)
            self.assertEqual(result["mcp_names"], ["synthetic"])

    def test_metadata_only_and_unknown_use(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as h:
            root = Path(t)
            (root / "AGENTS.md").write_text("PRIVATE-SYNTHETIC-CONTENT")
            (root / "data.json").write_text("CUSTOMER-SECRET")
            result = inventory(root, Path(h))
            self.assertNotIn("PRIVATE-SYNTHETIC-CONTENT", json.dumps(result))
            self.assertNotIn("CUSTOMER-SECRET", json.dumps(result))
            self.assertIsNone(result["context_measurement"]["startup_tokens"])
            self.assertEqual(result["use_evidence"]["execution_telemetry"], "unknown")

    def test_toml_server_names_without_values(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config.toml"
            p.write_text('[mcp_servers.synthetic]\ncommand = "never-return-command"\n[mcp_servers.synthetic.env]\nSECRET = "never-return-secret"\n')
            result = config_metadata(p)
            self.assertEqual(result["mcp_names"], ["synthetic"])
            self.assertNotIn("never-return", json.dumps(result))

    def test_linked_skill_source_is_inventoried(self):
        with tempfile.TemporaryDirectory() as t, tempfile.TemporaryDirectory() as h:
            root = Path(t)
            source = root / "canonical"
            source.mkdir()
            (source / "SKILL.md").write_text("synthetic skill")
            catalog = root / ".claude/skills"
            catalog.mkdir(parents=True)
            (catalog / "synthetic").symlink_to(source, target_is_directory=True)
            result = inventory(root, Path(h))
            self.assertTrue(any(s["path"] == ".claude/skills/synthetic/SKILL.md" for s in result["skills"]))
