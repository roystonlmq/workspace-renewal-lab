"""Private metadata inventory. Never include configuration values or session text."""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import subprocess
import time


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args, cwd=None, timeout=40):
    try:
        p = subprocess.run(args, cwd=cwd, capture_output=True, timeout=timeout)
        return {"returncode": p.returncode, "stdout": p.stdout.decode(errors="replace")}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"returncode": None, "error": type(e).__name__, "stdout": ""}


def metadata(path: Path, root: Path):
    st = path.stat()
    return {"path": os.path.relpath(path, root), "bytes": st.st_size,
            "mtime": st.st_mtime, "symlink": path.is_symlink()}


def config_metadata(path: Path):
    """Read config only to return names, event counts and irreversible hashes."""
    if not path.exists():
        return {"exists": False}
    try:
        if path.suffix == ".toml":
            import tomllib
            config = tomllib.loads(path.read_text())
        else:
            config = json.loads(path.read_text())
    except (ValueError, OSError):
        return {"exists": True, "parse": "unsupported", "sha256": digest(path)}
    hooks = {}
    for event, registrations in config.get("hooks", {}).items():
        registrations = registrations if isinstance(registrations, list) else [registrations]
        hooks[event] = {"registrations": len(registrations), "sha256": hashlib.sha256(
            json.dumps(registrations, sort_keys=True).encode()).hexdigest()}
    return {"exists": True, "sha256": digest(path), "hooks": hooks,
            "mcp_names": sorted(config.get("mcpServers", config.get("mcp_servers", {}))),
            "enabled_mcp_names": config.get("enabledMcpjsonServers", []),
            "disabled_mcp_names": config.get("disabledMcpjsonServers", []),
            "all_project_mcp_enabled": config.get("enableAllProjectMcpServers")}


def inventory(root: Path, home: Path | None = None):
    root = root.resolve()
    home = home or Path.home()
    now = time.time()
    cutoff = now - 90 * 86400
    result = {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "privacy": "PRIVATE metadata only, paths may identify projects",
              "root": str(root), "observation_window_days": 90,
              "use_limit": "mtime is modification evidence, not execution or non-use evidence",
              "context_measurement": {"startup_tokens": None,
                  "reason": "No harness token telemetry captured by filesystem inventory; bytes are not tokens"}}
    result["git_dirty"] = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    result["git_dirty"]["paths_nul"] = result["git_dirty"].pop("stdout")
    instructions = []
    skills = []
    repositories = []
    tools = []
    artifacts = []
    ignored = {"node_modules", ".git", ".venv", "venv", "__pycache__", "Library", ".next"}
    total_bytes = 0
    files = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        p = Path(base)
        if ".git" in dirs or ".git" in names:
            dirty = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"], p)
            upstream = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"], p)
            repositories.append({"path": os.path.relpath(p, root), "dirty": bool(dirty["stdout"]),
                "dirty_status_returncode": dirty["returncode"], "ahead_behind": upstream["stdout"].strip() or None,
                "upstream_check_returncode": upstream["returncode"], "retirement": "retain pending dependency and use evidence"})
        dirs[:] = [d for d in dirs if d not in ignored and not (p / d).is_symlink()]
        for name in names:
            file = p / name
            try:
                m = metadata(file, root)
                total_bytes += m["bytes"]
                files += 1
                if name in {"AGENTS.md", "CLAUDE.md"}:
                    instructions.append({**m, "sha256": digest(file), "resolved": str(file.resolve())})
                if name == "SKILL.md" and ".claude/skills" in str(file):
                    skills.append({**m, "name": file.parent.name, "source": ".claude/skills", "sha256": digest(file), "resolved": str(file.resolve())})
                if len(file.relative_to(root).parts) <= 2 and name.endswith((".sh", ".py", ".js", ".ts")):
                    tools.append(m)
                if len(file.relative_to(root).parts) == 1:
                    artifacts.append(m)
            except OSError:
                pass
    recorded_paths = {s["path"] for s in skills}
    for source in [root / ".claude/skills", root / ".agents/skills", root / ".codex/skills", home / ".codex/skills", home / ".agents/skills", home / ".claude/skills"]:
        if source.exists():
            for file in source.glob("*/SKILL.md"):
                try:
                    m = metadata(file, root)
                    if m["path"] not in recorded_paths:
                        skills.append({**m, "name": file.parent.name, "source": str(source), "sha256": digest(file), "resolved": str(file.resolve())})
                        recorded_paths.add(m["path"])
                except OSError:
                    pass
    grouped = collections.defaultdict(list)
    for skill in skills:
        grouped[skill["name"]].append(skill)
    duplicates = {name: {"copies": len(items), "distinct_hashes": len({i["sha256"] for i in items})}
                  for name, items in grouped.items() if len(items) > 1}
    result.update(instructions=instructions, skills=skills, skill_duplicates=duplicates,
                  repositories=repositories, root_artifacts=artifacts, shallow_scripts=tools,
                  disk={"regular_file_logical_bytes_excluding_caches": total_bytes,
                        "file_count": files, "excluded_directory_names": sorted(ignored),
                        "note": "Logical bytes, not allocated disk or loaded context"})
    configs = [root / ".claude/settings.json", root / ".claude/settings.local.json", root / ".mcp.json",
               root / ".codex/hooks.json", home / ".claude/settings.json", home / ".claude.json",
               home / ".codex/config.toml"]
    result["configurations"] = {str(p): config_metadata(p) for p in configs}
    result["effective_configuration"] = "Configured layers inventoried. Harness merging, runtime enablement and successful execution require separate verification."
    schedules = []
    for directory in [home / "Library/LaunchAgents", Path("/Library/LaunchAgents"), Path("/Library/LaunchDaemons")]:
        if directory.exists():
            for p in directory.glob("*.plist"):
                try:
                    v = plistlib.loads(p.read_bytes())
                    schedules.append({"file": str(p), "label": v.get("Label"),
                        "program_basename": Path(v.get("Program", (v.get("ProgramArguments") or [""])[0])).name,
                        "calendar": v.get("StartCalendarInterval"), "interval": v.get("StartInterval"),
                        "run_at_load": v.get("RunAtLoad", False), "sha256": digest(p)})
                except (ValueError, OSError, plistlib.InvalidFileException):
                    schedules.append({"file": str(p), "parse": "unsupported"})
    result["schedules"] = schedules
    launchctl = run(["launchctl", "list"])
    result["launchctl_labels"] = [l.split()[-1] for l in launchctl["stdout"].splitlines()[1:] if l.split()]
    result["launchctl_returncode"] = launchctl["returncode"]
    result["crontab"] = {"returncode": run(["crontab", "-l"])["returncode"], "content": "excluded because commands can embed secrets"}
    result["use_evidence"] = {"recently_modified_shallow_scripts": [m["path"] for m in tools if m["mtime"] >= cutoff],
                              "execution_telemetry": "unknown", "dependencies": "not inferred from age or mtime"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("baseline output exists; choose a new snapshot instead of overwriting")
    data = inventory(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(args.output, 0o600)
    print(json.dumps({"output": str(args.output), "instructions": len(data["instructions"]),
                      "skills": len(data["skills"]), "repositories": len(data["repositories"])}))


if __name__ == "__main__":
    main()
