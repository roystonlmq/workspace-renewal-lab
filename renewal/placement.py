"""Quiet placement checks for declared writes and supported hook payloads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def workspace_check(root: Path, path: str, customers: set[str], existing_paths=None) -> dict:
    """Check only unambiguous new placement mistakes, preserving existing files."""
    root = root.resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        return {'allowed': False, 'reason': 'Write is outside this workspace', 'suggestion': 'Use the owning repository'}
    relative = target.relative_to(root)
    parts = relative.parts
    if (str(relative) in existing_paths if existing_paths is not None else target.exists()):
        return {'allowed': True}
    if len(parts) >= 3 and parts[0] == 'docs' and parts[1] in customers:
        return {'allowed': False, 'reason': 'New customer artifacts need a project namespace',
                'suggestion': str(Path('customers') / parts[1] / '<project>' / Path(*parts[2:]))}
    if len(parts) == 1 and not relative.name.startswith('.'):
        return {'allowed': False, 'reason': 'New loose workspace artifacts need an owning folder',
                'suggestion': 'docs/<topic>/ or the owning source repository'}
    return {'allowed': True}


def registered_customers(root):
    registry = root / 'customers/registry.json'
    customers = set()
    if registry.is_file():
        for row in json.loads(registry.read_text()).get('projects', []):
            customers.update([row['customer'], *row.get('aliases', [])])
    return customers


def check(root: Path, path: str, kind: str = "shared", customer: str | None = None,
          project: str | None = None, owning_repo: str | None = None) -> dict:
    root = root.resolve()
    target = (root / path).resolve()
    try:
        relative = target.relative_to(root)
    except ValueError:
        return {"allowed": False, "reason": "Write is outside the declared workspace", "suggestion": "Declare the owning repository explicitly"}
    if kind == "customer":
        if not customer or not project or not SLUG.fullmatch(customer) or not SLUG.fullmatch(project):
            return {"allowed": False, "reason": "Customer and project must be explicit kebab-case names", "suggestion": "customers/<customer>/<project>/"}
        expected = Path("customers") / customer / project
    elif kind == "scratch":
        expected = Path("tmp/workspace-renewal")
    elif kind == "code":
        if not owning_repo:
            return {"allowed": False, "reason": "Code needs its owning repository", "suggestion": "Declare owning_repo"}
        repo = (root / owning_repo).resolve()
        try:
            expected = repo.relative_to(root)
        except ValueError:
            return {"allowed": False, "reason": "Owning repository is outside this workspace", "suggestion": "Run the checker from that repository"}
    elif kind == "shared":
        expected = Path("docs")
    else:
        return {"allowed": False, "reason": "Unknown artifact kind", "suggestion": "Use customer, shared, scratch or code"}
    allowed = relative != expected and relative.is_relative_to(expected)
    return {"allowed": allowed, "reason": "Placement accepted" if allowed else f"{kind} writes belong under {expected}/",
            "suggestion": str(expected / relative.name), "path": str(relative)}


def paths_from_payload(payload: dict) -> list[str]:
    tool_input = payload.get("tool_input") or payload.get("arguments") or {}
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            return []
    if not isinstance(tool_input, dict):
        return []
    paths = [str(tool_input[k]) for k in ("file_path", "path") if tool_input.get(k)]
    # apply_patch has several path headers. Reject traversal after parsing as usual.
    patch = tool_input.get("patch") or tool_input.get("input") or ""
    if isinstance(patch, str):
        paths.extend(re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", patch, re.M))
    return paths


def invoke_hook(payload: dict, root: Path, harness: str, scope: dict) -> tuple[int, dict | None]:
    workspace = scope.get('kind') == 'workspace'
    customers = registered_customers(root) if workspace else set()
    exceptions = scope.get('existing_paths')
    violations = [r for path in paths_from_payload(payload)
                  if not (r := (workspace_check(root, path, customers, exceptions) if workspace else check(root, path, **scope)))["allowed"]]
    if not violations:
        return 0, None
    reason = "; ".join(v["reason"] + ". Use " + v["suggestion"] for v in violations)
    if harness == "claude":
        return 0, {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}
    return 0, {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "Placement correction needed: " + reason},
               "enforcement": "post-write feedback, not prevention"}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--harness", choices=["claude", "codex"])
    p.add_argument("--kind", choices=["shared", "customer", "scratch", "code", "workspace"], default="shared")
    p.add_argument("--customer")
    p.add_argument("--project")
    p.add_argument("--owning-repo")
    p.add_argument('--exceptions-file', type=Path, help='Frozen legacy paths, required for meaningful post-write checks')
    p.add_argument("paths", nargs="*")
    a = p.parse_args()
    scope = {"kind": a.kind, "customer": a.customer, "project": a.project, "owning_repo": a.owning_repo}
    if a.exceptions_file:
        if a.kind != 'workspace':
            p.error('exceptions-file requires workspace kind')
        scope['existing_paths'] = set(json.loads(a.exceptions_file.read_text())['paths'])
    if a.harness:
        try:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict):
                raise ValueError("object required")
        except (ValueError, TypeError):
            print("Invalid hook input", file=sys.stderr)
            return 2
        code, output = invoke_hook(payload, a.root, a.harness, scope)
        if output:
            print(json.dumps(output))
        return code
    results = [workspace_check(a.root, path, registered_customers(a.root), scope.get('existing_paths'))
               if a.kind == 'workspace' else check(a.root, path, **scope) for path in a.paths]
    violations = [r for r in results if not r["allowed"]]
    if violations:
        print(json.dumps(violations))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
