"""Render the built tracker in both locales with intercepted API and asset requests."""

import mimetypes
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright


def check_tracker(browser, locale, dist):
    ar = locale == "ar"
    app = dict(id="app-1", company_name="Fixture Ltd", role_title="Analyst", transport_status="send_uncertain",
               queue_state="uncertain", response_status="human_reply", outcome_status="open",
               approval_state="approved", verification_status="verified")
    item = dict(id="source-1", validated_employer="Fixture Ltd", access_status="fetched_200",
                discovery_category="verified_email_vacancy", primary_skip_code="OUTSIDE_TARGET_COUNTRY")
    responses = {
        "auth/me": {"user": {"id": "u1", "email": "owner@example.test", "ui_locale": locale,
                             "full_name": "Fixture", "onboarding": {}, "timezone": "UTC"}},
        "applications": {"applications": [app], "total": 1},
        "applications/app-1": {"application": app, "source_evidence": {}, "match_explanation": {},
                               "followup": {"status": "awaiting_approval"}},
        "discovery-sources": {"items": [item], "total": 1},
    }

    def serve(route):
        url = urlsplit(route.request.url)
        if url.hostname != "app.test" or route.request.method != "GET":
            route.abort()
        elif url.path.startswith("/api/v1/"):
            route.fulfill(json=responses.get(url.path.removeprefix("/api/v1/"), {}))
        else:
            asset = dist / url.path.lstrip("/")
            if not asset.is_file():
                asset = dist / "index.html"
            route.fulfill(path=asset, content_type=mimetypes.guess_type(asset)[0] or "application/octet-stream")

    with browser.new_context(service_workers="block") as context:
        context.route("**/*", serve)
        page = context.new_page()
        page.goto("http://app.test/tracker")
        expect(page.locator("html")).to_have_attribute("dir", "rtl" if ar else "ltr")
        expect(page.get_by_role("columnheader", name="جهة التوظيف الفعلية" if ar else "Actual Employer")).to_be_visible()
        transport = page.get_by_role("combobox", name="حالة الإرسال" if ar else "Transport status", exact=True)
        uncertain = "غير مؤكد" if ar else "Uncertain"
        with page.expect_response(lambda r: parse_qs(urlsplit(r.url).query).get("transport_status") == ["send_uncertain"]):
            transport.select_option(label=uncertain)
        row = page.get_by_role("row").filter(has_text="Fixture Ltd")
        expect(row).to_contain_text(uncertain)
        expect(row).to_contain_text("رد شخصي" if ar else "Personal reply")
        row.click()
        expect(page.get_by_text("الموضوع:" if ar else "Subject:", exact=True)).to_be_visible()
        expect(page.get_by_text("بانتظار الموافقة" if ar else "Awaiting approval", exact=False)).to_be_visible()
        for code in ("awaiting_approval", "human_reply", "send_uncertain"):
            expect(page.locator("body")).not_to_contain_text(code)

        page.get_by_role("button", name="مصادر الاكتشاف والأدلة الخام" if ar else "Raw Discovery & Evidence").click()
        expect(page.get_by_role("columnheader", name="إمكانية الوصول" if ar else "Access", exact=True)).to_be_visible()
        reasons = page.get_by_role("combobox", name="سبب التخطي الرئيسي" if ar else "Primary skip reason")
        reason = "خارج الدولة المستهدفة" if ar else "Outside target country"
        with page.expect_response(lambda r: parse_qs(urlsplit(r.url).query).get("skip_code") == ["OUTSIDE_TARGET_COUNTRY"]):
            reasons.select_option(label=reason)
        expect(page.get_by_role("cell", name=reason, exact=True)).to_be_visible()
        expect(page.get_by_role("cell", name="تم جلب الصفحة" if ar else "Page retrieved", exact=True)).to_be_visible()


if __name__ == "__main__":
    dist = Path(os.environ.get("TRACKER_TEST_DIST", Path(__file__).resolve().parents[1] / "dist"))
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME_EXECUTABLE"))
        for language in ("en", "ar"):
            check_tracker(browser, language, dist)
            print(f"{language}: tracker localization passed", flush=True)
        browser.close()
