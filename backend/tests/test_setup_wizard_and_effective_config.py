"""Comprehensive verification of the Setup Wizard, bootstrap CLI, effective config, encryption, RBAC, and connection tests."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import cli_bootstrap
from app.core.config import get_settings
from app.core.encryption import hash_token
from app.db.base import utcnow
from app.db.models import AuditEvent, Batch, InstallationConfig, SetupClaimToken, User
from app.db.session import get_sessionmaker, reset_engine
from app.main import create_app
from app.services import connection_tests
from app.services.ai.grounding import get_configured_llm_provider
from app.services.effective_config import (
    resolve_effective_ai_config,
    resolve_effective_google_oauth,
    resolve_effective_ms_graph_oauth,
    resolve_effective_search_config,
)
from app.services.transactional_email import send_transactional_email


def _issue_claim_code_in_db(db_session, *, expires_delta=timedelta(hours=12), purpose="initial_claim") -> str:
    raw = "af_setup_test-one-time-high-entropy-token-001"
    db_session.add(
        SetupClaimToken(
            token_hash=hash_token(raw),
            purpose=purpose,
            expires_at=utcnow() + expires_delta,
        )
    )
    db_session.commit()
    return raw


def test_fresh_startup_works_without_any_provider_keys(client: TestClient):
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    h_data = health.json()
    assert h_data["status"] == "ok"
    assert h_data["llm_provider"] == "none"
    assert h_data["search_provider"] == "none"
    assert h_data["live_sending_enabled"] is False

    pub_setup = client.get("/api/v1/setup/status")
    assert pub_setup.status_code == 200
    assert pub_setup.json()["claimed"] is False
    assert pub_setup.json()["requires_claim"] is True
    assert pub_setup.json()["installation_kind"] == "fresh"
    # Public response must never expose secret/admin keys
    assert "installation" not in pub_setup.json()
    assert "Cache-Control" in pub_setup.headers
    assert "no-store" in pub_setup.headers["Cache-Control"]


def test_bootstrap_cli_hashes_token_and_detects_missing_encryption_key(tmp_path, monkeypatch: pytest.MonkeyPatch, capsys):
    # Ensure production encryption integrity check detects missing MAILBOX_ENCRYPTION_KEY when encrypted rows exist
    res_claim = cli_bootstrap.ensure_setup_claim_token(ttl_hours=12)
    assert res_claim == 0
    captured = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert captured["status"] == "token_issued"
    assert captured["setup_code"].startswith("af_setup_")

    # Verify DB holds only hash, never plaintext code
    with get_sessionmaker()() as db:
        tok = db.scalar(select(SetupClaimToken))
        assert tok is not None
        assert tok.token_hash != captured["setup_code"]
        assert tok.token_hash == hash_token(captured["setup_code"])

        # Simulate stored encrypted installation secrets
        inst = db.scalar(select(InstallationConfig).where(InstallationConfig.id == "default"))
        assert inst is not None
        from app.core.encryption import encrypt_json

        inst.encrypted_secrets_json = encrypt_json({"openai_api_key": "synthetic-credential-8d1fb8d2"})
        db.commit()

    # Routine check in matching environment succeeds
    assert cli_bootstrap.check_encryption_integrity() == 0

    # Switching to production with missing MAILBOX_ENCRYPTION_KEY must fail loudly (exit code 2)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("MAILBOX_ENCRYPTION_KEY", "")
    get_settings.cache_clear()
    rc = cli_bootstrap.check_encryption_integrity()
    assert rc == 2
    err_json = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert err_json["status"] == "recovery_problem"

    # Wrong key must also fail cleanly
    monkeypatch.setenv("MAILBOX_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    get_settings.cache_clear()
    assert cli_bootstrap.check_encryption_integrity() == 2


def test_atomic_claim_url_rejection_rate_limit_and_concurrency(db_session, capsys):
    raw_code = _issue_claim_code_in_db(db_session)
    app = create_app()
    client = TestClient(app)

    # Code in URL query string is rejected immediately
    url_reject = client.post(
        f"/api/v1/setup/claim?setup_code={raw_code}",
        json={"setup_code": raw_code, "email": "owner@example.com", "password": "OwnerPassword123!"},
    )
    assert url_reject.status_code == 400
    assert url_reject.json()["detail"]["code"] == "CODE_IN_URL_FORBIDDEN"

    # Rate-limit after 5 invalid code guesses
    for _ in range(5):
        bad = client.post(
            "/api/v1/setup/claim",
            json={"setup_code": "wrong-code-attempt", "email": "owner@example.com", "password": "OwnerPassword123!"},
        )
        assert bad.status_code == 403

    locked = client.post(
        "/api/v1/setup/claim",
        json={"setup_code": raw_code, "email": "owner@example.com", "password": "OwnerPassword123!"},
    )
    assert locked.status_code == 429

    # Reset lockout via new token and verify strictly 1 winner in concurrent claim race
    with get_sessionmaker()() as db:
        tok = db.scalar(select(SetupClaimToken))
        tok.locked_until = None
        tok.attempt_count = 0
        db.commit()

    barrier = threading.Barrier(2)
    outcomes: list[int] = []
    lock = threading.Lock()

    def _race(email: str):
        with TestClient(app) as tc:
            barrier.wait()
            resp = tc.post(
                "/api/v1/setup/claim",
                json={"setup_code": raw_code, "email": email, "password": "OwnerPassword123!"},
            )
            with lock:
                outcomes.append(resp.status_code)

    t1 = threading.Thread(target=_race, args=("owner1@example.com",))
    t2 = threading.Thread(target=_race, args=("owner2@example.com",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(outcomes) == [200, 409]
    with get_sessionmaker()() as db:
        inst = db.scalar(select(InstallationConfig).where(InstallationConfig.id == "default"))
        assert inst.owner_user_id is not None
        owner = db.scalar(select(User).where(User.id == inst.owner_user_id))
        assert owner.is_operator is True
        # First owner can complete setup without pre-configured SMTP verification email
        assert owner.email_verified_at is not None


def test_rbac_encryption_masking_and_multi_tenant_isolation(client: TestClient, db_session):
    raw_code = _issue_claim_code_in_db(db_session)

    # Normal user registers first (NOT promoted to owner, remains unverified normal user)
    reg = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "AlicePassword123!", "full_name": "Alice"},
    )
    assert reg.status_code == 201
    alice_csrf = reg.json()["user"]["csrf_token"]
    assert reg.json()["user"]["is_owner"] is False
    assert reg.json()["user"]["is_operator"] is False
    assert reg.json()["user"]["email_verified"] is False

    # Ordinary user cannot configure installation settings or test installation endpoints
    for method, path, body in [
        ("PUT", "/api/v1/setup/installation/ai", {"provider": "openai", "api_key": "sk-hack-123"}),
        ("PUT", "/api/v1/setup/installation/search", {"provider": "tavily", "tavily_api_key": "tvly-hack"}),
        ("POST", "/api/v1/setup/live-sending", {"enabled": True}),
        ("POST", "/api/v1/setup/complete", {}),
        ("POST", "/api/v1/integrations/test", {"scope": "installation", "integration": "openai"}),
    ]:
        r = client.request(method, path, json=body, headers={"X-CSRF-Token": alice_csrf})
        assert r.status_code == 403, f"Expected 403 for {method} {path}, got {r.status_code}"

    # Owner claims ownership cleanly via separate client
    owner_client = TestClient(client.app)
    claim_r = owner_client.post(
        "/api/v1/setup/claim",
        json={"setup_code": raw_code, "email": "admin@example.com", "password": "AdminPassword123!"},
    )
    assert claim_r.status_code == 200
    owner_csrf = claim_r.json()["user"]["csrf_token"]

    # Owner saves sensitive secrets
    secret_ai = "synthetic-credential-de630f9e"
    secret_tavily = "tvly-dev-supersecret-tavily-112233"
    r_ai = owner_client.put(
        "/api/v1/setup/installation/ai",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": secret_ai, "allow_shared_ai_for_users": False},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert r_ai.status_code == 200
    assert secret_ai not in r_ai.text
    assert r_ai.json()["installation"]["ai"]["masked_openai_key"].startswith(secret_ai[:4])
    assert "••••" in r_ai.json()["installation"]["ai"]["masked_openai_key"]

    r_s = owner_client.put(
        "/api/v1/setup/installation/search",
        json={"provider": "tavily", "tavily_api_key": secret_tavily, "allow_shared_search_for_users": False},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert r_s.status_code == 200
    assert secret_tavily not in r_s.text

    # DB encrypted_secrets_json and AuditEvent rows must NEVER contain plaintext secrets
    with get_sessionmaker()() as db:
        inst = db.scalar(select(InstallationConfig).where(InstallationConfig.id == "default"))
        assert secret_ai not in inst.encrypted_secrets_json
        assert secret_tavily not in inst.encrypted_secrets_json
        audits = db.scalars(select(AuditEvent)).all()
        for a in audits:
            dumped = json.dumps(a.details_json)
            assert secret_ai not in dumped
            assert secret_tavily not in dumped

    # Submitting masked placeholder or empty string preserves original key cleanly
    r_preserve = owner_client.put(
        "/api/v1/setup/installation/ai",
        json={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-p••••7766"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert r_preserve.status_code == 200
    with get_sessionmaker()() as db:
        eff = resolve_effective_ai_config(db, user_id=None)
        assert eff.api_key == secret_ai
        assert eff.model == "gpt-4o-mini"

    # Alice still sees no installation secrets and cannot consume owner keys when allow_shared=False
    alice_status = client.get("/api/v1/setup/status").json()
    assert "installation" not in alice_status
    assert secret_ai not in json.dumps(alice_status)
    assert alice_status["user_integrations"]["ai"]["effective_provider"] == "none"

    # Alice saves her own personal Gemini key; Bob (a third user) remains isolated
    alice_gemini = "AIzaSyAlicePrivateKey000111222"
    r_ua = client.put(
        "/api/v1/integrations/user",
        json={"ai_provider": "gemini", "ai_model": "gemini-3.8-flash", "gemini_api_key": alice_gemini},
        headers={"X-CSRF-Token": alice_csrf},
    )
    assert r_ua.status_code == 200
    assert alice_gemini not in r_ua.text
    assert r_ua.json()["user_integrations"]["ai"]["effective_provider"] == "gemini"
    assert r_ua.json()["user_integrations"]["ai"]["effective_source"] == "user"

    bob_client = TestClient(client.app)
    reg_bob = bob_client.post(
        "/api/v1/auth/register",
        json={"email": "bob@example.com", "password": "BobPassword12345!", "full_name": "Bob"},
    )
    assert reg_bob.status_code == 201
    bob_id = reg_bob.json()["user"]["id"]
    with get_sessionmaker()() as db:
        bob_ai = resolve_effective_ai_config(db, user_id=bob_id)
        assert bob_ai.provider == "none"
        assert bob_ai.api_key == ""


def test_wizard_resume_finish_safe_defaults_and_worker_dynamic_config(db_session, tmp_path, monkeypatch: pytest.MonkeyPatch):
    app = create_app()
    raw_code = _issue_claim_code_in_db(db_session)
    owner_client = TestClient(app)

    c_resp = owner_client.post(
        "/api/v1/setup/claim",
        json={"setup_code": raw_code, "email": "owner@global.test", "password": "OwnerPassword123!"},
    )
    csrf = c_resp.json()["user"]["csrf_token"]
    owner_id = c_resp.json()["user"]["id"]

    # Skip step 2 & move to step 4
    p1 = owner_client.patch(
        "/api/v1/setup/progress",
        json={"current_step": 4, "complete_step": 1, "skip_step": 2, "deployment_mode": "hosted", "default_locale": "ar"},
        headers={"X-CSRF-Token": csrf},
    ).json()
    assert p1["installation_kind"] == "resumed"
    assert p1["current_step"] == 4
    assert 2 in p1["skipped_steps"]
    assert 1 in p1["completed_steps"]

    # Configure Gemini + Brave + Google OAuth + MS Graph dynamically via API (with env unset!)
    owner_client.put(
        "/api/v1/setup/installation/ai",
        json={"provider": "gemini", "model": "gemini-3.8-flash", "api_key": "AIzaSyOwnerDynamicKey123"},
        headers={"X-CSRF-Token": csrf},
    )
    owner_client.put(
        "/api/v1/setup/installation/search",
        json={"provider": "brave", "brave_api_key": "BSA_OwnerDynamicSearch999"},
        headers={"X-CSRF-Token": csrf},
    )
    owner_client.put(
        "/api/v1/setup/installation/google-oauth",
        json={"client_id": "123.apps.googleusercontent.com", "client_secret": "gsec-owner-dynamic"},
        headers={"X-CSRF-Token": csrf},
    )
    owner_client.put(
        "/api/v1/setup/installation/ms-graph",
        json={"client_id": "ms-cid-123", "tenant_id": "common", "client_secret": "ms-csec-456"},
        headers={"X-CSRF-Token": csrf},
    )

    # Complete wizard -> NEVER enables live sending & NEVER starts batches
    done = owner_client.post("/api/v1/setup/complete", headers={"X-CSRF-Token": csrf})
    assert done.status_code == 200
    assert done.json()["setup_state"] == "completed"
    assert done.json()["installation"]["live_sending_enabled"] is False

    # Verify workers/services dynamically read saved config without restarting
    with get_sessionmaker()() as db:
        assert db.scalars(select(Batch)).all() == []
        llm = get_configured_llm_provider(db=db, user_id=owner_id)
        assert llm is not None
        assert llm.name == "gemini"
        assert llm.api_key == "AIzaSyOwnerDynamicKey123"
        srch = resolve_effective_search_config(db, user_id=owner_id)
        assert srch.provider == "brave"
        assert srch.brave_api_key == "BSA_OwnerDynamicSearch999"
        go = resolve_effective_google_oauth(db)
        assert go.client_id == "123.apps.googleusercontent.com"
        assert go.client_secret == "gsec-owner-dynamic"
        ms = resolve_effective_ms_graph_oauth(db)
        assert ms.client_id == "ms-cid-123"
        assert send_transactional_email("owner@global.test", "Test", "Body", db=db)["provider"] == "console"

    # Simulated persistence across engine reset & restart
    db_file = tmp_path / "test_applyflow.db"
    engine2 = reset_engine(f"sqlite:///{db_file}")
    assert engine2 is not None
    status_after_restart = owner_client.get("/api/v1/setup/status").json()
    assert status_after_restart["setup_state"] == "completed"
    assert status_after_restart["installation"]["ai"]["provider"] == "gemini"

    # LOCK_ENV_CONFIG=true overrides DB config cleanly
    monkeypatch.setenv("LOCK_ENV_CONFIG", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-credential-c5f672fb")
    monkeypatch.setenv("LIVE_SENDING_ENABLED", "false")
    get_settings.cache_clear()
    with get_sessionmaker()() as db:
        eff_locked = resolve_effective_ai_config(db, user_id=owner_id)
        assert eff_locked.source == "env_locked"
        assert eff_locked.provider == "openai"
        assert eff_locked.api_key == "synthetic-credential-c5f672fb"
    reject_toggle = owner_client.post("/api/v1/setup/live-sending", json={"enabled": True}, headers={"X-CSRF-Token": csrf})
    assert reject_toggle.status_code == 409


@pytest.mark.asyncio
async def test_mocked_connection_tests_and_ssrf_protection(db_session, client: TestClient):
    # OpenAI /v1/models check (non-billable)
    openai_ok_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [{"id": "gpt-4.1-mini"}, {"id": "gpt-4o-mini"}]})
    )
    res_oai = await connection_tests.test_openai_connection("sk-valid-key", "gpt-4.1-mini", transport=openai_ok_transport)
    assert res_oai.ok is True
    assert res_oai.status == "verified"

    openai_bad_transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"message": "bad key"}}))
    res_oai_bad = await connection_tests.test_openai_connection("sk-bad-key-1234", "gpt-4.1-mini", transport=openai_bad_transport)
    assert res_oai_bad.ok is False
    assert res_oai_bad.status == "needs_attention"
    assert "sk-bad-key-1234" not in res_oai_bad.message

    # Gemini /v1beta/models check (non-billable header auth)
    gemini_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"models": [{"name": "models/gemini-3.8-flash"}]})
        if req.headers.get("x-goog-api-key") == "valid-gem"
        else httpx.Response(403, json={})
    )
    res_gem = await connection_tests.test_gemini_connection("valid-gem", "gemini-3.8-flash", transport=gemini_transport)
    assert res_gem.ok is True
    assert res_gem.status == "verified"

    # Tavily /usage check (non-billable)
    tavily_transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"account": {"current_plan": "Bootstrap"}})
        if req.url.path == "/usage"
        else httpx.Response(404)
    )
    res_tav = await connection_tests.test_tavily_connection("tvly-good", transport=tavily_transport)
    assert res_tav.ok is True
    assert res_tav.details["plan"] == "Bootstrap"

    # Brave 1-query check
    brave_transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"web": {"results": []}}))
    res_brv = await connection_tests.test_brave_connection("BSA-good", transport=brave_transport)
    assert res_brv.ok is True
    assert res_brv.details["queries_used"] == 1

    # SSRF defense blocks internal/loopback SMTP host during transactional test & user SMTP test
    res_ssrf = await connection_tests.test_transactional_smtp(
        provider="smtp",
        host="127.0.0.1",
        port=587,
        username="u",
        password="secret-pw",
        use_tls=True,
        from_email="no-reply@example.com",
    )
    assert res_ssrf.ok is False
    assert res_ssrf.status == "needs_attention"
    assert "secret-pw" not in res_ssrf.message

    # Unsaved failed key test must NOT erase previously saved working key
    raw_code = _issue_claim_code_in_db(db_session)
    claim_r = client.post(
        "/api/v1/setup/claim",
        json={"setup_code": raw_code, "email": "tester@example.com", "password": "OwnerPassword123!"},
    )
    csrf = claim_r.json()["user"]["csrf_token"]
    client.put(
        "/api/v1/setup/installation/ai",
        json={"provider": "openai", "model": "gpt-4.1-mini", "api_key": "synthetic-credential-f9d59090"},
        headers={"X-CSRF-Token": csrf},
    )

    async def _fake_bad_oai(api_key, model="", **_kwargs):
        return connection_tests.ConnectionTestResult(ok=False, status="needs_attention", message="Mock rejected key")

    orig = connection_tests.test_openai_connection
    try:
        from app.api import routes_setup

        routes_setup.test_openai_connection = _fake_bad_oai
        t_resp = client.post(
            "/api/v1/integrations/test",
            json={"scope": "installation", "integration": "openai", "api_key": "sk-bad-new-key"},
            headers={"X-CSRF-Token": csrf},
        )
        assert t_resp.status_code == 200
        assert t_resp.json()["ok"] is False
        assert t_resp.json()["tested_unsaved_input"] is True
        # Saved working key remains untouched!
        with get_sessionmaker()() as db:
            assert resolve_effective_ai_config(db).api_key == "synthetic-credential-f9d59090"
    finally:
        from app.api import routes_setup

        routes_setup.test_openai_connection = orig

    # Explicit send test email goes strictly to owner's own address
    self_mail = client.post("/api/v1/integrations/send-test-email", headers={"X-CSRF-Token": csrf})
    assert self_mail.status_code == 200
    assert self_mail.json()["details"]["recipient"] == "tester@example.com"
