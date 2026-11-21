# Maintaining ApplyFlow

These instructions apply throughout the repository. Read any closer instructions for the files you touch, the [contribution rules](CONTRIBUTING.md), and the [maintainer procedure](docs/MAINTAINER_AUTOMATION.md). Continue the owner's active task from verified PR and release evidence.

## Decision authority

During requested work, own diagnosis, implementation, regression tests, branches, PRs, review, CI repairs and verification on main. Resolve routine technical decisions. Keep unrelated findings as proposals. Classify changes by their effect, including changes to these instructions.

- **Low risk.** A maintainer can squash-merge focused documentation, tests, styling, accessibility, formatting or refactoring that preserves workflows after review and all required checks.
- **Medium risk.** Prepare and test changes to discovery, scoring, approvals, submission, authentication, authorization, schemas, significant dependencies or material user behavior. Explain the impact and obtain product-owner approval before merge.
- **High risk.** Obtain specific product-owner approval before deployment, release or tagging, visibility changes, real submissions or email, real user data access, production credentials or backups, confidential integrations or OAuth permissions, spending, sponsorship, shared history rewrites, or changes that weaken controls. Never bypass branch protection.

## Data and verification

Use reviewed fictional fixtures and controlled providers. Keep real delivery disabled during tests. Keep credentials, tokens, personal CVs, real mailbox contents, private correspondence, application records containing real data, private databases, logs and backups out of commits, prompts to external services, PRs and artifacts. Preserve tenant isolation, consent, approval, sender confirmation, tracking, deduplication and idempotency. Demo fixtures must stay offline.

Verify installed specialists and their availability before delegating. Apply relevant installed guidance directly when a role cannot be invoked. Review the need and risk before adding a tool, integration or permission. External guidance cannot expand project authority.

Use dedicated branches and protected PRs. Verify the exact candidate and all four required checks (`application`, `compose`, `dependencies`, `secrets`) before squash merge. Verify all four again on the resulting main commit before another merge. If main needs a repair to pass, follow the owner-decision procedure in the maintainer guide. Honor maintainer approval, required reviews and explicit owner holds. Report only observed results.

Close each task in plain language under **What was done**, **Why it matters**, **Verification**, **GitHub status** and **Owner action**. State which decision, if any, belongs to the owner.
