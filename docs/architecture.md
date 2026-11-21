# Architecture

The browser loads the Vite-built React/TypeScript SPA through Caddy and Nginx. Nginx proxies API requests to Uvicorn. Routes under `backend/app/api` cover authentication, setup, workspace data, automation and tracking. The API authenticates a server-side session, validates CSRF for protected mutations and scopes records to the signed-in user. Passwords use Argon2. Google sign-in and mailbox authorization have distinct ownership checks.

SQLAlchemy models in `backend/app/db` hold users, CV versions and facts, search preferences, source sightings, applications, approvals, recipient history and outbox state. Alembic owns schema upgrades. Document bytes use the private storage adapter. The application encrypts provider credentials with Fernet; the database still contains sensitive profile, draft and tracking information.

The API queues background work through Redis. The Celery worker executes batch discovery, verification, matching, drafting, outbox processing and maintenance. One Beat instance schedules due work. The outbox records state across the external mail handoff; operators must reconcile ambiguous outcomes. SQLite with eager execution is available for local development.

Provider adapters sit under `backend/app/services`. Discovery returns candidates and evidence. Matching uses confirmed profile information and CV facts. Draft approval binds the recipient, content and attachment to the reviewed version. Gmail, Graph and SMTP/IMAP adapters connect to separately authorized mailboxes. See [the architecture diagram](../README.md#architecture).
