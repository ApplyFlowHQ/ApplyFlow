# Contributing to ApplyFlow

Start in Issues with `good first issue` or `help wanted`. Read the acceptance criteria and leave a short note about the change you want to make. Report a defect with the bug form, or use the feature form to explain the user problem and alternatives. Security issues have a [private reporting path](SECURITY.md).

1. Fork the repository, clone your fork and create a focused branch with `git switch -c fix/describe-the-change`.
2. Set up an isolated development checkout using the [development guide](docs/development.md). Keep live sending off and use fictional people, employers, files and correspondence.
3. Follow the existing API/service/task boundaries. Preserve user isolation, approval checks and supported workflows. Add regression coverage for changed behavior and document any user-visible change.
4. Run backend tests, Ruff and the frontend build. Run browser tests for review, tracking or UI changes. Include the exact commands and results in the pull request.
5. Use short imperative commits, such as “Preserve approval state after preview”. Push your branch to your fork, open a PR against the default branch, link the issue, and explain the behavior before and after.
6. A maintainer checks correctness, privacy, compatibility, test results and scope. Resolve review comments and rerun affected checks. Merge requires passing required checks and maintainer approval.

Keep secrets, real CVs, recipient lists, account IDs, screenshots of real workspaces and OAuth responses out of reports, tests and commits. Automation must respect the candidate’s consent, platform rules, deduplication and review. Spam or unauthorized submission features are outside the project scope.

Contribute only material you can license. Record origins and required notices for third-party code or assets. Contributions are accepted under the Apache License, Version 2.0 unless maintainers agree otherwise in writing. [Contributor guide](docs/contributing-guide.md) · [Code of conduct](CODE_OF_CONDUCT.md)
