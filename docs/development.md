# Development

Use Python 3.13 and Node 24 to match the images. From the repository root create and activate a virtual environment in `backend/.venv`, then initialize a private SQLite development configuration. Choose a fresh checkout separate from your Compose instance.

```sh
python -m venv backend/.venv
# POSIX: source backend/.venv/bin/activate
# PowerShell: backend/.venv/Scripts/Activate.ps1
python -m pip install -r backend/requirements.lock
python scripts/generate_secrets.py --write-local-env
cd backend
python -m alembic upgrade head
python -m pytest -q
python -m ruff check .
python -m uvicorn app.main:app --port 8000
```

In a second terminal run the frontend commands below. Vite proxies the API during development. Use the installation CLI from the backend directory to obtain a first-run setup claim when needed.

```sh
cd frontend
npm ci
npm run build
npm run dev
```

For the browser suite activate the backend environment, install the `playwright` version pinned in the backend browser extra, then run `python -m playwright install chromium` and `npm run test:e2e` from frontend. The harness builds the frontend and serves it with an isolated API and simulated mailboxes. On Linux install Chromium system dependencies with `python -m playwright install --with-deps chromium`.

Keep test databases, files and browser artifacts private and disposable. Use the Python lint rules in pyproject.toml, check types with the frontend build and review migrations with tests against an empty database. CI checks integration startup and new setup-script formatting.
