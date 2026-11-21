"""Real Chrome workflow check. From backend run python -m pytest -q -s tests/e2e_workflow_check.py."""

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from playwright.sync_api import expect
from sqlalchemy import func, select

from app.db.models import OutboxEntry
from app.db.session import session_scope
from app.tasks.pipeline import task_drain_outbox

pytest_plugins = ["tests.workflow_browser_harness"]


def assert_preview(page, link, pdf):
    """Click the actual inline preview and compare its authenticated response with the uploaded bytes."""
    with page.context.expect_event("response", predicate=lambda r: r.url.endswith("/attachment-preview")) as received:
        with page.expect_popup() as opened:
            link.click()
    response = received.value
    try:
        assert response.status == 200
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["x-attachment-sha256"] == hashlib.sha256(pdf).hexdigest()
        # Chromium can stream PDF navigation bodies into its viewer. Fetch the same URL with the same session.
        assert page.request.get(response.url).body() == pdf
    finally:
        opened.value.close()


def outbox_count():
    with session_scope() as db:
        return db.scalar(select(func.count()).select_from(OutboxEntry))


@pytest.mark.parametrize("locale", ["en", "ar"], ids=["desktop-en", "mobile-ar"])
@pytest.mark.parametrize("stopped", [False, True], ids=["active", "stopped"])
def test_dashboard_chooser_opens_with_keyboard(locale, stopped, browser, isolated_app, tmp_path):
    from tests.workflow_browser_harness import open_page, prepare_workspace

    with open_page(browser, isolated_app.url, locale, tmp_path) as page:
        _, headers = prepare_workspace(page.request, locale)
        if stopped:
            assert page.request.post("/api/v1/account/stop-automation", headers=headers).ok
            rejected = page.request.post(
                "/api/v1/batches",
                headers=headers,
                data={"mode": "live", "run_mode": "approval_required", "run_immediately": True},
            )
            assert rejected.status == 409
            assert rejected.json()["detail"]["code"] == "AUTOMATION_STOPPED"

        submissions = []

        def record_submission(request):
            if request.method == "POST" and request.url.endswith("/api/v1/batches"):
                submissions.append(request.url)

        page.on("request", record_submission)
        page.goto("/dashboard")
        shortcut = page.get_by_test_id("dashboard-find-prepare")
        expect(shortcut).to_be_enabled()
        shortcut.focus()
        expect(shortcut).to_be_focused()
        page.keyboard.press("Enter")
        expect(page).to_have_url(isolated_app.url + "/batches")

        form = page.locator("form").filter(has=page.get_by_test_id("find-and-prepare"))
        demo = form.locator('input[value="demo_fixture"]')
        expect(demo).to_be_checked()
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if locale == "ar" else "ltr")
        assert form.evaluate("(form) => form.getBoundingClientRect().width <= window.innerWidth")

        notice = page.locator("#discovery-paused")
        buttons = form.locator('button[type="submit"]')
        expect(buttons).to_have_count(2)
        if stopped:
            expect(notice).to_contain_text("الأتمتة المباشرة متوقفة" if locale == "ar" else "Live automation is stopped")
            for button in buttons.all():
                expect(button).to_be_disabled()
        else:
            expect(notice).to_have_count(0)
            for button in buttons.all():
                expect(button).to_be_enabled()

        demo.focus()
        page.keyboard.press("ArrowLeft")
        expect(demo).not_to_be_checked()
        assert not submissions
        assert not page.request.get("/api/v1/batches", headers=headers).json()["batches"]


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_both_batch_controls_persist_selected_mode(locale, browser, isolated_app, tmp_path, monkeypatch):
    from app.api import routes_automation
    from tests.workflow_browser_harness import open_page, prepare_workspace

    # The HTTP API and persistence are real. Stop at the broker boundary for the payload matrix.
    monkeypatch.setattr(routes_automation, "_publish_discovery_batch", lambda _batch_id: None)
    with open_page(browser, isolated_app.url, locale, tmp_path) as page:
        prepare_workspace(page.request, locale)
        page.goto(isolated_app.url + "/batches")
        demo = page.locator('input[name=batch_mode][value=demo_fixture]')
        expect(demo).to_be_visible()
        expect(demo).to_be_checked()
        page.get_by_text("More discovery options" if locale == "en" else "خيارات بحث إضافية", exact=True).click()
        page.locator("#batch-goal-count").fill("2")
        page.locator("#batch-max-queries").fill("3")
        page.get_by_role("combobox", name=re.compile("standing|دعوات", re.I)).select_option("yes")
        for mode in ["demo_fixture", "dry_run", "approval_required"]:
            page.locator(f'input[name=batch_mode][value="{mode}"]').check()
            canonical = "live" if mode == "approval_required" else mode
            form = page.locator("form").filter(has=page.get_by_test_id("find-and-prepare"))
            paste = form.get_by_role("checkbox")
            paste.set_checked(mode == "dry_run")
            extra = {}
            if mode == "dry_run":
                text = "Fictional vacancy for the local browser test."
                form.locator("textarea").fill(text)
                extra["pasted_posting"] = {"title": "Public Job Posting", "company_name": "",
                                           "source_url": "", "location_raw": "", "raw_text": text}
            for button in [page.get_by_test_id("find-and-prepare"),
                           page.get_by_role("button", name="Start Batch" if locale == "en" else "بدء الدفعة", exact=True)]:
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/batches")) as event:
                    button.click()
                response = event.value
                assert response.request.post_data_json == {
                    "mode": canonical, "run_mode": mode, "run_immediately": True,
                    "target_goal_count": 2, "max_search_queries": 3, "include_standing_invitations": True, **extra,
                }
                assert response.status == 201
                result = response.json()
                assert (result["mode"], result["run_mode"]) == (canonical, mode)
                stored = page.request.get("/api/v1/batches/" + result["batch_id"]).json()["batch"]
                assert (stored["mode"], stored["run_mode"]) == (canonical, mode)


@pytest.mark.parametrize("iteration", range(int(os.getenv("WORKFLOW_REPEATS", "1"))))
@pytest.mark.parametrize("locale", ["en", "ar"], ids=["desktop-ltr", "mobile-rtl"])
def test_review_and_send_once(locale, iteration, browser, isolated_app, tmp_path):
    from tests.workflow_browser_harness import CC, COMPANY, EXTRA_CC, RECEIPT, TO, open_page, prepare_workspace

    ar = locale == "ar"
    app = isolated_app
    with open_page(browser, app.url, locale, tmp_path) as page:
        pdfs, headers = prepare_workspace(page.request, locale, upload_page=page)
        page.goto(app.url + "/batches")
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if ar else "ltr")
        expect(page.locator("html")).to_have_attribute("lang", locale)
        find = page.get_by_role("button", name="البحث عن الوظائف وتجهيز الطلبات" if ar else "Find and Prepare Applications")
        page.locator('input[name=batch_mode][value=approval_required]').check()
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/batches")) as started:
            find.click()
        batch = started.value.json()
        assert (batch["mode"], batch["run_mode"]) == ("live", "approval_required")
        progress = page.get_by_test_id("batch-progress").get_by_role("status")
        expect(progress).to_contain_text("البحث عن وظائف" if ar else "Searching for openings", timeout=15000)
        app.search_release.set()
        expect(progress).to_contain_text("مكتمل" if ar else "Completed", timeout=20000)
        expect(progress).to_contain_text("1/1")
        card = page.get_by_role("article").filter(has_text=COMPANY)
        expect(card).to_have_count(1)
        send = card.get_by_role("button", name="إرسال المعتمد" if ar else "Send Approved", exact=True)
        expect(send).to_be_disabled()
        preview_name = "معاينة المرفق" if ar else "Preview attachment"
        preview = card.get_by_role("link", name=preview_name)
        card.get_by_role("button", name="تعديل" if ar else "Edit", exact=True).click()
        select_cv = card.get_by_role("combobox")
        original = select_cv.input_value()
        revised, = set(pdfs) - {original}
        assert_preview(page, preview, pdfs[original][1])
        select_cv.select_option(revised)
        cc = card.get_by_label(re.compile("CC"))
        assert set(map(str.strip, cc.input_value().split(","))) == {CC, EXTRA_CC}
        cc.fill(CC)
        subject = "Reviewed application for CAFM Administrator"
        card.get_by_label("الموضوع" if ar else "Subject", exact=True).fill(subject)
        card.get_by_role("button", name="حفظ التعديلات" if ar else "Save Revision").click()
        expect(card.get_by_text(subject, exact=False)).to_be_visible()
        expect(card).to_contain_text(pdfs[revised][0])
        assert_preview(page, preview, pdfs[revised][1])
        assert outbox_count() == 0 and not app.messages
        card.get_by_role("button", name="اعتماد" if ar else "Approve", exact=True).click()
        expect(send).to_be_enabled()
        send.click()
        dialog = page.get_by_role("dialog")
        for value in (TO, CC, pdfs[revised][0], subject):
            expect(dialog).to_contain_text(value)
        expect(dialog).not_to_contain_text(EXTRA_CC)
        body_label = "نص الرسالة" if ar else "Email Body"
        dialog.get_by_text(body_label, exact=True).click()
        body = dialog.locator("details p").inner_text()
        assert len(body.strip()) >= 20
        assert outbox_count() == 0 and not app.messages
        expect(dialog.get_by_test_id("confirm-send-approved-submit-btn")).to_be_disabled()
        dialog.get_by_test_id("send-confirm-history-ack").check()
        page.screenshot(path=tmp_path / "confirmation.png")

        endpoint = "/api/v1/drafts/send-approved"
        with page.expect_response(lambda r: r.url.endswith(endpoint)) as submitted:
            dialog.get_by_role("button", name="تأكيد الإرسال وإضافته إلى قائمة الانتظار" if ar else "Confirm and queue").click()
        request = submitted.value.request.post_data_json
        app_id, = request["application_ids"]
        assert submitted.value.json()["queued_ids"] == [app_id]
        phase = dialog.get_by_test_id(f"send-modal-item-status-{app_id}")
        expect(phase).to_have_text("في قائمة الانتظار" if ar else "Queued")
        assert outbox_count() == 1 and not app.messages
        queued_replay = page.request.post(endpoint, headers=headers, data=request)
        assert queued_replay.ok, queued_replay.text()
        assert queued_replay.json()["queued_ids"] == [app_id]
        assert outbox_count() == 1 and not app.messages
        with ThreadPoolExecutor(max_workers=1) as worker:
            sent = worker.submit(task_drain_outbox.run)
            try:
                expect(phase).to_have_text("قيد الإرسال" if ar else "Sending", timeout=12000)
            finally:
                app.mail_release.set()
            assert sent.result(timeout=15) == 1
        expect(phase).to_have_text("مقبول من المزود" if ar else "Accepted by Provider", timeout=12000)
        expect(dialog).to_contain_text(RECEIPT)
        dialog.get_by_role("button", name="إغلاق" if ar else "Close", exact=True).click()
        replay = page.request.post(endpoint, headers=headers, data=request)
        assert replay.ok, replay.text()
        outcome = replay.json()
        assert not outcome["queued_ids"] and not outcome["sent_ids"]
        assert [item["id"] for item in outcome["blocked"]] == [app_id]
        assert task_drain_outbox.run() == 0
        assert outbox_count() == 1
        message, = app.messages
        assert (message.to_email, message.cc_emails, message.subject, message.body_text) == (TO, [CC], subject, body)
        attachment, = message.attachments
        assert (attachment.filename, attachment.data) == pdfs[revised]

        page.get_by_role("link", name="سجل الطلبات" if ar else "Applications", exact=True).click()
        page.reload()
        row = page.get_by_role("row").filter(has_text=COMPANY)
        expect(row).to_have_count(1)
        for value in ("تم الإرسال" if ar else "Sent", RECEIPT, TO):
            expect(row).to_contain_text(value)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=tmp_path / "tracker.png")
        print(f"{locale} run {iteration + 1}: one outbox row, one handoff, replay blocked. Evidence {tmp_path}")


@pytest.mark.parametrize("locale", ["en", "ar"])
def test_claim_fields_have_localized_names_and_tab_order(browser, isolated_app, locale):
    names = {
        "en": ["One-time setup code (never in URL)", "Owner email address", "Full name",
               "Owner password (min 10 characters)", "Timezone (IANA)"],
        "ar": ["رمز الإعداد لمرة واحدة (لا يوضع في الرابط)", "البريد الإلكتروني للمالك", "الاسم الكامل",
               "كلمة مرور المالك (10 أحرف على الأقل)", "المنطقة الزمنية (IANA)"],
    }
    page = browser.new_page(viewport={"width": 390 if locale == "ar" else 1280, "height": 844})
    try:
        page.goto(isolated_app.url + "/setup")
        switch = page.get_by_role("button", name="العربية", exact=True)
        if locale == "ar":
            switch.click()
            switch = page.get_by_role("button", name="English", exact=True)
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if locale == "ar" else "ltr")
        switch.focus()
        form = page.locator("form")
        for name in names[locale]:
            page.keyboard.press("Tab")
            field = form.get_by_label(name, exact=True)
            expect(field).to_be_focused()
            expect(field).to_have_accessible_name(name)
        page.keyboard.press("Tab")
        expect(form.locator("button[type=submit]")).to_be_focused()
    finally:
        page.close()
