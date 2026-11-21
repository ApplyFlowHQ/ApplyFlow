"""Unicode CSV/XLSX tracker export (formula-injection hardened) and historical application import."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.core.security import sanitize_spreadsheet_cell
from app.db.models import (
    Application,
    Batch,
    DiscoverySourceSighting,
    GmailSentRecord,
    JobMatch,
    JobPosting,
    RecipientHistory,
)

TRACKER_HEADERS = [
    "Application ID",
    "Batch ID",
    "Batch Mode",
    "Is Demo",
    "Discovered At",
    "Sent At",
    "Actual Employer",
    "Source Domain",
    "Role Extracted",
    "Country",
    "City",
    "Recipient",
    "CCs",
    "Selected CV",
    "Evidence URL",
    "Fit Score",
    "Eligibility Decision",
    "Transport Status",
    "Response Type",
    "Outcome",
    "Follow-up Count",
    "Next Follow-up At",
    "Primary Skip Code",
    "Secondary Skip Codes",
    "Error / Skip Reason",
    "Verification State",
    "Initial Approval State",
    "Notes",
]

DISCOVERY_AUDIT_HEADERS = [
    "Batch ID",
    "Batch Mode",
    "Is Demo",
    "Seen At",
    "Source Provider",
    "Publisher",
    "Source Domain",
    "Actual Employer",
    "Purported Employer",
    "Role Extracted",
    "Country",
    "City",
    "Location Confidence",
    "Date Source",
    "Access Status",
    "Disposition / Category",
    "Duplicate",
    "Dedupe Reason",
    "Recipient",
    "Required Subject",
    "Evidence URL",
    "Search Query",
    "Primary Skip Code",
    "Secondary Skip Codes",
    "Skip / Evidence Note",
]


def _fmt_dt(dt: datetime | None, tz_name: str) -> str:
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = timezone.utc
        tz_name = "UTC"
    local_dt = dt.astimezone(tz)
    return f"{local_dt.strftime('%Y-%m-%d %H:%M')} ({tz_name})"


def build_tracker_rows(
    applications: list[Application],
    *,
    batches_by_id: dict[str, Batch],
    postings_by_id: dict[str, JobPosting],
    matches_by_id: dict[str, JobMatch],
    cv_labels_by_version_id: dict[str, str],
    tz_name: str,
) -> list[list[object]]:
    rows: list[list[object]] = []
    for app in applications:
        batch = batches_by_id.get(app.batch_id)
        posting = postings_by_id.get(app.job_posting_id)
        match = matches_by_id.get(app.job_match_id or "")
        cv_label = cv_labels_by_version_id.get(app.selected_cv_version_id or "", app.frozen_cv_filename)
        actual_employer = (posting.validated_employer if posting else "") or app.company_name
        source_domain = app.source_domain or (posting.company_domain if posting else "")
        role_extracted = (posting.role_extracted if posting else "") or app.role_title
        evidence_url = (posting.canonical_url if posting else "") or (posting.source_url if posting else "")
        elig_decision = (
            str(match.eligibility_check_json.get("status", ""))
            if match and isinstance(match.eligibility_check_json, dict) and match.eligibility_check_json.get("status")
            else ("rejected" if app.transport_status == "skipped" else "unknown")
        )
        primary_code = app.primary_skip_code or (posting.primary_skip_code if posting else "")
        secondary_codes = app.secondary_skip_codes_json or (posting.secondary_skip_codes_json if posting else []) or []
        raw_row = [
            app.id,
            app.batch_id,
            batch.mode if batch else "",
            "demo" if app.is_demo else "real",
            _fmt_dt(app.discovered_at, tz_name),
            _fmt_dt(app.sent_at, tz_name),
            actual_employer,
            source_domain,
            role_extracted,
            app.country_code,
            app.city,
            app.recipient_email,
            ", ".join(app.cc_emails_json or []),
            cv_label,
            evidence_url,
            round(match.fit_score, 1) if match else "",
            elig_decision,
            app.transport_status,
            app.response_status,
            app.outcome_status,
            app.follow_up_count,
            _fmt_dt(app.next_follow_up_at, tz_name),
            primary_code,
            ", ".join(secondary_codes),
            app.skip_or_error_reason,
            posting.verification_status if posting else "unknown",
            app.approval_state,
            app.notes,
        ]
        rows.append([sanitize_spreadsheet_cell(cell) for cell in raw_row])
    return rows


def build_discovery_audit_rows(
    sightings: list[DiscoverySourceSighting],
    *,
    batches_by_id: dict[str, Batch],
    postings_by_id: dict[str, JobPosting],
    tz_name: str,
) -> list[list[object]]:
    rows: list[list[object]] = []
    for s in sightings:
        batch = batches_by_id.get(s.batch_id)
        p = postings_by_id.get(s.job_posting_id or "")
        primary_code = s.primary_skip_code or (p.primary_skip_code if p else "")
        sec_codes = s.secondary_skip_codes_json or (p.secondary_skip_codes_json if p else []) or []
        raw_row = [
            s.batch_id,
            batch.mode if batch else "",
            "demo" if s.is_demo else "real",
            _fmt_dt(s.seen_at, tz_name),
            s.source_provider,
            s.publisher or (p.publisher if p else ""),
            s.source_domain or (p.company_domain if p else ""),
            p.validated_employer if p else "",
            p.purported_employer if p else "",
            (p.role_extracted if p else "") or s.title_raw,
            p.country_code if p else "",
            p.city if p else "",
            p.location_confidence if p else "",
            p.date_source if p else "",
            s.access_status or (p.access_status if p else ""),
            (p.discovery_category if p else "") or s.disposition,
            "yes" if s.is_duplicate else "no",
            s.dedupe_reason,
            p.application_email if p else "",
            p.subject_instruction if p else "",
            s.canonical_url or s.source_url,
            s.search_query or (p.search_query if p else ""),
            primary_code,
            ", ".join(sec_codes),
            p.skip_reason if p else s.dedupe_reason,
        ]
        rows.append([sanitize_spreadsheet_cell(cell) for cell in raw_row])
    return rows


def export_discovery_csv(rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(DISCOVERY_AUDIT_HEADERS)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode("utf-8-sig")


def export_applications_csv(rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(TRACKER_HEADERS)
    for row in rows:
        writer.writerow(row)
    # UTF-8 with BOM for clean Excel opening with Arabic/Unicode text
    return buf.getvalue().encode("utf-8-sig")


def _excel_dt(dt: datetime | None, tz_name: str):
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = timezone.utc
    # Excel expects timezone-naive datetimes; store normalized local wall time with clean ISO format display
    return dt.astimezone(tz).replace(tzinfo=None)


def _style_worksheet(ws, headers: list[str], data_rows: list[list[object]]) -> None:
    ws.append(headers)
    header_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center")

    for r_idx, row in enumerate(data_rows, start=2):
        safe_row: list[object] = []
        for val in row:
            if isinstance(val, (datetime, int, float)) and not isinstance(val, bool):
                safe_row.append(val)
            elif isinstance(val, bool):
                safe_row.append("yes" if val else "no")
            else:
                safe_row.append(sanitize_spreadsheet_cell(val))
        ws.append(safe_row)
        for c_idx, val in enumerate(safe_row, start=1):
            cell = ws.cell(row=r_idx, column=c_idx)
            if isinstance(val, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
            elif isinstance(val, str) and (val.startswith("https://") or val.startswith("http://")):
                cell.hyperlink = val
                cell.font = Font(color="2563EB", underline="single")

    ws.freeze_panes = "A2"
    if headers:
        last_col = get_column_letter(len(headers))
        last_row = max(1, len(data_rows) + 1)
        ws.auto_filter.ref = f"A1:{last_col}{last_row}"
        for col_idx, h in enumerate(headers, start=1):
            max_len = len(str(h))
            for r in data_rows[:200]:
                if col_idx - 1 < len(r):
                    v = r[col_idx - 1]
                    max_len = max(max_len, min(48, len(str(v or ""))))
            ws.column_dimensions[get_column_letter(col_idx)].width = max(13, min(48, max_len + 3))


def export_applications_xlsx(rows: list[list[object]], tz_name: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Applications"
    _style_worksheet(ws, TRACKER_HEADERS, rows)

    meta = wb.create_sheet("Metadata")
    meta.append(["Exported At UTC", datetime.now(timezone.utc).isoformat()])
    meta.append(["Display Timezone", tz_name or "UTC"])
    meta.append(["Authoritative Store", "ApplyFlow PostgreSQL Tracker"])

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def export_vnext_workbook_xlsx(
    *,
    applications: list[Application],
    batches: list[Batch],
    postings: list[JobPosting],
    sightings: list[DiscoverySourceSighting],
    matches_by_id: dict[str, JobMatch],
    cv_labels_by_version_id: dict[str, str],
    recipient_histories: list[RecipientHistory],
    gmail_sent_records: list[GmailSentRecord],
    tz_name: str,
    mailbox_senders: dict[str, str] | None = None,
) -> bytes:
    """Build the full Section 10.2 5-sheet ApplyFlow Global audit workbook."""
    wb = Workbook()
    batches_by_id = {b.id: b for b in batches}
    postings_by_id = {p.id: p for p in postings}

    # Sheet 1: Batch Summary
    ws_summary = wb.active
    ws_summary.title = "Batch Summary"
    summary_headers = [
        "Batch ID",
        "Pipeline Version",
        "Run Mode",
        "Status",
        "Goal Target",
        "Goal Met",
        "Goal Shortfall Reason",
        "Queries Executed",
        "Follow-up Queries",
        "Corroboration Queries",
        "Pages Fetched",
        "Raw URLs Discovered",
        "Unique Canonical URLs",
        "Tier 1 Official",
        "Tier 2 Authorized",
        "Tier 3 Third-Party Claim",
        "Eligible Drafts",
        "Needs Human Review",
        "Sent Accepted",
        "Send Uncertain",
        "Skipped",
        "Duplicates",
        "Estimated Cost USD",
        "Started At",
        "Finished At",
    ]
    summary_rows: list[list[object]] = []
    for b in batches:
        m = b.metrics_json or {}
        summary_rows.append(
            [
                b.id,
                getattr(b, "pipeline_version", "legacy"),
                getattr(b, "run_mode", b.mode) or b.mode,
                b.status,
                getattr(b, "target_goal_count", m.get("goal_target", 10)),
                "yes" if m.get("goal_met") else "no",
                getattr(b, "goal_shortfall_reason", ""),
                m.get("search_queries_executed", 0),
                m.get("followup_queries_executed", 0),
                m.get("corroboration_queries_executed", 0),
                m.get("pages_fetched", 0),
                m.get("raw_urls_discovered", b.discovered_count),
                m.get("unique_canonical_urls", b.discovered_count),
                m.get("vacancies_tier_1_official", 0),
                m.get("vacancies_tier_2_authorized", 0),
                m.get("vacancies_tier_3_third_party_claim", 0),
                b.eligible_count,
                m.get("needs_human_review", 0),
                b.sent_count,
                b.uncertain_count,
                b.skipped_count,
                b.duplicates_removed_count,
                round(float(b.estimated_cost_usd or 0.0), 4),
                _excel_dt(b.started_at, tz_name),
                _excel_dt(b.finished_at, tz_name),
            ]
        )
    summary_rows.append([])
    summary_rows.append(["Metadata: Exported At UTC", datetime.now(timezone.utc).isoformat(), "Timezone", tz_name or "UTC", "Store", "ApplyFlow PostgreSQL Tracker"])
    _style_worksheet(ws_summary, summary_headers, summary_rows)

    # Sheet 2: Verified Opportunities
    ws_opps = wb.create_sheet("Verified Opportunities")
    opps_headers = [
        "Posting ID",
        "Batch ID",
        "Verification Tier",
        "Corroboration Status",
        "Corroboration URL",
        "Actual Employer",
        "Role Extracted",
        "Requisition ID",
        "Country",
        "City",
        "Application Email",
        "CCs",
        "Required Subject",
        "Evidence Quote",
        "Source URL",
        "Posted At",
        "Retrieved At",
    ]
    opps_rows: list[list[object]] = []
    for p in postings:
        if p.verification_status != "verified" and not p.application_email:
            continue
        opps_rows.append(
            [
                p.id,
                p.batch_id,
                getattr(p, "source_verification_tier", "tier_5_unrelated_or_prohibited"),
                getattr(p, "corroboration_status", "uncorroborated"),
                getattr(p, "corroboration_url", ""),
                p.validated_employer or p.company_name,
                p.role_extracted or p.title,
                getattr(p, "requisition_id", ""),
                p.country_code,
                p.city,
                p.application_email,
                ", ".join(p.cc_emails_json or []),
                p.subject_instruction,
                p.evidence_quote,
                p.canonical_url or p.source_url,
                _excel_dt(p.posted_at, tz_name),
                _excel_dt(p.retrieved_at, tz_name),
            ]
        )
    _style_worksheet(ws_opps, opps_headers, opps_rows)

    # Sheet 3: Research & Skips
    ws_res = wb.create_sheet("Research & Skips")
    res_headers = [
        "Batch ID",
        "Research Stage",
        "Provider",
        "Publisher",
        "Source Domain",
        "Actual Employer",
        "Purported Employer",
        "Role / Title",
        "Category / Disposition",
        "Access Status",
        "Duplicate",
        "Dedupe Reason",
        "Primary Skip Code",
        "Secondary Skip Codes",
        "Skip / Corroboration Note",
        "Corroborated",
        "Source URL",
        "Search Query",
        "Seen At",
    ]
    res_rows: list[list[object]] = []
    for s in sightings:
        p = postings_by_id.get(s.job_posting_id or "")
        p_code = s.primary_skip_code or (p.primary_skip_code if p else "")
        sec_codes = s.secondary_skip_codes_json or (p.secondary_skip_codes_json if p else []) or []
        res_rows.append(
            [
                s.batch_id,
                getattr(s, "research_stage", "discovery_search"),
                s.source_provider,
                s.publisher or (p.publisher if p else ""),
                s.source_domain or (p.company_domain if p else ""),
                p.validated_employer if p else "",
                p.purported_employer if p else "",
                (p.role_extracted if p else "") or s.title_raw,
                (p.discovery_category if p else "") or s.disposition,
                s.access_status or (p.access_status if p else ""),
                "yes" if s.is_duplicate else "no",
                s.dedupe_reason,
                p_code,
                ", ".join(sec_codes),
                p.skip_reason if p else s.dedupe_reason,
                "yes" if getattr(s, "corroborated", False) else "no",
                s.canonical_url or s.source_url,
                s.search_query or (p.search_query if p else ""),
                _excel_dt(s.seen_at, tz_name),
            ]
        )
    _style_worksheet(ws_res, res_headers, res_rows)

    # Sheet 4: Applications & Outcomes
    ws_apps = wb.create_sheet("Applications & Outcomes")
    apps_headers = [
        *TRACKER_HEADERS[:14],
        "Source Tier",
        "Attachment PDF SHA-256",
        "Approval State",
        *TRACKER_HEADERS[14:17],
        "Review Reasons",
        *TRACKER_HEADERS[17:-2],
        "Notes",
    ]
    apps_rows: list[list[object]] = []
    for app in applications:
        batch = batches_by_id.get(app.batch_id)
        posting = postings_by_id.get(app.job_posting_id)
        match = matches_by_id.get(app.job_match_id or "")
        cv_label = cv_labels_by_version_id.get(app.selected_cv_version_id or "", app.frozen_cv_filename)
        actual_employer = (posting.validated_employer if posting else "") or app.company_name
        source_domain = app.source_domain or (posting.company_domain if posting else "")
        role_extracted = (posting.role_extracted if posting else "") or app.role_title
        evidence_url = (posting.canonical_url if posting else "") or (posting.source_url if posting else "")
        elig_decision = (
            str(match.eligibility_check_json.get("status", ""))
            if match and isinstance(match.eligibility_check_json, dict) and match.eligibility_check_json.get("status")
            else ("rejected" if app.transport_status == "skipped" else "unknown")
        )
        primary_code = app.primary_skip_code or (posting.primary_skip_code if posting else "")
        sec_codes = app.secondary_skip_codes_json or (posting.secondary_skip_codes_json if posting else []) or []
        apps_rows.append(
            [
                app.id,
                app.batch_id,
                getattr(batch, "run_mode", batch.mode if batch else "") if batch else "",
                "demo" if app.is_demo else "real",
                _excel_dt(app.discovered_at, tz_name),
                _excel_dt(app.sent_at, tz_name),
                actual_employer,
                source_domain,
                role_extracted,
                app.country_code,
                app.city,
                app.recipient_email,
                ", ".join(app.cc_emails_json or []),
                cv_label,
                getattr(app, "source_verification_tier", ""),
                getattr(app, "attachment_pdf_sha256", "") or app.frozen_cv_sha256,
                getattr(app, "approval_state", "draft"),
                evidence_url,
                round(match.fit_score, 1) if match else "",
                elig_decision,
                ", ".join(getattr(app, "review_reasons_json", []) or []),
                app.transport_status,
                app.response_status,
                app.outcome_status,
                app.follow_up_count,
                _excel_dt(app.next_follow_up_at, tz_name),
                primary_code,
                ", ".join(sec_codes),
                app.skip_or_error_reason,
                posting.verification_status if posting else "unknown",
                app.notes,
            ]
        )
    _style_worksheet(ws_apps, apps_headers, apps_rows)

    # Sheet 5: Dedup & Suppression
    ws_dedup = wb.create_sheet("Dedup & Suppression")
    dedup_headers = [
        "Record Source",
        "Normalized Email",
        "Display Email",
        "Company / Domain",
        "Classification",
        "Suppressed",
        "Notes / Subject",
        "Recorded / Sent At",
    ]
    dedup_rows: list[list[object]] = []
    for rh in recipient_histories:
        dedup_rows.append(
            [
                rh.source,
                rh.normalized_email,
                rh.display_email,
                rh.company_name,
                "ledger",
                "yes",
                rh.notes,
                _excel_dt(rh.imported_at or rh.created_at, tz_name),
            ]
        )
    from app.services.mailbox.gmail_sent import classify_sent_message_for_dedup, external_contact_key

    for gs in gmail_sent_records:
        sender = (mailbox_senders or {}).get(gs.mailbox_connection_id, "")
        for addr in gs.contact_keys_json or [gs.recipient_normalized]:
            blocked = bool(external_contact_key(addr, sender) and gs.confidence_reason != "Self-sent message")
            classification = gs.classification if addr == gs.recipient_normalized else classify_sent_message_for_dedup(
                sender_email=sender, recipient_email=addr, subject=gs.subject)[0]
            dedup_rows.append([
                "gmail_sent_sync", addr, gs.recipient_email if addr == gs.recipient_normalized else addr,
                addr.partition("@")[2], classification, "yes" if blocked else "no",
                gs.subject or gs.confidence_reason, _excel_dt(gs.sent_at or gs.created_at, tz_name),
            ])
    _style_worksheet(ws_dedup, dedup_headers, dedup_rows)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def parse_historical_import_file(filename: str, data: bytes) -> list[dict[str, str]]:
    """Parse CSV or XLSX historical applications for recipient deduplication."""
    entries: list[dict[str, str]] = []
    low = filename.lower()
    if low.endswith(".csv"):
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            lowered = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
            email_val = lowered.get("email") or lowered.get("recipient") or lowered.get("to") or ""
            if email_val:
                entries.append(
                    {
                        "email": email_val,
                        "company": lowered.get("company", ""),
                        "notes": lowered.get("notes", "Imported from CSV"),
                    }
                )
        return entries

    if low.endswith(".xlsx"):
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        header_row = next(rows_iter, None)
        if not header_row:
            return []
        headers = [str(h or "").strip().lower() for h in header_row]
        for r in rows_iter:
            row_dict = {headers[i]: str(r[i] or "").strip() for i in range(min(len(headers), len(r)))}
            email_val = row_dict.get("email") or row_dict.get("recipient") or row_dict.get("to") or ""
            if email_val:
                entries.append(
                    {
                        "email": email_val,
                        "company": row_dict.get("company", ""),
                        "notes": row_dict.get("notes", "Imported from XLSX"),
                    }
                )
        return entries

    raise ValueError("Historical import supports .csv or .xlsx files")
