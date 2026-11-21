"""Tests for Google Sheets idempotent sync, provider contract mocks, and concurrent recipient dedupe."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from app.core.encryption import encrypt_json
from app.db.models import Application, Batch, JobPosting, SheetsConnection, SheetsRowMapping, User
from app.db.session import get_sessionmaker
from app.services.dedupe import DuplicateRecipientError, reserve_recipients_atomically
from app.services.discovery.brave import BraveSearchProvider
from app.services.discovery.tavily import TavilySearchProvider
from app.services.sheets import sync_applications_to_google_sheet


def test_google_sheets_idempotent_sync_does_not_duplicate_rows(db_session):
    user = User(email="sheets@example.org", email_normalized="sheets@example.org")
    db_session.add(user)
    db_session.flush()

    batch = Batch(user_id=user.id, mode="dry_run", status="completed")
    db_session.add(batch)
    db_session.flush()
    post = JobPosting(
        user_id=user.id,
        batch_id=batch.id,
        canonical_url_hash="u-sh",
        content_hash="c-sh",
        source_provider="demo_fixture",
        source_url="https://example.org/job",
        title="Engineer",
        company_name="SheetCo",
        jd_text="Python",
    )
    db_session.add(post)
    db_session.flush()

    app1 = Application(
        user_id=user.id,
        batch_id=batch.id,
        job_posting_id=post.id,
        company_name="SheetCo",
        role_title="Engineer",
        recipient_email="jobs@sheetco.example",
        recipient_normalized="jobs@sheetco.example",
        transport_status="sent",
    )
    db_session.add(app1)
    db_session.flush()

    conn = SheetsConnection(
        user_id=user.id,
        status="connected",
        sheet_name="Applications",
        encrypted_credentials=encrypt_json(
            {
                "access_token": "mock-token",
                "refresh_token": "mock-refresh",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            }
        ),
    )
    db_session.add(conn)
    db_session.commit()

    put_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "spreadsheets" in str(request.url):
            return httpx.Response(200, json={"spreadsheetId": "ss-123", "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/ss-123"})
        if request.method == "PUT":
            put_calls.append(str(request.url))
            return httpx.Response(200, json={"updatedRows": 1})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    res1 = sync_applications_to_google_sheet(db_session, user_id=user.id, http_transport=transport)
    assert res1["rows_synced"] == 1
    assert len(put_calls) == 2  # Header row A1 + data row A2

    # Second sync with unchanged row does NOT create a duplicate row or extra PUT
    res2 = sync_applications_to_google_sheet(db_session, user_id=user.id, http_transport=transport)
    assert res2["rows_synced"] == 0
    assert len(put_calls) == 2

    # Update application outcome -> updates exact row A2, never A3!
    app1.outcome_status = "interview"
    db_session.commit()
    res3 = sync_applications_to_google_sheet(db_session, user_id=user.id, http_transport=transport)
    assert res3["rows_synced"] == 1
    assert "Applications!A2" in put_calls[-1]
    assert db_session.query(SheetsRowMapping).count() == 1


def test_concurrent_recipient_reservation_allows_only_one_winner(db_session):
    user = User(email="conc@example.org", email_normalized="conc@example.org")
    db_session.add(user)
    db_session.commit()
    user_id = user.id

    def worker(idx: int) -> str:
        sess = get_sessionmaker()()
        try:
            reserve_recipients_atomically(
                sess,
                user_id=user_id,
                application_id=None,
                to_email="HR@TargetCo.Example",
                cc_emails=["cc@TargetCo.Example"],
            )
            sess.commit()
            return "won"
        except DuplicateRecipientError:
            sess.rollback()
            return "duplicate"
        except Exception:
            sess.rollback()
            return "locked"
        finally:
            sess.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(worker, range(4)))

    assert outcomes.count("won") == 1


async def test_search_provider_contract_adapters():
    def tavily_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["time_range"] == "week"
        return httpx.Response(200, json={"results": [{"url": "https://a.example/job", "title": "Analyst", "content": "Send CV to hr@a.example", "score": 0.9}]})

    t = TavilySearchProvider(api_key="tvly-test", transport=httpx.MockTransport(tavily_handler))
    tout = await t.search("Data Analyst", freshness_days=7)
    assert len(tout.candidates) == 1
    assert tout.candidates[0].company_domain == "a.example"

    def brave_handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["X-Subscription-Token"] == "brave-test"
        assert req.url.params["freshness"] == "pm"
        return httpx.Response(200, json={"web": {"results": [{"url": "https://b.example/careers", "title": "Engineer", "description": "Apply by email"}]}})

    b = BraveSearchProvider(api_key="brave-test", transport=httpx.MockTransport(brave_handler))
    bout = await b.search("Backend Engineer", freshness_days=20)
    assert len(bout.candidates) == 1
    assert bout.candidates[0].company_domain == "b.example"
