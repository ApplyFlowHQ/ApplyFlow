# Install a local demo

Use a fresh clone in a private directory, with Python 3.12+ and Docker running Linux containers. Check `python --version`, `docker version` and `docker compose version`. Compose v2.24+ supports the optional env-file syntax in the service definitions. Choose an unused project name for each installation to isolate its volumes. The following commands assume the repository root.

```sh
python scripts/init_compose_env.py
docker compose -p applyflow-demo --env-file .env -f deploy/docker-compose.yml config --quiet
docker compose -p applyflow-demo --env-file .env -f deploy/docker-compose.yml up -d --build --wait
docker compose -p applyflow-demo --env-file .env -f deploy/docker-compose.yml ps -a
docker compose -p applyflow-demo --env-file .env -f deploy/docker-compose.yml exec backend python -m app.cli_bootstrap ensure-setup-claim-token
```

The generator creates the DB password, connection URL, session secret and Fernet key. It refuses to overwrite an existing file. Restrict access to your OS account. Keep the setup code in your private console. On an unclaimed installation open [setup](http://localhost:8180/setup). Refresh [health](http://localhost:8180/api/v1/health) to confirm the proxy reaches the API. Use a fictional profile and the demo batch first.

Alembic upgrades the DB before the worker and API start. An exited migration container with code zero is expected. To stop the demo while retaining volumes:

```sh
docker compose -p applyflow-demo --env-file .env -f deploy/docker-compose.yml down
```

The Windows bootstrapper also supports Docker and a local Python/Vite mode. It maintains its own private bootstrap state. Use the same installation method and project name when restarting an existing workspace. [Troubleshooting](troubleshooting.md)
The Windows launcher defaults to the applyflow-global Compose project. For a parallel evaluation on an existing host, use the named applyflow-demo commands above.
