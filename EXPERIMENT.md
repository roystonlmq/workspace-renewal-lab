# Matched task experiment

The implementation supplies accounting and acceptance tools. It does not claim a
completed three-condition experiment. Keep run configuration, snapshots, raw output
and real session history outside this repository.

1. Preserve the original instructions, hooks, selected local memory and tool versions
   before changing the workspace. Retain their original formats and record hashes.
   Do not mix customer scopes in a snapshot or a task context.
2. Prepare three conditions: `current-local`, `clean-local`, and `clean-notion`.
   The two cleaned conditions must use identical instructions, skills, tools and
   task fixtures. Change only the relevant memory source. Verify the Notion account
   and actual ACL before writing private memory or running the Notion condition.
3. Select the acceptance check from `evals/development.json` before making a call.
   Use `evals/heldout.json` only for evaluation, and preserve `evals/freeze.json`.
   A report needs substantive sections at its requested path. Playbook work needs
   every applicable regression, including failures and recovery paths.
4. Run representative tasks at least three times per condition, with separate cold and
   warm cache groups. Use the same explicit model, effort, harness version,
   permissions, fixture hash and tools hash. Record how cache state was controlled.
   When the provider cannot establish it, set `cache_verified` to false. A configured
   model name alone is insufficient: record the observed model separately.
5. Invoke each command through `renewal.harness`. Use an argv JSON list, your own
   authenticated harness and a private metadata file. There are no internal retries.
   Log repairs and retries as additional calls. A process exit code is not acceptance.
6. Check delivery through `renewal.experiment.accepted`, then append acceptance to
   that call's ledger ID using `Ledger.finish`. Include retrieval, judge and nightly
   maintenance calls in the same private ledger. Record non-model retrieval time
   separately. Include failed and interrupted attempts in the totals.
7. Run `python3 -m renewal.experiment /path/to/private-ledger.jsonl`. Unknown model
   observations and unverified cache states cannot form matched comparisons. Compare
   completion rates first, then matched accepted outcome costs and all-attempt cost.
   Keep provider estimates distinct from actual charges and cite dated pricing if
   estimates are added. Missing billing is unknown, never zero.
8. Review reliability and full costs before selecting authoritative memory or a
   project launcher. The comparison tool never automatically adopts either.

Example metadata for one reader-supplied call:

```json
{
  "condition": "clean-local",
  "task": "report-complete",
  "repeat": 1,
  "cache": "cold",
  "cache_verified": false,
  "model": "explicit-model-from-your-provider",
  "effort": "high",
  "permissions": "workspace-write",
  "fixture_hash": "replace-with-verified-sha256",
  "tools_hash": "replace-with-verified-sha256",
  "harness_version": "replace-with-observed-version"
}
```

The harness extracts an exposed `observed_model` from JSON events. If the provider
does not expose it, preserve that uncertainty. Do not fill it with the requested name.

`fixtures/` contains only synthetic history for clear and resume experiments.
`python3 -m renewal.eval --output /tmp/renewal-verification` exercises isolated
memory, ingestion, due-report, archive and operational-change scenarios without
requiring a model or credentials. These checks establish behavior, not savings.
