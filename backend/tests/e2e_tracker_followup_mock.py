"""Run against a frontend production build. All browser requests are intercepted locally."""
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def check_tracker(browser, locale, viewport):
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    ctx = browser.new_context(viewport=viewport, service_workers="block")
    page = ctx.new_page()
    errors, approvals = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    app = {"id": "application-1", "company_name": "Hiring Co", "role_title": "Analyst", "transport_status": "sent",
           "outcome_status": "open", "response_status": "none", "approval_state": "approved", "verification_status": "verified",
           "recipient_email": "hr@hiring.test", "next_follow_up_at": "2020-01-08T00:00:00Z"}
    draft = {"id": "draft-1", "status": "awaiting_approval", "sender": "owner@sender.test", "recipient": app["recipient_email"],
             "subject": "Re: Analyst", "body": "Reviewed follow-up body", "confirm_inbox_required": True}

    def intercept(route):
        path = urlsplit(route.request.url).path
        if path.startswith("/api/v1"):
            endpoint = path.removeprefix("/api/v1")
            if endpoint.endswith("/followup/approve"):
                approvals.append(route.request.post_data_json)
                draft["status"] = "queued"
                reply = draft
            else:
                reply = {"/auth/me": {"user": {"id": "u1", "email": "owner@sender.test", "ui_locale": locale,
                                              "setup_state": "completed", "csrf_token": "mock"}},
                         "/applications": {"applications": [app], "total": 1},
                         "/applications/application-1": {"application": app, "followup": draft, "source_evidence": {},
                                                         "match_explanation": {}, "events": []}}.get(endpoint, {})
            route.fulfill(json=reply)
        elif path == "/tracker":
            route.fulfill(path=dist / "index.html", content_type="text/html")
        elif path.startswith("/assets/") and (dist / path.lstrip("/")).is_file():
            route.fulfill(path=dist / path.lstrip("/"), content_type="text/css" if path.endswith(".css") else "application/javascript")
        else:
            route.fulfill(status=404, body="")

    ctx.route("**/*", intercept)
    page.goto("https://tracker.invalid/tracker")
    page.get_by_text("Hiring Co", exact=True).click()
    expect(page.get_by_text(draft["body"])).to_be_visible()
    approve = page.get_by_role("button", name="Approve and queue follow-up" if locale == "en" else "الموافقة وإضافة المتابعة إلى قائمة الإرسال")
    expect(approve).to_be_disabled()
    page.get_by_role("checkbox", name="I checked" if locale == "en" else "راجعت").check()
    approve.click()
    expect(approve).to_have_count(0)
    assert approvals == [{"draft_id": "draft-1", "confirm_inbox": True}]
    assert page.locator("html").get_attribute("dir") == ("rtl" if locale == "ar" else "ltr")
    assert not errors, errors
    ctx.close()


if __name__ == "__main__":
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        for lang, size in [("en", {"width": 1280, "height": 800}), ("ar", {"width": 390, "height": 844})]:
            check_tracker(browser, lang, size)
            print(f"PASS {lang} tracker approval at {size['width']}px")
        browser.close()

