"""Real Chromium + loopback API. Run from backend with python -m pytest ../frontend/tests/test_send_coverage.py."""

from datetime import UTC, datetime

import pytest
from app.db.models import GmailSentRecord, MailboxConnection, OutboxEntry
from app.db.session import session_scope
from playwright.sync_api import expect
from sqlalchemy import func, select
from tests import conftest as backend_fixtures
from tests.workflow_browser_harness import open_page, prepare_workspace

pytest_plugins = ["tests.workflow_browser_harness"]
isolated_test_env = backend_fixtures.isolated_test_env


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_partial_scan_confirmation(browser, isolated_app, tmp_path, locale):
    app = isolated_app
    earliest = datetime(2026, 6, 1, 8, tzinfo=UTC)
    latest = datetime(2026, 8, 2, 8, tzinfo=UTC)
    last_sync = datetime(2026, 7, 2, 10, tzinfo=UTC)
    checked_through = datetime(2026, 7, 2, 9, tzinfo=UTC)
    with open_page(browser, app.url, locale, tmp_path) as page:
        _, headers = prepare_workspace(page.request, locale)
        with session_scope() as db:
            sender = db.scalars(select(MailboxConnection)).one()
            sender.imap_host = "imap.oasisfacilities.test"
            sender.sent_sync_status = "partial"
            sender.sent_last_synced_at = last_sync
            sender.sent_earliest_covered_at = earliest
            sender.sent_sync_checkpoint_json = {
                "checked_through_at": checked_through.isoformat()
            }
            for sequence, date in enumerate((earliest, latest)):
                db.add(
                    GmailSentRecord(
                        user_id=sender.user_id,
                        mailbox_connection_id=sender.id,
                        provider_message_id=f"past-{sequence}",
                        recipient_email="former@example.org",
                        recipient_normalized="former@example.org",
                        sent_at=date,
                    )
                )
            sender.sent_records_count = 2
            sender_id, sender_address = sender.id, sender.sender_email

        app.search_release.set()
        page.goto(app.url + "/batches")
        page.get_by_test_id("find-and-prepare").click()
        approve = page.locator('[data-testid^="approve-draft-btn-"]')
        expect(approve).to_be_enabled(timeout=30000)
        approve.click()
        page.get_by_test_id("batch-send-all-approved-btn").click()
        dialog = page.get_by_role("dialog")
        expect(dialog.get_by_test_id("send-confirm-sender")).to_have_text(
            sender_address
        )
        expect(dialog.get_by_test_id("send-confirm-coverage")).to_have_attribute(
            "data-mode", "partial"
        )
        expect(dialog.get_by_test_id("send-confirm-coverage")).to_contain_text(
            "partial" if locale == "en" else "جزئي"
        )
        for date in (earliest, latest, last_sync, checked_through):
            expect(
                dialog.locator(f'time[datetime="{date.isoformat()}"]')
            ).to_be_visible()
        confirm = dialog.get_by_test_id("confirm-send-approved-submit-btn")
        ack = dialog.get_by_test_id("send-confirm-history-ack")
        expect(ack).not_to_be_checked()
        expect(confirm).to_be_disabled()
        ack.check()
        expect(confirm).to_be_enabled()

        endpoint = "/api/v1/drafts/send-approved"
        review = page.request.get("/api/v1/review-queue?include_all=true").json()[
            "items"
        ]
        (draft,) = review
        payload = {
            "application_ids": [draft["id"]],
            "expected_hashes": {draft["id"]: draft["approved_content_hash"]},
            "expected_mailbox_id": sender_id,
            "acknowledge_incomplete_history": False,
            "dispatch_immediately": False,
        }
        assert page.request.post(endpoint, headers=headers, data=payload).status == 409
        with session_scope() as db:
            assert db.scalar(select(func.count()).select_from(OutboxEntry)) == 0
        with page.expect_response(
            lambda response: response.url.endswith(endpoint)
        ) as submitted:
            confirm.click()
        assert submitted.value.request.post_data_json == {
            **payload,
            "acknowledge_incomplete_history": True,
        }
        assert submitted.value.ok
        assert submitted.value.json()["queued_ids"] == [draft["id"]]
        with session_scope() as db:
            assert db.scalar(select(func.count()).select_from(OutboxEntry)) == 1
        assert not app.messages


@pytest.mark.parametrize("failure", ["getItem", "setItem", None], ids=["read-denied", "write-denied", "available"])
def test_language_storage_and_batch_navigation(browser, isolated_app, failure):
    with browser.new_context(base_url=isolated_app.url) as context:
        if failure:
            context.add_init_script("""
                const method = %s;
                const original = Storage.prototype[method];
                Storage.prototype[method] = function(key, ...args) {
                    if (key === 'applyflow_locale') throw new DOMException('Storage unavailable', 'SecurityError');
                    return original.call(this, key, ...args);
                };
            """ % repr(failure))
        page = context.new_page()
        prepare_workspace(page.request, "en")
        page.goto(isolated_app.url + "/batches")
        action = page.get_by_test_id("find-and-prepare")
        expect(action).to_be_visible()
        for toggle, lang, direction in [("العربية", "ar", "rtl"), ("English", "en", "ltr")]:
            with page.expect_response(lambda r: r.request.method == "PATCH" and r.url.endswith("/account/settings")):
                page.get_by_role("button", name=toggle, exact=True).click()
            expect(page.locator("html")).to_have_attribute("lang", lang)
            expect(page.locator("html")).to_have_attribute("dir", direction)
            expect(action).to_be_visible()
            if failure is None:
                assert page.evaluate("localStorage.getItem('applyflow_locale')") == lang
                page.reload()
                expect(action).to_be_visible()
                expect(page.locator("html")).to_have_attribute("lang", lang)
                expect(page.locator("html")).to_have_attribute("dir", direction)
