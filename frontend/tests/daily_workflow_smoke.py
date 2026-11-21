"""Exercise the built SPA with synthetic data. Every request is intercepted in Chromium."""

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright

dist = Path(__file__).resolve().parents[1] / "dist"


def check_approval_snapshots(page, queue_rows, item, state, locale):
    # Two eligible snapshots with distinct hashes and one legacy row without a hash.
    common = {
        **item,
        "approval_state": "ready_for_approval",
        "approved_content_hash": None,
        "transport_status": "draft",
        "outbox_status": None,
        "sent_at": None,
        "provider_message_id": None,
        "primary_skip_code": None,
    }
    first = {**common, "id": "first", "draft_content_hash": "first-v1"}
    second = {**common, "id": "second", "draft_content_hash": "second-v1"}
    legacy = {**common, "id": "legacy", "draft_content_hash": None}
    queue_rows[:] = [first, second, legacy]
    page.goto("http://app.test/batches?selected=run-1")
    expect(page.get_by_test_id("approve-draft-btn-legacy")).to_be_disabled()
    # A second tab changes only the second draft after this tab renders it.
    second.update(draft_content_hash="second-v2", subject="Edited in another tab")
    page.get_by_test_id("batch-approve-all-ready-btn").click()
    expect(
        page.get_by_text("Approval requires the current draft hash.", exact=False)
    ).to_be_visible()
    assert state["mutations"][-1][1]["expected_hashes"] == {
        "first": "first-v1",
        "second": "second-v1",
    }
    assert first["approved_content_hash"] == "first-v1"
    assert second["approved_content_hash"] is None
    expect(page.get_by_test_id("prepared-app-card-second")).to_contain_text(
        "Edited in another tab"
    )

    first.update(draft_content_hash="first-v2", approval_state="ready_for_approval")
    page.goto("http://app.test/tracker?tab=review")
    page.get_by_test_id("review-bulk-approve-ready-btn").click()
    expect(page.get_by_test_id("review-bulk-send-approved-btn")).to_contain_text("(2)")
    assert state["mutations"][-1][1]["expected_hashes"] == {
        "first": "first-v2",
        "second": "second-v2",
    }
    assert legacy["approved_content_hash"] is None

    # A failed transport label with an ambiguous handoff must also stay locked.
    first.update(
        transport_status="failed",
        outbox_status="failed",
        outbox_handoff_outcome="ambiguous_handoff",
    )
    page.reload()
    reconcile = page.get_by_test_id("reconcile-outbox-btn-first")
    reconcile.click()
    card = page.get_by_test_id("prepared-app-card-first")
    expect(card.get_by_role("alert")).to_contain_text(
        "uncertain" if locale == "en" else "غير مؤكدة"
    )
    expect(page.get_by_test_id("retry-send-btn-first")).to_have_count(0)
    expect(page.get_by_test_id("send-approved-btn-first")).to_have_count(0)
    expect(page.get_by_test_id("approve-draft-btn-first")).to_be_disabled()
    expect(reconcile).to_be_enabled()
    state["positive_reconciliation"] = True
    reconcile.click()
    expect(card).to_contain_text("reconciled-receipt")
    expect(reconcile).to_have_count(0)
    expect(page.get_by_test_id("retry-send-btn-first")).to_have_count(0)


def check_batch_record_groups(page, locale, batch, queue_rows, state):
    batch.update(drafted_count=0, eligible_count=0, duplicates_removed_count=7,
                 budget_usage={"pages_fetched": 22, "pages_inaccessible": 3},
                 sources_searched=[{"successful_fetches": 4}, {"successful_fetches": 6}])
    state["workflow_counts"] = {"imported": 3, "blocked": 2, "duplicate": 1}
    state["sightings"] = [
        {"is_duplicate": True, "dedupe_reason": "prior_batch_content_match"} for _ in range(5)
    ] + [{"is_duplicate": True, "dedupe_reason": "duplicate_in_batch"} for _ in range(2)]
    template = {**queue_rows[0], "outbox_status": None, "outbox_handoff_outcome": None,
                "provider_message_id": None, "sent_at": None, "primary_skip_code": None}
    queue_rows[:] = [
        {**template, "id": f"excluded-{i}", "approval_state": "rejected",
         "transport_status": "skipped", "primary_skip_code": code, "subject": "", "body_text": ""}
        for i, code in enumerate(["PREVIOUSLY_CONTACTED", "LOCATION_MISMATCH", "MISSING_REQUIREMENT"])
    ]
    page.goto("http://app.test/batches?selected=run-1")
    progress = page.get_by_test_id("batch-progress")
    for label, count in [
        ("Page fetch attempts across all stages" if locale == "en" else "محاولات جلب الصفحات في جميع المراحل", "22"),
        ("Confirmed primary page fetches" if locale == "en" else "عمليات الجلب الناجحة المؤكدة في البحث الأولي", "10"),
        ("Repeat source sightings removed" if locale == "en" else "نتائج المصادر المكررة المستبعدة", "7"),
    ]:
        expect(progress.get_by_text(label, exact=True).locator("..").locator("dd")).to_have_text(count)
    expect(page.get_by_test_id("wf-count-imported")).to_have_count(0)
    prepared = page.get_by_test_id("batch-app-filter-all")
    excluded = page.get_by_test_id("batch-app-filter-duplicate_blocked")
    expect(prepared).to_contain_text("(0)")
    expect(excluded).to_contain_text("(3)")
    cards = page.locator('[data-testid^="prepared-app-card-"]')
    expect(cards).to_have_count(0)
    excluded.click()
    expect(cards).to_have_count(3)
    expect(cards.first).to_contain_text("PREVIOUSLY_CONTACTED")
    breakdown = page.get_by_text("Why source sightings were removed" if locale == "en" else "أسباب استبعاد نتائج المصادر المكررة")
    breakdown.click()
    expect(breakdown.locator("..").locator("dd")).to_have_text(["5", "2"])

    # Mixed import: keep the import provenance, and keep excluded rows out of bulk approval.
    batch.update(import_origin="gemini_zip", drafted_count=1)
    queue_rows.append({**template, "id": "prepared", "approval_state": "ready_for_approval",
                       "transport_status": "draft", "approved_content_hash": None})
    page.reload()
    expect(prepared).to_contain_text("(1)")
    expect(cards).to_have_count(1)
    expect(page.get_by_test_id("wf-count-imported")).to_be_visible()
    expect(page.get_by_test_id("batch-progress")).to_have_count(0)
    expect(page.get_by_test_id("batch-approve-all-ready-btn")).to_contain_text("(1)")


def run(browser, locale, width):
    ctx = browser.new_context(
        viewport={"width": width, "height": 800}, service_workers="block"
    )
    state = {"created": False, "reads": 0, "sent": False, "polls": 0, "mutations": []}
    coverage = {
        "coverage_mode": "partial",
        "sent_sync_status": "partial",
        "sent_last_synced_at": "2026-10-15T10:00:00Z",
        "checked_through_at": "2026-10-15T09:00:00Z",
        "sent_earliest_covered_at": "2026-08-03T09:00:00Z",
        "sent_latest_covered_at": "2026-11-01T09:00:00Z",
    }
    mailbox = {
        "id": "mailbox-a",
        "provider": "smtp_imap",
        "status": "connected",
        "sender_email": "sender@example.test",
        "dedup_coverage": coverage,
    }
    preferences = {
        "freshness_days": 14,
        "excluded_recipients": ["old@example.test"],
        "batch_size": 5,
    }
    item = {
        "id": "draft-a",
        "batch_id": "run-1",
        "role_title": "Analyst",
        "company_name": "Fixture Ltd",
        "location_raw": "Test City",
        "recipient_email": "team@example.test",
        "cc_emails": [],
        "subject": "Synthetic subject",
        "body_text": "Synthetic review body.",
        "selected_cv_version_id": "version-1",
        "selected_cv_document_id": "cv-1",
        "attachment_pdf_sha256": "a" * 64,
        "frozen_cv_filename": "fixture.pdf",
        "approved_content_hash": "h1",
        "draft_content_hash": "h1",
        "approval_state": "approved",
        "transport_status": "draft",
        "evidence_quote": "Apply by email",
        "source_url": "https://example.test/open",
        "verification_explanation": "Fixture evidence",
    }
    batch = {
        "id": "run-1",
        "mode": "live",
        "status": "running",
        "stage": "discovery_search",
        "created_at": "2026-11-01T00:00:00Z",
        "progress_counts": {
            "queries_executed": 1,
            "candidates_processed": 0,
            "candidates_found": 2,
            "openings_discovered": 1,
        },
        "eligible_count": 1,
        "drafted_count": 1,
        "category_counts": {},
        "skip_reason_counts": {},
        "metrics": {},
    }
    queue_rows = [item]
    status_polls = []

    def serve(route):
        path = urlparse(route.request.url).path
        if path.startswith("/api/v1/"):
            key = path[8:]
            verb = route.request.method
            data = route.request.post_data_json if route.request.post_data else {}
            if verb != "GET":
                state["mutations"].append((key, data))
            response = {}
            if key == "auth/me":
                response = {
                    "user": {
                        "id": "u",
                        "email": "owner@example.test",
                        "full_name": "Fixture",
                        "timezone": "UTC",
                        "email_verified": True,
                        "ui_locale": locale,
                        "csrf_token": "mock",
                        "onboarding": {},
                        "live_sending_enabled_on_server": False,
                    }
                }
            elif key == "batches" and verb == "POST":
                assert (
                    data["run_mode"] == "approval_required"
                    and data["mode"] == "live"
                    and "pasted_posting" not in data
                )
                state["created"] = True
                response = {"batch_id": "run-1"}
            elif key == "batches":
                response = {"batches": [batch] if state["created"] else []}
            elif key == "batches/run-1":
                state["reads"] += 1
                if state["reads"] > 1:
                    batch.update(status="completed", stage="completed")
                response = {
                    "batch": batch,
                    "all_applications": [
                        row for row in queue_rows if row["batch_id"] == batch["id"]
                    ],
                    "live_sending_enabled": True,
                    "workflow_counts": state.get("workflow_counts", {"approved": 1}),
                    "sightings": state.get("sightings", []),
                    "postings": [],
                }
            elif key == "review-queue":
                query = parse_qs(urlparse(route.request.url).query)
                rows = queue_rows
                if "application_ids" in query:
                    ids = query["application_ids"][0].split(",")
                    assert query["include_all"] == ["true"]
                    status_polls.append(ids)
                    if state["sent"] and item["id"] in ids:
                        item.update(
                            transport_status="sent",
                            outbox_status="sent",
                            sent_at="2026-11-01T01:00:00Z",
                            provider_message_id="receipt-fixture",
                        )
                    rows = [row for row in rows if row["id"] in ids]
                if "batch_id" in query:
                    rows = [
                        row for row in rows if row["batch_id"] == query["batch_id"][0]
                    ]
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["100"])[0])
                response = {
                    "total": len(rows),
                    "items": rows[offset : offset + limit],
                    "live_sending_enabled": True,
                }
            elif key == "cvs":
                response = {
                    "cvs": [
                        {
                            "id": "cv-1",
                            "variant_label": "Fixture",
                            "versions": [
                                {
                                    "id": "version-1",
                                    "original_filename": "fixture.pdf",
                                    "mime_type": "application/pdf",
                                    "sha256": "a" * 64,
                                }
                            ],
                        }
                    ]
                }
            elif key == "drafts/draft-a/revise":
                assert data["recipient_email"] == "updated@example.test" and data[
                    "cc_emails"
                ] == ["cc@example.test"]
                item.update(
                    **data,
                    draft_content_hash="h2",
                    approved_content_hash=None,
                    approval_state="ready_for_approval",
                )
                response = {"draft": item, "approval_invalidated": True}
            elif key == "drafts/approve":
                ids = data["application_ids"]
                hashes = data["expected_hashes"]
                assert set(hashes) == set(ids) and all(hashes.values())
                response = {"approved_ids": [], "failed": []}
                for app_id in ids:
                    row = next(row for row in queue_rows if row["id"] == app_id)
                    if hashes[app_id] != row["draft_content_hash"]:
                        response["failed"].append(
                            {
                                "id": app_id,
                                "error": "Approval requires the current draft hash. Refresh and review this draft.",
                            }
                        )
                    else:
                        row.update(
                            approved_content_hash=hashes[app_id],
                            approval_state="approved",
                        )
                        response["approved_ids"].append(app_id)
            elif key == "applications/first/reconcile-outbox":
                assert route.request.post_data is None
                if state.get("positive_reconciliation"):
                    queue_rows[0].update(
                        transport_status="sent",
                        outbox_status="sent",
                        outbox_handoff_outcome="provider_accepted",
                        provider_message_id="reconciled-receipt",
                    )
                    response = {
                        "ok": True,
                        "reconciled_outcome": "sent",
                        "provider_message_id": "reconciled-receipt",
                    }
                else:
                    response = {"ok": True, "reconciled_outcome": "still_uncertain"}
            elif key == "drafts/send-approved":
                assert data == {
                    "application_ids": ["draft-a"],
                    "expected_hashes": {"draft-a": "h2"},
                    "expected_mailbox_id": mailbox["id"],
                    "acknowledge_incomplete_history": coverage["coverage_mode"]
                    != "applyflow_ledger_plus_gmail_sent",
                    "dispatch_immediately": False,
                }
                if state.pop("switch_sender", False):
                    mailbox.update(id="mailbox-b", sender_email="changed@example.test")
                    coverage.update(
                        coverage_mode="sync_error", sent_sync_status="error"
                    )
                    route.fulfill(
                        status=409,
                        json={
                            "detail": {
                                "code": "SENDER_CHANGED",
                                "message": "Sending mailbox changed. Review again."
                            }
                        },
                    )
                    return
                state["sent"] = True
                item.update(transport_status="queued", outbox_status="queued")
                response = {"queued_ids": ["draft-a"], "sent_ids": [], "blocked": []}
            elif key == "drafts/draft-a/reject":
                item.update(approval_state="rejected")
                response = {"approval_state": "rejected"}
            elif key == "applications/draft-a/duplicate-override":
                assert data["reason"] == "Different requisition"
                item.update(
                    approval_state="ready_for_approval",
                    transport_status="draft",
                    primary_skip_code=None,
                )
            elif key == "preferences":
                if verb == "PUT":
                    preferences.update(data)
                response = {"preferences": preferences}
            elif key == "schedules":
                response = {"schedules": []}
            elif key == "mailboxes":
                response = {
                    "mailboxes": [
                        {
                            "id": "old",
                            "status": "revoked",
                            "sender_email": "other@example.test",
                        },
                        mailbox,
                    ],
                    "sending_mailbox_id": mailbox["id"],
                }
            route.fulfill(json=response)
            return
        if urlparse(route.request.url).hostname != "app.test":
            route.abort()
            return
        f = dist / path.lstrip("/")
        if not f.is_file():
            f = dist / "index.html"
        route.fulfill(
            path=f,
            content_type=mimetypes.guess_type(str(f))[0] or "application/octet-stream",
        )

    ctx.route("**/*", serve)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    print("Loading dashboard", locale, width, flush=True)
    page.set_default_timeout(10000)
    page.goto("http://app.test/dashboard")
    page.get_by_test_id("dashboard-find-prepare").click()
    page.wait_for_url("**/batches")
    assert not state["mutations"]
    expect(page.locator('input[value=demo_fixture]')).to_be_checked()
    page.locator('input[value=approval_required]').check()
    page.get_by_test_id("find-and-prepare").click()
    progress = page.get_by_test_id("batch-progress")
    expect(progress).to_contain_text(
        "Completed" if locale == "en" else "مكتمل", timeout=10000
    )
    assert len(state["mutations"]) == 1
    page.get_by_test_id("edit-draft-btn-draft-a").click()
    page.locator("#edit-recipient-draft-a").fill("updated@example.test")
    page.locator("#edit-cc-draft-a").fill("cc@example.test")
    page.get_by_test_id("save-revision-btn-draft-a").click()
    expect(page.get_by_test_id("send-approved-btn-draft-a")).to_be_disabled()
    page.get_by_test_id("approve-draft-btn-draft-a").click()
    expect(page.get_by_test_id("preview-attachment-draft-a")).to_have_attribute(
        "href", "/api/v1/applications/draft-a/attachment-preview"
    )
    page.get_by_test_id("batch-send-all-approved-btn").click()
    modal = page.get_by_role("dialog")
    expect(modal).to_contain_text("cc@example.test")
    expect(modal).to_contain_text("fixture.pdf")
    expect(modal).to_contain_text("Synthetic subject")
    expect(modal.get_by_test_id("send-confirm-sender")).to_have_text(
        "sender@example.test"
    )
    expect(modal.get_by_test_id("send-confirm-coverage")).to_have_attribute(
        "data-mode", "partial"
    )
    for key in (
        "sent_last_synced_at",
        "checked_through_at",
        "sent_earliest_covered_at",
        "sent_latest_covered_at",
    ):
        expect(modal.locator(f'time[datetime="{coverage[key]}"]')).to_be_visible()
    acknowledge = page.get_by_test_id("send-confirm-history-ack")
    btn = page.get_by_test_id("confirm-send-approved-submit-btn")
    expect(btn).to_be_disabled()
    acknowledge.check()
    expect(btn).to_be_enabled()
    page.keyboard.press("Escape")
    expect(modal).to_have_count(0)
    assert not state["sent"]
    page.get_by_test_id("batch-send-all-approved-btn").click()
    expect(acknowledge).not_to_be_checked()
    expect(btn).to_be_disabled()
    acknowledge.check()
    btn.evaluate("(el)=>{el.click();el.click()}")
    status = page.get_by_test_id("send-modal-item-status-draft-a")
    expect(status).to_have_text(
        "Queued" if locale == "en" else "في قائمة الانتظار", timeout=3000
    )
    expect(modal).to_contain_text("receipt-fixture", timeout=12000)
    assert len([r for r in state["mutations"] if r[0] == "drafts/send-approved"]) == 1
    expect(page.locator("html")).to_have_attribute(
        "dir", "rtl" if locale == "ar" else "ltr"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_test_id("done-send-confirm-modal-btn").click()
    item.update(
        transport_status="skipped",
        approval_state="blocked",
        outbox_status=None,
        primary_skip_code="PREVIOUSLY_CONTACTED",
        sent_at=None,
        provider_message_id=None,
        approved_content_hash=None,
    )
    state["sent"] = False
    page.goto("http://app.test/tracker?tab=review&batch_id=run-1")
    page.get_by_role(
        "button",
        name="Review shared inbox exception"
        if locale == "en"
        else "مراجعة استثناء البريد المشترك",
    ).click()
    page.get_by_role(
        "textbox", name="Explain how" if locale == "en" else "وضّح كيف", exact=False
    ).fill("Different requisition")
    page.get_by_role(
        "button",
        name="Confirm different vacancy"
        if locale == "en"
        else "تأكيد أن الوظيفة مختلفة",
    ).click()
    page.get_by_test_id("reject-draft-btn-draft-a").click()
    expect(page.get_by_test_id("approve-draft-btn-draft-a")).to_be_disabled()
    page.goto("http://app.test/preferences")
    recipients = page.get_by_label(
        "Excluded recipients" if locale == "en" else "عناوين البريد المستبعدة"
    )
    expect(recipients).to_have_value("old@example.test")
    recipients.fill("new@example.test, second@example.test")
    page.get_by_label(
        "Maximum posting age (days)" if locale == "en" else "أقصى عمر للإعلان (بالأيام)"
    ).fill("7")
    page.locator('button[type="submit"]').click()
    expect(page.locator('[role="status"]')).to_be_visible()
    assert preferences["excluded_recipients"] == [
        "new@example.test",
        "second@example.test",
    ]
    assert preferences["freshness_days"] == 7
    # Put one approved row in each of two pages, across different batches.
    item.update(
        approval_state="approved",
        approved_content_hash="h2",
        transport_status="draft",
        outbox_status=None,
    )
    queue_rows[:0] = [
        {
            **item,
            "id": f"newer-{n}",
            "batch_id": "run-2",
            "role_title": f"Newer {n}",
            "approval_state": "approved" if n == 0 else "rejected",
            "approved_content_hash": "h2" if n == 0 else None,
        }
        for n in range(100)
    ]
    page.goto("http://app.test/tracker?tab=review")
    send_shown = page.get_by_test_id("review-bulk-send-approved-btn")
    page_range = page.get_by_test_id("review-page-range")
    expect(page_range).to_have_text(
        "Applications 1–100 of 101"
        if locale == "en"
        else "الطلبات من 1 إلى 100 من أصل 101"
    )
    expect(send_shown).to_contain_text("(1)")
    coverage.update(
        coverage_mode="applyflow_ledger_plus_gmail_sent", sent_sync_status="ok"
    )
    send_shown.click()
    expect(modal.get_by_test_id("send-confirm-item-newer-0")).to_be_visible()
    expect(modal.get_by_test_id("send-confirm-coverage")).to_have_attribute(
        "data-mode", "complete"
    )
    expect(acknowledge).to_have_count(0)
    expect(btn).to_be_enabled()
    expect(modal.get_by_test_id("send-confirm-item-draft-a")).to_have_count(0)
    page.keyboard.press("Escape")
    page.get_by_test_id("review-filter-duplicate_blocked").click()
    expect(send_shown).to_be_disabled()
    page.get_by_test_id("review-filter-all").click()
    page.get_by_test_id("review-page-next").click()
    expect(page.get_by_test_id("review-page-next")).to_be_disabled()
    expect(page.get_by_test_id("prepared-app-card-draft-a")).to_be_visible()
    expect(page_range).to_have_text(
        "Applications 101–101 of 101"
        if locale == "en"
        else "الطلبات من 101 إلى 101 من أصل 101"
    )
    expect(page.get_by_test_id("prepared-app-card-newer-0")).to_have_count(0)
    send_shown.click()
    expect(modal.get_by_test_id("send-confirm-item-draft-a")).to_be_visible()
    expect(modal.get_by_test_id("send-confirm-item-newer-0")).to_have_count(0)
    previous_polls = len(status_polls)
    state["switch_sender"] = True
    btn.click()
    expect(modal.get_by_test_id("send-confirm-sender")).to_have_text(
        "changed@example.test"
    )
    expect(modal.get_by_test_id("send-confirm-coverage")).to_have_attribute(
        "data-mode", "unavailable"
    )
    expect(acknowledge).not_to_be_checked()
    expect(btn).to_be_disabled()
    acknowledge.check()
    btn.click()
    expect(modal).to_contain_text("receipt-fixture", timeout=12000)
    assert status_polls[previous_polls:] == [["draft-a"]]
    page.get_by_test_id("done-send-confirm-modal-btn").click()
    page.get_by_test_id("review-page-prev").click()
    expect(page.get_by_test_id("prepared-app-card-newer-0")).to_be_visible()
    expect(page.get_by_test_id("review-page-prev")).to_be_disabled()
    page.get_by_test_id("review-page-next").click()
    page.get_by_test_id("review-batch-select").select_option("run-1")
    expect(page_range).to_have_text(
        "Applications 1–1 of 1" if locale == "en" else "الطلبات من 1 إلى 1 من أصل 1"
    )
    expect(page.get_by_test_id("review-page-prev")).to_be_disabled()
    check_approval_snapshots(page, queue_rows, item, state, locale)
    check_batch_record_groups(page, locale, batch, queue_rows, state)
    assert not errors, errors
    print(
        f"{locale} {width}px: daily workflow, paging, approvals, reconciliation, send coverage and batch record grouping passed"
    )
    ctx.close()


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.environ.get("CHROME_EXECUTABLE"), headless=True
        )
        run(browser, "en", 1280)
        run(browser, "ar", 360)
        browser.close()
