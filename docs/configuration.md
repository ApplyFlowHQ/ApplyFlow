# Configuration

Generate the local Compose environment using `scripts/init_compose_env.py`. Keep the resulting file readable only by the operator. The generator fills all four blank values in the example. The API, migration job, worker and Beat must resolve the same database and application secrets.

| Required for the Compose quick start | Purpose |
| --- | --- |
| APP_SECRET_KEY | Random, persistent signing and keyed-hash material. APP_SECRET_KEY_FILE can supply it from a mounted file. |
| MAILBOX_ENCRYPTION_KEY | A persistent URL-safe base64 Fernet key for stored credentials. MAILBOX_ENCRYPTION_KEY_FILE is the mounted-file alternative. Back up privately. |
| POSTGRES_PASSWORD | PostgreSQL service password. The bootstrapper can use POSTGRES_PASSWORD_FILE and the password placeholder in the connection URL. |
| DATABASE_URL | SQLAlchemy connection URL using the psycopg driver and the internal postgres service. The generator uses the same random password as PostgreSQL. |
| REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND | Internal Redis connections for application state, task delivery and task results. |
| APP_URL, API_PUBLIC_URL, ALLOWED_ORIGINS | Browser-facing application origin, API origin used for callbacks and comma-separated browser origin allowlist. |
| APPLYFLOW_HTTP_PORT, APPLYFLOW_HTTPS_PORT, APPLYFLOW_CADDYFILE | Host bindings and Caddy configuration. The example selects the loopback HTTP proxy. |
| LOCAL_STORAGE_PATH | Shared private document directory inside the backend and worker containers. |

| Optional settings | Effect |
| --- | --- |
| ENVIRONMENT, COOKIE_SECURE, SESSION_TTL_HOURS | Runtime mode, HTTPS-only cookie flag and session lifetime. Use secure cookies with HTTPS. |
| LIVE_SENDING_ENABLED, LOCK_ENV_CONFIG, OPERATOR_ADMIN_EMAILS | Sending starts off. Once claimed, installation settings control sending unless environment configuration is locked. Restrict operator privileges. |
| LLM_PROVIDER, OPENAI_API_KEY, OPENAI_MODEL, GEMINI_API_KEY, GEMINI_MODEL | Select a drafting backend and its private credentials. With no model configured, deterministic drafts are available. |
| SEARCH_PROVIDER, TAVILY_API_KEY, BRAVE_API_KEY | Enable network discovery. Demo fixtures run without keys. |
| GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI_AUTH, GOOGLE_OAUTH_REDIRECT_URI_GMAIL, GOOGLE_OAUTH_REDIRECT_URI_SHEETS | Register separate sign-in, Gmail and Sheets callbacks against API_PUBLIC_URL. |
| MS_GRAPH_CLIENT_ID, MS_GRAPH_CLIENT_SECRET, MS_GRAPH_TENANT_ID, MS_GRAPH_REDIRECT_URI | Microsoft mailbox OAuth client, permitted tenant and callback. |
| TRANSACTIONAL_EMAIL_PROVIDER, TRANSACTIONAL_FROM_EMAIL, TRANSACTIONAL_SMTP_HOST, TRANSACTIONAL_SMTP_PORT, TRANSACTIONAL_SMTP_USERNAME, TRANSACTIONAL_SMTP_PASSWORD, TRANSACTIONAL_SMTP_USE_TLS | Verification and recovery delivery. Configure privately for real users. |
| STORAGE_BACKEND, S3_BUCKET, S3_REGION, S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY | Use local storage or private S3-compatible objects. |
| MAX_DAILY_RECIPIENTS_DEFAULT, MAX_DAILY_RECIPIENTS_HARD_CAP, MAX_USER_CONCURRENT_BATCHES | Per-user sending and concurrency limits. |
| MAX_BATCH_SEARCH_COST_USD, MAX_MONTHLY_USER_COST_USD, MODEL_TIMEOUT_SECONDS, SEARCH_TIMEOUT_SECONDS | Budget estimates and provider timeouts. Set provider billing limits separately. |
| CELERY_TASK_ALWAYS_EAGER | Synchronous task execution for isolated development. |
| DATA_RETENTION_DAYS_DEFAULT, INBOX_STALE_MINUTES | Default retention period and mailbox freshness threshold. |
| HTTPS_DOMAIN | Host handled by Caddy in TLS mode. |

Private user integration settings, installation-wide settings and environment defaults are resolved in `backend/app/services/effective_config.py`. Sharing and environment locking affect precedence. The complete typed field list, including upload bounds, estimated cost rates, FX, OCR and worker tuning, is in [Settings](../backend/app/core/config.py).
