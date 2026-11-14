---
name: renewal-fixture
description: Execute the synthetic workspace renewal report fixture and verify its requested destination. Contains no customer data.
---

Run `python3 scripts/report.py <workspace-root> <relative-report-path>` from this skill directory.
The report must be placed under `docs/`. A failed placement check must prevent writing.
Verify the report JSON has `completed: true`. Report only the exact output path and status.
