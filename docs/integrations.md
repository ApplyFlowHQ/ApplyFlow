# Integrations

| Integration | Data boundary and setup |
| --- | --- |
| Brave Search, Tavily and career pages | Search queries may reveal job preferences. Configure the selected provider in Setup or private environment settings. Read source terms, use bounded queries and verify the employer's application invitation. |
| OpenAI and Gemini | Drafting can transmit selected CV facts and job context. Obtain permission before enabling it, check the provider’s data terms and review generated claims. Deterministic drafting is available. |
| Google sign-in, Gmail and Sheets | Register the exact callback URIs shown in Setup. Sign-in, mailbox access and spreadsheet access use distinct flows. Verify requested scopes, ownership, revocation and sent-history coverage before live use. Spreadsheet recipients gain access to exported data. |
| Microsoft Graph | Configure tenant, client ID, secret and callback. Authorize the candidate's mailbox. Reconnect when consent expires. |
| SMTP/IMAP | Use a mailbox the user controls, encrypted transport and credentials scoped to that account. Confirm an explicit test message only to an authorized destination. |
| Object storage | With the S3 adapter, restrict bucket access and credentials to the intended private object prefix. |
| Transactional mail | Console mode supports local setup. Configure SMTP for real verification and recovery messages. |
| Imported ZIPs | Treat job records and PDF attachments as untrusted. Inspect provenance, source verification and duplicate warnings before approving anything. |

Live provider availability, consent-screen approval and delivery depend on the operator's accounts. Respect job-site terms and employer instructions. Avoid unsolicited bulk outreach. Stop automation if consent or evidence changes.

## Outbound connections

The career-page reader permits HTTP and HTTPS on ports 80, 443, 8080 and 8443. Each DNS response must contain only permitted public unicast addresses. IPv4-mapped IPv6 is evaluated as IPv4. Local, reserved, multicast, metadata and IPv6 transition destinations are blocked. Use standard IP notation or a DNS name. Credentials in URLs, scoped addresses and ambiguous authorities are rejected.

Each connection dials a checked numeric address. HTTP Host, TLS SNI and certificate verification use the original host. Each redirect gets a fresh address check and connection pool. Fallback uses addresses from the same checked answer set. The reader ignores environment proxies and rejects HTTPS downgrades. Defaults allow four redirects, ten seconds total and 1,500,000 bytes of identity-encoded response data.

Configurable mail connections apply the same public-address checks on ports 25, 465, 587, 143 and 993. Authenticated SMTP requires implicit TLS or STARTTLS with certificate verification. Use a publicly reachable mail endpoint with a certificate valid for the configured hostname.
