# Workspace renewal lab

A standard-library Python lab for protected agent evals, portable harness adapters,
placement checks, reversible archives, private memory lifecycle, incremental session
ingestion and bounded nightly measurement. Examples and tests contain synthetic data.

Requires Python 3.11 or later. No credentials are included. Run from this directory:

```sh
python3 -m unittest discover -s tests -v
python3 -m renewal.eval --output /tmp/renewal-evals
python3 -m renewal.adapters .
python3 -m renewal.placement --root . --kind customer --customer synthetic-a --project demo customers/synthetic-a/demo/report.md
```

Edit `adapters/common.md` and the canonical fixture skill, then regenerate adapters.
Generated hook configurations are examples for isolated execution. Install only after
reviewing the harness support and trust requirements. Claude's declared file tools can
be blocked before a write. The Codex adapter currently offers post-write feedback and
explicit preflight, with no claim of universal prevention. Shell writes need preflight.

```sh
python3 -m renewal.harness --help
python3 -m renewal.experiment /tmp/private-run-ledger.jsonl
python3 -m renewal.nightly --help
```

The harness runner records the start before invoking a command, persists failures,
captures exposed token usage, and makes no retries. Supply your own authenticated
harness, model, effort and permissions. Raw logs belong outside this repository.

The nightly runner can read dedicated private ledgers through an `accounting`
configuration with explicit absolute `ledger_paths` ending in `.jsonl`. Include evals,
repairs and maintenance calls. `renewal.accounting.LedgerCostSummary` counts failed
and unfinished calls, rejects conflicting duplicates and keeps unknown totals null.
Actual charges require `actual_charge_source` and `actual_charge_date`; estimates
require `pricing_source` and `pricing_date`. Dates use ISO format. Provider-reported
costs remain separate. Token fields are grouped by harness because provider input
semantics differ. Missing ledgers and unrecorded outer orchestration prevent complete
spend claims. The reader never adds prices or treats unknown billing as zero.

The private Notion transport requires your own credentials and fresh, complete ACL
evidence. A null public URL is insufficient. No production local memory cache exists.
Strict mode requires an independent live ACL provider. An explicitly approved
CLI pilot can use live identity, destination, ancestry and publication checks within
a fixed window of at most fourteen days. It cannot prove all sharing changes and
never silently renews. A configuration flag cannot supply proof. Finding extraction needs
an explicit isolated session route and a reviewed extractor. Supply your own private
data source with `Name` title, `Payload` rich text and `Status` select properties.
Remote memory operations enforce namespace and customer scope, provenance, bounded
recall, correction, recovery exclusion and a protected ask-before-forgetting preference.
Expired recovery pages can be moved to Notion trash. Permanent physical deletion is
unsupported and must be verified separately before claiming a 90-day deletion policy.
Synthetic services live only in tests. Never connect public examples to real sessions
until privacy and scope checks pass.

Nightly execution is blocked unless the remote destination, activity detection and
readiness gates are verified. Scheduling a timer does not prove execution. The runtime
caps daily work at 20 sessions and one operational improvement attempt, processes
stable identifiers and record offsets, and reserves attempts before side effects.
The 14-day report path is tested using a synthetic clock. No live pilot or future
report is implied by this source package.

Protected tests run before operational changes, and rollback rejects concurrent edits.
Installation errors after replacing a file restore its original bytes and mode when
the installed content still matches. Newer concurrent edits remain intact. If recovery
storage fails, retain the original receipt and review the conflict before proceeding.
`renewal.improvement.ImprovementPipeline` copies only approved inputs to a private
candidate directory, makes at most one explicitly configured harness invocation,
requires the frozen checks, applies one allowlisted patch and runs a postcheck.
A failing postcheck rolls back its own change. Receipts, candidate evidence and model
output stay outside the public package. Connect trusted checks and activity callbacks
through `renewal.improvement.create`; the default nightly factory makes no model calls.
This is a guard for trusted integration code, not an isolation sandbox for hostile code.
Do not let candidates modify protected criteria, privacy, recovery or spending controls.

Archives preserve bytes and hashes and verify before restoration. They do not remove
sources automatically. Do not delete active work, dirty or unpushed repositories,
credentials or original evidence. Local copies do not reclaim laptop disk. Component
retirement requires verified eligibility and storage after 30 days without renewed use;
memory recovery lasts 90 days.

There are no measured model savings in this initial release. Synthetic test latency
does not measure task costs. Compare current local, cleaned local and cleaned Notion
conditions with matched tasks, models, effort, permissions, tools and cache conditions.
Report completion and full failed-task costs separately. Unknown billing stays unknown.
See [RESULTS.md](RESULTS.md) for observed checks and [EXPERIMENT.md](EXPERIMENT.md)
for the reproducible comparison protocol.

No LinkedIn post or external message is sent by this project.

The trusted `renewal.runtime:create` connector accepts byte-pinned source receipts,
explicit namespace and scope routes, and reviewed canonical facts. It handles Claude
and Codex message schemas without copying arbitrary transcript text. Changed sources
are quarantined; adding another source requires explicit scope review. Maintenance
only trashes expired recovery rows and never forgets active records from age alone.

`renewal.activity` reads complete paginated Codex metadata over the documented local
WebSocket control socket and Claude session status with process-start verification.
It never starts, resumes or interrupts a thread. Unknown metadata, transport failures,
active tasks and insufficient computer idle time defer mutations. Verify the local
interface after Codex upgrades. The scheduler inherits the installation PATH and
runs every five minutes while awake; the runner enforces the Singapore due time.
Registration, invocation, useful work and a running pilot are separate observations.
A full pilot cannot start unless its report precedes the configured policy expiry.
