# Deployment

Use the Compose file under `deploy/`. PostgreSQL and Redis stay on the Compose network. Migrations run once per startup/upgrade, the API and worker share private storage, and the scheduler should run as a single replica. Caddy forwards browser traffic to Nginx. Each installation needs distinct volumes and key material.

The example config binds HTTP to loopback. For a host deployment set the public origin, API URL, allowed origins, HTTPS domain, secure cookies and production environment consistently. Use the TLS Caddyfile and configure the intended public ports and DNS before opening access. Supply a real transactional delivery method, protect database and storage backups, and test restore together with the corresponding encryption key. Protect Redis, PostgreSQL and container management access.

Before an upgrade take an application-consistent backup and rehearse the migration on a restored copy. Inspect the migration exit status and API health, then verify a demo batch and queue processing. For rollback restore compatible application images, database, storage and secrets together. Document your deployment's retention, outbound provider access, recovery contacts and log access. Never publish backups, even for support.
