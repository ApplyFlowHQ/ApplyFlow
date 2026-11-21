"""Tests for multi-tenant isolation, auth lifecycle, SSRF defense, CSRF, and account deletion."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.ssrf import SSRFBlockedError, validate_external_url, validate_mail_host
from tests.conftest import build_pdf_bytes


def _register_and_auth(client: TestClient, email: str, password: str = "StrongPass123!") -> tuple[dict, dict]:
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": email.split("@")[0], "timezone": "UTC"},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    csrf = data["user"]["csrf_token"]
    return data, {"X-CSRF-Token": csrf}


def test_tenant_isolation_and_account_lifecycle(client: TestClient):
    # Register User A
    reg_a, headers_a = _register_and_auth(client, "alice@example.org")
    ver_tok = reg_a["verification_token_for_test"]
    vr = client.post("/api/v1/auth/verify-email", json={"token": ver_tok}, headers=headers_a)
    assert vr.status_code == 200 and vr.json()["verified"] is True

    # User A uploads a private CV
    pdf_bytes = build_pdf_bytes(
        [
            "Alice Data",
            "alice@example.org",
            "Senior Data Analyst 2020-01 - 2024-01",
            "Skills: Python, SQL, Power BI, Excel",
        ]
    )
    up = client.post(
        "/api/v1/cvs/upload",
        data={"variant_label": "Analytics"},
        files={"file": ("alice_cv.pdf", pdf_bytes, "application/pdf")},
        headers=headers_a,
    )
    assert up.status_code == 201, up.text
    cv_info = up.json()

    # User A runs an isolated demo_fixture dry run
    client.put(
        "/api/v1/profile",
        json={
            "full_name": "Alice Data",
            "contact_email": "alice@example.org",
            "residence_country_code": "AE",
            "residence_city": "Dubai",
            "work_authorizations": ["AE", "EG"],
            "confirm_profile": True,
        },
        headers=headers_a,
    )
    b = client.post(
        "/api/v1/batches",
        json={"mode": "demo_fixture", "run_immediately": True},
        headers={**headers_a, "Idempotency-Key": "batch-a-1"},
    )
    assert b.status_code == 201, b.text
    batch_id = b.json()["batch_id"]
    # Default /applications hides demo fixtures; ?scope=demo exposes User A's isolated demo sandbox
    assert client.get("/api/v1/applications").json()["total"] == 0
    assert "Crescent Logistics" not in client.get("/api/v1/exports/csv").text
    apps_a = client.get("/api/v1/applications?scope=demo").json()["applications"]
    assert len(apps_a) >= 1
    app_a_id = apps_a[0]["id"]

    # Register User B on separate cookie jar
    client_b = TestClient(client.app)
    _, headers_b = _register_and_auth(client_b, "bob@example.org")

    # User B MUST NOT access User A's CV, download, batch, application, export, or operator routes
    assert client_b.get(f"/api/v1/cvs/{cv_info['document_id']}/versions/{cv_info['version_id']}").status_code == 404
    assert client_b.get(f"/api/v1/cvs/{cv_info['document_id']}/versions/{cv_info['version_id']}/download").status_code == 404
    assert client_b.get(f"/api/v1/batches/{batch_id}").status_code == 404
    assert client_b.post(f"/api/v1/batches/{batch_id}/cancel", headers=headers_b).status_code == 404
    assert client_b.get(f"/api/v1/applications/{app_a_id}").status_code == 404
    assert client_b.get("/api/v1/applications").json()["total"] == 0
    assert client_b.get("/api/v1/applications?scope=demo").json()["total"] == 0
    assert "Crescent Logistics" not in client_b.get("/api/v1/exports/csv?scope=demo").text
    assert client_b.get("/api/v1/operator/overview").status_code == 403

    # CSRF missing check on state-changing POST
    assert client.post("/api/v1/account/stop-automation").status_code == 403

    # User A deletes account -> removes session and blocks further access
    del_res = client.delete("/api/v1/account", headers=headers_a)
    assert del_res.status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_ssrf_and_upload_magic_byte_defenses(client: TestClient):
    for bad_url in [
        "http://127.0.0.1:8000/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost/internal",
        "file:///etc/passwd",
        "http://10.0.0.1/secret",
    ]:
        with pytest.raises(SSRFBlockedError):
            validate_external_url(bad_url)

    with pytest.raises(SSRFBlockedError):
        validate_mail_host("127.0.0.1", 587)
    with pytest.raises(SSRFBlockedError):
        validate_mail_host("smtp.example.com", 22)

    _, headers = _register_and_auth(client, "uploadtest@example.org")
    # Spoofed executable disguised as .pdf
    spoof = client.post(
        "/api/v1/cvs/upload",
        data={"variant_label": "Bad"},
        files={"file": ("malware.pdf", b"MZ\x90\x00not_a_pdf", "application/pdf")},
        headers=headers,
    )
    assert spoof.status_code == 400
    assert spoof.json()["detail"]["code"] == "INVALID_FILE_TYPE"
