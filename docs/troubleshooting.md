# Troubleshooting

| Symptom | Check |
| --- | --- |
| Port already in use | Select a free loopback port in the private environment, and update APP_URL, API_PUBLIC_URL and ALLOWED_ORIGINS to match. |
| API unavailable | Run Compose `ps -a`. Check that PostgreSQL is healthy and the migration exited with zero. Inspect logs privately. |
| First-run setup needs a code | Run the claim command from the installation guide on the same stack. Use the CLI's reissue option only for an unclaimed instance you control. |
| OAuth redirect mismatch | Compare the public API origin and the precise callback for the intended flow with the provider console. |
| No drafts | Inspect source evidence, preference filters, eligibility and CV facts. Try a fixture batch before configuring network discovery. |
| Approved draft cannot send | Check effective live mode, mailbox health, changed content, recipient history, stop-automation state and the outbox reason. |
| Handoff uncertain | Compare mailbox Sent history and the application record, then reconcile. A blind retry can duplicate mail. |
| Previously stored credentials fail to decrypt | Restore the matching encryption key from a private backup. Generating a replacement cannot recover prior ciphertext. |

Share only fictional reproductions. [Support](../SUPPORT.md) · [Private security reports](../SECURITY.md)
