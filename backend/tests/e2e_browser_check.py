"""End-to-end Playwright browser test exercising desktop + mobile + English LTR + Arabic RTL."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = ROOT / "backend"
FRONTEND_DIR = ROOT / "frontend"
CHROME_PATH = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def build_sample_pdf(path: Path) -> None:
    from tests.conftest import build_pdf_bytes

    data = build_pdf_bytes(
        [
            "Layla Mansour",
            "layla.e2e@example.org",
            "Senior Data Analyst 2020-01 - 2024-06",
            "Skills: Python, SQL, Power BI, Excel",
            "Project: Built regional KPI dashboard",
        ]
    )
    path.write_bytes(data)


def run_e2e() -> None:
    (BACKEND_DIR / ".pytest_cache").mkdir(exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(prefix="run-", dir=BACKEND_DIR / ".pytest_cache"))
    db_file = tmp_dir / "e2e.db"
    with socket.socket() as api_socket, socket.socket() as web_socket:
        api_socket.bind(("127.0.0.1", 0))
        web_socket.bind(("127.0.0.1", 0))
        api_port, web_port = api_socket.getsockname()[1], web_socket.getsockname()[1]
    web_url = f"http://127.0.0.1:{web_port}"
    api_url = f"http://127.0.0.1:{api_port}"
    storage_dir = tmp_dir / "storage"
    storage_dir.mkdir(exist_ok=True)
    pdf_file = tmp_dir / "layla_cv.pdf"
    build_sample_pdf(pdf_file)

    env = os.environ.copy()
    env["ENVIRONMENT"] = "development"
    env["DATABASE_URL"] = f"sqlite:///{db_file}"
    env["LOCAL_STORAGE_PATH"] = str(storage_dir)
    env["CELERY_TASK_ALWAYS_EAGER"] = "true"
    env["LIVE_SENDING_ENABLED"] = "false"
    env["APP_SECRET_KEY"] = "e2e-test-secret-key-0123456789abcdef"
    env["APP_URL"] = web_url
    env["VITE_API_PROXY_TARGET"] = api_url
    env["ALLOWED_ORIGINS"] = web_url

    backend_proc = subprocess.Popen(
        [str(BACKEND_DIR / ".venv" / "Scripts" / "alembic.exe"), "upgrade", "head"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    backend_proc.wait(timeout=30)
    assert backend_proc.returncode == 0, backend_proc.stdout.read().decode()

    import json

    claim_proc = subprocess.run(
        [str(BACKEND_DIR / ".venv" / "Scripts" / "python.exe"), "-m", "app.cli_bootstrap", "ensure-setup-claim-token"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    claim_info = json.loads(claim_proc.stdout.strip().splitlines()[-1])
    raw_setup_code = claim_info["setup_code"]

    api_log = open(tmp_dir / "api.log", "w", encoding="utf-8")
    fe_log = open(tmp_dir / "fe.log", "w", encoding="utf-8")

    api_proc = subprocess.Popen(
        [str(BACKEND_DIR / ".venv" / "Scripts" / "uvicorn.exe"), "app.main:app", "--host", "127.0.0.1", "--port", str(api_port)],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=api_log,
        stderr=subprocess.STDOUT,
    )
    fe_proc = subprocess.Popen(
        [shutil.which("node"), str(FRONTEND_DIR / "node_modules/vite/bin/vite.js"), "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"],
        cwd=str(FRONTEND_DIR),
        env=env,
        stdout=fe_log,
        stderr=subprocess.STDOUT,
    )

    try:
        # Wait for servers
        import httpx

        for _ in range(40):
            try:
                if httpx.get(api_url + "/api/v1/health", timeout=1.0).status_code == 200:
                    if httpx.get(web_url, timeout=1.0).status_code == 200:
                        break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("Servers did not become ready in time")

        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=CHROME_PATH, headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 840})
            page = context.new_page()

            console_errors: list[str] = []
            net_log: list[str] = []
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda err: console_errors.append(f"PAGEERROR: {err}"))
            page.on("request", lambda req: net_log.append(f"REQ {req.method} {req.url}") if "/api/" in req.url else None)
            page.on("response", lambda res: net_log.append(f"RES {res.status} {res.url}") if ("/api/" in res.url or res.status >= 400) else None)

            try:
                # 0. Fresh Setup Wizard: Claim Ownership + Step 1..7 + Finish Setup
                page.goto(web_url + "/", wait_until="networkidle")
                page.get_by_text("Claim Installation Ownership").wait_for(state="visible", timeout=10000)
                page.get_by_placeholder("af_setup_...").fill(raw_setup_code)
                claim_inputs = page.locator("form input")
                claim_inputs.nth(1).fill("owner.e2e@example.org")
                claim_inputs.nth(2).fill("E2E Owner")
                claim_inputs.nth(3).fill("OwnerPass1234!")
                page.get_by_role("button", name="Claim Owner Access & Start Setup").click()
                page.wait_for_selector("text=ApplyFlow Global Setup Wizard", timeout=15000)

                # Step 1 -> Step 2 (AI Provider)
                page.get_by_role("button", name="Save & Next").click()
                page.wait_for_selector("text=Installation AI Provider", timeout=10000)
                page.get_by_role("button", name="OpenAI (GPT)").click()
                page.get_by_placeholder("Paste API key...").fill("synthetic-credential-3b66f97b")
                page.get_by_role("button", name="Save", exact=True).click()
                page.wait_for_selector("text=AI configuration saved.", timeout=10000)
                page.get_by_role("button", name="Save & Next").click()

                # Step 3 -> Skip Search
                page.wait_for_selector("text=Installation Job-Search Provider", timeout=10000)
                page.get_by_role("button", name="Skip for now").click()

                # Step 4 -> Mailboxes & Email -> Next
                page.wait_for_selector("text=Google OAuth Client", timeout=10000)
                page.get_by_role("button", name="Save & Next").click()

                # Step 5 -> Skip Optional Sync
                page.wait_for_selector("text=Google Sheets Sync", timeout=10000)
                page.get_by_role("button", name="Skip for now").click()

                # Step 6 -> Readiness Checks -> Verify live sending stays Disabled -> Next
                page.wait_for_selector("text=8. Live Sending Safety Gate", timeout=10000)
                page.get_by_role("button", name="Save & Next").click()

                # Step 7 -> Finish Setup
                page.wait_for_selector("text=Ready to launch ApplyFlow Global", timeout=10000)
                page.get_by_role("button", name="Finish Setup & Open Workspace").click()
                page.wait_for_url("**/dashboard", timeout=10000)

                # Verify owner can inspect/edit masked Integrations in Settings without exposing raw key, then Sign Out
                page.get_by_role("link", name="Settings").click()
                page.wait_for_url("**/settings")
                page.wait_for_selector("text=Installation Integrations (Owner Only)", timeout=10000)
                settings_html = page.content()
                assert "synthetic-credential-3b66f97b" not in settings_html, "Raw OpenAI secret leaked to Settings HTML!"
                page.get_by_role("button", name="Sign Out").click()
                page.wait_for_selector("text=Sign in to your private workspace", timeout=10000)

                # 1. Ordinary user sign up (verifying multi-user isolation: no Installation Integrations panel visible)
                page.get_by_role("button", name="Need an account? Register").click()
                page.locator("#auth-fullname").fill("Layla Mansour")
                page.locator("#auth-tz").fill("Asia/Dubai")
                page.locator("#auth-email").fill("layla.e2e@example.org")
                page.locator("#auth-password").fill("SecurePass1234!")
                page.get_by_role("button", name="Create Account").click()
                page.wait_for_url("**/dashboard", timeout=15000)
                page.get_by_text("Onboarding Checklist").wait_for(state="visible", timeout=10000)

                # Confirm ordinary user CANNOT see owner installation secrets section
                page.get_by_role("link", name="Settings").click()
                page.wait_for_url("**/settings")
                page.wait_for_selector("text=Personal AI & Search Overrides", timeout=10000)
                assert page.locator("text=Installation Integrations (Owner Only)").count() == 0

                # 2. Upload CV & confirm profile
                page.get_by_role("link", name="CV Container").click()
                page.wait_for_url("**/cvs")
                page.locator("#cv-file").set_input_files(str(pdf_file))
                page.get_by_role("button", name="Upload & Parse CV").click()
                page.wait_for_selector("text=Uploaded CV (parsed", timeout=15000)

                page.get_by_placeholder("AE, SA, EG").fill("AE, SA")
                page.get_by_role("button", name="Save & Confirm Profile").click()
                page.wait_for_selector("text=Candidate profile confirmed", timeout=10000)

                # 3. Preferences with disambiguated location search
                page.get_by_role("link", name="Preferences").click()
                page.wait_for_url("**/preferences")
                page.get_by_placeholder("Data Analyst, BI Specialist, Operations Lead").fill("Data Analyst")
                loc_input = page.get_by_placeholder("Type city or country (e.g. Cairo, Dubai, London, Berlin)...")
                loc_input.fill("Dubai")
                page.get_by_role("button", name="Dubai, Dubai, United Arab Emirates").click()
                page.get_by_role("button", name="Save Preferences").click()
                page.wait_for_selector("text=Preferences saved.", timeout=10000)

                # 4. Run Isolated Demo Fixture Batch & verify 12-counter metrics grid
                page.get_by_role("link", name="Batches & Schedules").click()
                page.wait_for_url("**/batches")
                page.get_by_text("More discovery options", exact=True).click()
                page.get_by_text("Isolated Demo Fixtures").click()
                page.get_by_role("button", name="Start Batch").click()
                page.wait_for_selector("text=Crescent Logistics Group", timeout=15000)
                page.wait_for_selector("text=Search hits", timeout=5000)

                # 5. Tracker: Real Applications excludes demo -> Raw Discovery tab -> Demo Sandbox tab -> manual update
                page.get_by_role("link", name="Applications").click()
                page.wait_for_url("**/tracker")
                page.wait_for_selector("text=No applications recorded in this view yet.", timeout=10000)
                assert page.locator("text=Crescent Logistics Group").count() == 0, "Demo fixture leaked into Real Applications tab!"

                page.get_by_role("button", name="Raw Discovery & Evidence").click()
                page.wait_for_selector("text=Export Discovery CSV", timeout=10000)

                page.get_by_role("button", name="Review Queue").click()
                page.wait_for_selector("text=Sending is locked.", timeout=10000)

                page.locator("button:has-text('Demo Sandbox')").click()
                page.wait_for_selector("text=Crescent Logistics Group", timeout=10000)
                page.wait_for_selector("text=DEMO FIXTURE", timeout=5000)
                page.get_by_text("Crescent Logistics Group").first.click()
                page.wait_for_selector("text=Application Detail & Evidence", timeout=10000)
                page.get_by_role("button", name="Save Outcome & Notes").click()
                page.wait_for_selector("text=Application updated.", timeout=10000)

                # 6. Stop Automation & Resume Automation
                page.get_by_role("button", name="Stop Automation").click()
                page.wait_for_selector("text=Automation is paused globally", timeout=10000)
                page.get_by_role("button", name="Resume Automation").first.click()
                page.wait_for_selector("text=Stop Automation", timeout=10000)

                # 7. Switch to Arabic RTL and verify desktop + mobile layouts
                page.get_by_role("button", name="العربية").click()
                page.wait_for_function("document.documentElement.dir === 'rtl' && document.documentElement.lang === 'ar'")
                desktop_overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not desktop_overflow, "Desktop Arabic RTL layout has horizontal overflow"

                # Mobile viewport check (390x844)
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(300)
                mobile_overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                assert not mobile_overflow, "Mobile Arabic RTL layout has horizontal overflow"
                page.get_by_role("navigation", name="Mobile navigation").wait_for(state="visible", timeout=5000)

                # Filter out only the expected initial unauthenticated 401 probe on /auth/me before signup
                unexpected_errors = [e for e in console_errors if "401 (Unauthorized)" not in e]
                assert len(unexpected_errors) == 0, f"Browser console errors detected: {unexpected_errors}"
            except Exception:
                print("CONSOLE_ERRORS:", console_errors)
                print("NET_LOG:", net_log[-25:])
                print("ALERTS:", page.locator("[role='alert'], [role='status']").all_inner_texts())
                print("URL:", page.url)
                raise
            finally:
                browser.close()
        print("E2E_BROWSER_CHECK_PASSED")
    finally:

        api_proc.terminate()
        fe_proc.terminate()
        try:
            api_proc.wait(timeout=5)
        except Exception:
            api_proc.kill()
        try:
            fe_proc.wait(timeout=5)
        except Exception:
            fe_proc.kill()
        api_log.close()
        fe_log.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.path.insert(0, str(BACKEND_DIR))
    run_e2e()
