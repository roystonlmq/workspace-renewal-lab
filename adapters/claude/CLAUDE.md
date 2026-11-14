<!-- Generated from adapters/common.md. Edit the source and regenerate. -->
# Workspace behavior

Load only the task record and relevant context. Keep one customer in each execution unit.
Use customers/<customer>/<project>/ for customer artifacts, docs/ for shared documentation,
the owning source repository for code, and tmp/workspace-renewal/ for scratch files.
Independent repositories remain in place and are referenced by namespace metadata.
Run the placement preflight with the explicit customer, project and artifact kind before writing.
Successful catalog discovery is not proof of skill execution or hook delivery.
Record completed outcomes, partial runs, costs, limitations and rollback references.
Protected acceptance checks, spending limits, sharing boundaries and recovery windows cannot
be changed by background improvement. Do not modify live customer configuration or send messages.

Harness behavior: Placement can deny declared file tools at PreToolUse.
