# Maintainer procedure

## Pick up the task

1. Read the [repository instructions](../AGENTS.md), [contribution rules](../CONTRIBUTING.md) and [security policy](../SECURITY.md). Check for scoped instructions. Verify the remote is ApplyFlowHQ/ApplyFlow, fetch main, and check the worktree and existing PRs. Preserve other contributors' work.
2. Recover the last verified change, outstanding checks and owner decisions. For the initial v0.1.0 handoff, see the [Global Stop correction and release-gate evidence](https://github.com/ApplyFlowHQ/ApplyFlow/pull/21). Changing which discovery modes may run requires owner approval before merge. Publication requires a separate decision. The owner accepted the earlier merge format and overlapping checks for PRs #19, #17 and #18. Leave that history intact.
3. Choose specialists for the actual change. Check locally installed Codex agent definitions and available callable roles. For example, pair a frontend engineer with an accessibility reviewer for a bilingual UI change, or a backend engineer with a privacy reviewer for data handling. Delegate bounded work and review the result. Distinguish a callable role, a general reviewer following installed guidance and guidance used directly. Use the existing toolchain where possible.

## Prepare and review

1. Reproduce the problem with fictional data in a disposable environment. Use a dedicated branch. Make the smallest complete correction and add a regression test for the reported failure. Preserve existing approvals and delivery safeguards.
2. Use commands and versions from the [development guide](development.md), [application and Compose workflow](../.github/workflows/ci.yml) and [security workflow](../.github/workflows/security.yml). Run applicable local checks. For workflow changes exercise backend, frontend, browser and integration paths. For UI changes cover English and Arabic, keyboard focus, accessible names and narrow screens. Test schema changes on both a fresh and an upgraded disposable database.
3. Inspect the final diff, staged files, licenses, workflow permissions, privacy and compatibility. Check for unintended artifacts. Investigate failures before retrying. Replace misleading synthetic secret fixtures when appropriate and retain scanner coverage. Send sensitive findings through the private channel in SECURITY.md.
4. Push only the intended branch and open a focused PR. Record the user impact, risk, tests actually run and outstanding limitations. Resolve review comments. Avoid history replacement. Seek specific approval before rewriting a shared branch.

## Merge one change at a time

1. Record the current base SHA and PR head SHA. Check the diff, conflicts, review decision and repository rules. Every PR needs affirmative maintainer approval of the reviewed candidate. The delegated maintainer may make that decision for eligible low-risk work. Honor any separate reviewer requirement. For medium-risk work, also obtain explicit owner approval for the reviewed behavior. Reassess approvals when the candidate changes.
2. Verify successful GitHub Actions results for `application`, `compose`, `dependencies` and `secrets` on the applicable PR merge candidate against current main. Inspect the event and SHAs. A green run on a stale base or another branch is insufficient. Refresh the candidate and repeat checks if either SHA moves.
3. Serialize merges. Use the protected squash-merge path with the expected PR head and recheck the base immediately before merging. When multiple maintainers are merging, coordinate a single merge window or use an existing enforced up-to-date branch rule. If the base moves, recompute the candidate. Respect required reviews and repository permissions. If access prevents completion, report the exact restriction and smallest owner action that resolves it.
4. Verify the resulting main SHA, expected tree and all four hosted checks on that SHA. If a check fails, pause merges, diagnose it and prepare a focused repair. If the existing SHA cannot pass without a code change, seek a specific owner decision for a repair-only PR through the same protected review and CI gates. Resume other merges only after all four checks pass on repaired main.

## Release handoff

On the final candidate, verify fresh installation, a supported upgrade, a PostgreSQL backup and clean restore including private storage, and real worker execution with isolated fictional providers. Check retry, cancellation, idempotency, approval and confirmation. Verify demo makes zero external calls. Document the external calls permitted in dry run. Simulated delivery establishes only the simulated outcome.

Run the backend tests, Ruff, TypeScript, Vite/PWA, Chromium, Compose startup and health, migrations, dependency audits and redacted scans of source and reachable history. Review the file inventory, modes, privacy and SHA-256 manifest against the final commit. Reassess accepted dependency risks if exposure changes.

Summarize what users can safely evaluate, what remains experimental and what evidence is outstanding. Keep the release on hold until the owner explicitly approves publication of the identified candidate. Separate release, deployment and spending decisions. Carry out only the authorized action.

Use one owner-action status in the handoff: **No action needed**, **Product decision required**, **Approval required**, or **Blocked by an external service**. For a decision, explain the consequence and recommend a course the owner can evaluate without performing Git operations.
