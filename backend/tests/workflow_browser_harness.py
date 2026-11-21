"""Loopback ASGI server, fresh UI build and synthetic I/O for the workflow browser regression."""

import json
import os
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from playwright.sync_api import sync_playwright

from app.api import routes_automation
from app.core.config import get_settings
from app.db.base import utcnow
from app.main import create_app
from app.services import outbox
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.mailbox.base import HandoffOutcome, OutboundEmailMessage, SendResult
from app.tasks import pipeline
from tests.conftest import build_pdf_bytes

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
DOMAIN = "oasisfacilities.test"
COMPANY = "Oasis Facilities"
TITLE = "CAFM Administrator"
TO = f"careers@{DOMAIN}"
CC = f"team@{DOMAIN}"
EXTRA_CC = f"hr@{DOMAIN}"
RECEIPT = "synthetic-mail-receipt-1"
VACANCY = f"https://{DOMAIN}/careers/cafm-administrator"
INTRO = f"{COMPANY} is hiring a {TITLE} in Dubai, UAE. IBM Maximo, SQL and Power BI."
INSTRUCTIONS = f"Send your CV to {TO}. CC: {CC}, {EXTRA_CC}."


@pytest.fixture(scope="session")
def frontend(tmp_path_factory):
    dist = tmp_path_factory.mktemp("workflow-ui")
    subprocess.run([shutil.which("node"), "node_modules/vite/bin/vite.js", "build", "--outDir", str(dist)],
                   cwd=FRONTEND, check=True, timeout=120)
    return dist


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as pw:
        chrome = os.environ.get("WORKFLOW_CHROME")
        if not chrome and os.name == "nt":
            installed = Path(os.environ["ProgramFiles"]) / "Google/Chrome/Application/chrome.exe"
            chrome = str(installed) if installed.exists() else None
        with pw.chromium.launch(executable_path=chrome) as instance:
            yield instance


@dataclass
class SyntheticProviders:
    url: str
    name = "synthetic_search"
    search_release: threading.Event = field(default_factory=threading.Event)
    mail_release: threading.Event = field(default_factory=threading.Event)
    messages: list[OutboundEmailMessage] = field(default_factory=list)

    async def search(self, query, **kwargs):
        assert self.search_release.wait(30), "Browser did not observe the active discovery stage"
        candidate = DiscoveredCandidate(
            source_provider=self.name, source_url=VACANCY, source_type="job_post", title=TITLE,
            company_name=COMPANY, company_domain=DOMAIN, jd_text=INTRO + " " + INSTRUCTIONS,
            location_raw="Dubai, UAE", retrieved_at=utcnow(), posted_at=utcnow(),
        )
        return SearchProviderOutcome(self.name, query, [candidate])

    def fetch(self, request):
        assert request.url.host == DOMAIN
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        assert str(request.url) == VACANCY
        metadata = {"@context": "https://schema.org", "@type": "JobPosting", "title": TITLE,
                    "datePosted": utcnow().date().isoformat(), "employmentType": "FULL_TIME",
                    "hiringOrganization": {"@type": "Organization", "name": COMPANY},
                    "jobLocation": {"address": {"addressLocality": "Dubai", "addressCountry": "AE"}}}
        html = (f'<html><head><script type="application/ld+json">{json.dumps(metadata)}</script></head>'
                f'<body><main><h1>{TITLE}</h1><p>{INTRO}</p><p>{INSTRUCTIONS}</p></main></body></html>')
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    def send_message(self, message):
        self.messages.append(message)
        assert self.mail_release.wait(30), "Browser did not observe the in-flight handoff"
        return SendResult(HandoffOutcome.PROVIDER_ACCEPTED, provider_message_id=RECEIPT)


@pytest.fixture
def isolated_app(isolated_test_env, frontend, monkeypatch):
    # conftest creates a database and private storage directory per test case.
    monkeypatch.chdir(isolated_test_env)
    if os.name == "nt":
        # Storage keys include three UUIDs and pytest's case name can exceed MAX_PATH.
        monkeypatch.setenv("LOCAL_STORAGE_PATH", "\\\\?\\" + str(isolated_test_env / "private_storage"))
    for key, value in {
        "ENVIRONMENT": "development", "CELERY_TASK_ALWAYS_EAGER": "false",
        "CELERY_BROKER_URL": "memory://", "CELERY_RESULT_BACKEND": "cache+memory://",
        "LIVE_SENDING_ENABLED": "true", "LOCK_ENV_CONFIG": "true", "STORAGE_BACKEND": "local",
        "LLM_PROVIDER": "none", "SEARCH_PROVIDER": "none", "TRANSACTIONAL_EMAIL_PROVIDER": "console",
        "COOKIE_SECURE": "false",
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        providers = SyntheticProviders(origin)
        monkeypatch.setenv("APP_URL", origin)
        monkeypatch.setenv("ALLOWED_ORIGINS", origin)

        # Resolve fixture hosts offline for the real URL checks. No application socket may dial a provider.
        resolve = socket.getaddrinfo
        def offline_dns(host, *args, **kwargs):
            if host in (DOMAIN, "smtp." + DOMAIN):
                host = "93.184.216.34"
            assert host in {"127.0.0.1", "::1", "93.184.216.34"}, f"Unexpected DNS lookup: {host}"
            return resolve(host, *args, **kwargs)

        connect = socket.socket.connect
        def isolated_connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                # Windows asyncio also builds its internal socketpair over loopback.
                assert address[0] in {"127.0.0.1", "::1"}, f"Unexpected network connection: {address}"
            return connect(sock, address)

        monkeypatch.setattr(socket, "getaddrinfo", offline_dns)
        monkeypatch.setattr(socket.socket, "connect", isolated_connect)
        monkeypatch.setattr(routes_automation, "_publish_discovery_batch", pipeline.task_run_batch.run)
        monkeypatch.setattr(pipeline, "execute_batch_pipeline", partial(
            pipeline.execute_batch_pipeline, search_providers_override=[providers],
            transport_override=httpx.MockTransport(providers.fetch),
        ))
        monkeypatch.setattr(outbox, "get_mailbox_transport", lambda db, conn: providers)
        app = create_app()
        for path in ("/", "/dashboard", "/setup", "/cvs", "/batches", "/tracker"):
            app.add_api_route(path, lambda: FileResponse(frontend / "index.html"), include_in_schema=False)
        app.mount("/", StaticFiles(directory=frontend))
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            # The socket is already listening. The GET completes only after ASGI startup.
            with httpx.Client(trust_env=False, timeout=15) as client:
                client.get(origin + "/api/v1/health").raise_for_status()
            yield providers
        finally:
            providers.search_release.set()
            providers.mail_release.set()
            server.should_exit = True
            thread.join(20)
            assert not thread.is_alive(), "Isolated API failed to stop"


@contextmanager
def open_page(browser, origin, locale, directory):
    phone = locale == "ar"
    context = browser.new_context(
        base_url=origin, viewport={"width": 390 if phone else 1280, "height": 844},
        locale=locale, is_mobile=phone, has_touch=phone, service_workers="block",
        record_video_dir=directory,
    )
    unexpected = []
    def local_only(route):
        url = urlsplit(route.request.url)
        if url.scheme in {"http", "https"} and f"{url.scheme}://{url.netloc}" != origin:
            unexpected.append(route.request.url)
            route.abort()
        else:
            route.continue_()
    context.route("**/*", local_only)
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    errors = []
    events = []
    page.on("console", lambda msg: events.append(f"{msg.type}: {msg.text}"))
    page.on("pageerror", lambda error: errors.append(str(error)))
    context.on("response", lambda r: events.append(f"{r.status} {r.request.method} {r.url}"))
    try:
        yield page
        assert not errors and not unexpected, (errors, unexpected)
    except BaseException:
        page.screenshot(path=directory / "failure.png")
        (directory / "page.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
        raise
    finally:
        (directory / "events.log").write_text("\n".join(events + errors + unexpected), encoding="utf-8")
        context.tracing.stop(path=directory / "trace.zip")
        context.close()


def prepare_workspace(api, locale, *, upload_page=None):
    name, email = "Layla Mansour", "workflow@example.org"
    def checked(response):
        assert response.ok, f"{response.status} {response.url}: {response.text()}"
        return response.json()

    user = checked(api.post("/api/v1/auth/register", data={
        "email": email, "password": "SyntheticPassword123!", "full_name": name, "ui_locale": locale,
    }))["user"]
    headers = {"X-CSRF-Token": user["csrf_token"]}
    checked(api.put("/api/v1/profile", headers=headers, data={
        "full_name": name, "contact_email": email, "residence_country_code": "AE",
        "residence_city": "Dubai", "work_authorizations": ["AE"], "confirm_profile": True,
    }))
    checked(api.put("/api/v1/preferences", headers=headers, data={
        "target_titles": [TITLE], "target_locations": [{"country_code": "AE", "city": "Dubai"}],
        "min_match_score": 40, "target_goal_count": 1, "max_search_queries_per_batch": 1,
        "send_window_start_hhmm": "00:00", "send_window_end_hhmm": "00:00",
        "quiet_hours_start_hhmm": "00:00", "quiet_hours_end_hhmm": "00:00",
        "campaign_mode": "approval_required",
    }))
    checked(api.post("/api/v1/mailboxes/smtp", headers=headers, data={
        "sender_email": email, "smtp_host": "smtp." + DOMAIN, "password": "offline-fixture",
    }))
    pdfs = {}
    if upload_page is not None:
        upload_page.goto("/cvs")
    for label in ("Facilities", "Operations"):
        filename = f"Layla_{label}.pdf"
        data = build_pdf_bytes([name, email, "2020-01 to 2025-10 CAFM Administrator - Dubai, UAE",
                                "Skills: IBM Maximo, SQL, Power BI, Facilities Management", f"Portfolio: {label}"])
        if upload_page is None:
            item = checked(api.post("/api/v1/cvs/upload", headers=headers, multipart={
                "variant_label": label, "file": {"name": filename, "mimeType": "application/pdf", "buffer": data},
            }))
        else:
            upload_page.locator("#cv-variant").fill(label)
            upload_page.locator("#cv-file").set_input_files({"name": filename, "mimeType": "application/pdf", "buffer": data})
            with upload_page.expect_response(lambda r: r.url.endswith("/cvs/upload") and r.request.method == "POST") as uploaded:
                upload_page.locator("form:has(#cv-file) button[type=submit]").click()
            assert uploaded.value.ok, uploaded.value.text()
            item = uploaded.value.json()
        pdfs[item["version_id"]] = filename, data
    return pdfs, headers
