"""Generate isolated harness adapters, without rewriting live workspace configuration."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shlex
import sys


def generate(package: Path, destination: Path | None = None) -> dict:
    package = package.resolve()
    destination = destination or package / "adapters"
    common = package / "adapters/common.md"
    skill = package / "adapters/skills/renewal-fixture"
    if not common.is_file() or not (skill / "SKILL.md").is_file():
        raise ValueError("Common instructions and canonical skill source are required")
    generated = {}
    for harness in ("claude", "codex"):
        directory = destination / harness
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("CLAUDE.md" if harness == "claude" else "AGENTS.md")
        text = "<!-- Generated from adapters/common.md. Edit the source and regenerate. -->\n" + common.read_text()
        text += "\nHarness behavior: " + ("Placement can deny declared file tools at PreToolUse.\n" if harness == "claude" else "Placement feedback runs after writes. Run the preflight command before declared writes.\n")
        target.write_text(text)
        if harness == "codex":
            link = directory / "CLAUDE.md"
            if not link.exists() and not link.is_symlink():
                link.symlink_to("AGENTS.md")
        source_dir = directory / (".claude" if harness == "claude" else ".agents") / "skills"
        source_dir.mkdir(parents=True, exist_ok=True)
        link = source_dir / "renewal-fixture"
        expected = skill.resolve()
        if link.is_symlink():
            if link.resolve() != expected:
                raise ValueError("Refusing to replace a foreign skill link")
        elif link.exists():
            raise ValueError("Refusing to replace an existing skill directory")
        else:
            import os
            link.symlink_to(os.path.relpath(skill, link.parent), target_is_directory=True)
        import os
        hook_script = directory / "placement-hook.py"
        expected_hook = package / "renewal/placement.py"
        if hook_script.is_symlink():
            if hook_script.resolve() != expected_hook:
                raise ValueError("Refusing to replace a foreign hook link")
        elif hook_script.exists():
            raise ValueError("Refusing to replace an existing hook file")
        else:
            hook_script.symlink_to(os.path.relpath(expected_hook, directory))
        command = shlex.join(["python3", "./placement-hook.py", "--root", ".", "--harness", harness])
        event = "PreToolUse" if harness == "claude" else "PostToolUse"
        config = {"hooks": {event: [{"matcher": "Write|Edit|apply_patch", "hooks": [{"type": "command", "command": command, "timeout": 5}]}]}}
        config_file = directory / (".claude/settings.json" if harness == "claude" else ".codex/hooks.json")
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config, indent=2) + "\n")
        generated[harness] = {"instructions_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "event": event, "config": str(config_file.relative_to(destination)),
            "status": "generated, not installed into active workspace", "full_harness_launch": "not established by generation"}
    manifest = {"common_sha256": hashlib.sha256(common.read_bytes()).hexdigest(), "harnesses": generated,
                "unsupported": ["Codex pre-write blocking is not established by observed configuration or CLI help", "Shell writes and unrecognized tool payloads require explicit preflight"]}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("package", type=Path)
    args = p.parse_args()
    print(json.dumps(generate(args.package)))
