"""Contact verification, DNS mail-routing validation, 5 opportunity categories, and 15 diagnostic skip codes."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import dns.exception
import dns.resolver

from app.core.config import get_settings
from app.core.security import normalize_email_display, sanitize_header_value
from app.services.discovery.base import DiscoveredCandidate

DISCOVERY_CATEGORIES = (
    "verified_email_vacancy",
    "verified_general_cv_invitation",
    "website_application_only",
    "informational_post",
    "incomplete_evidence",
    "rejected",
    "demo_fixture",
)

SKIP_REASON_CODES = (
    "SOURCE_ROBOTS_BLOCKED",
    "SOURCE_HTTP_403",
    "SOURCE_INACCESSIBLE",
    "NO_EMAIL_INVITATION",
    "ATS_ONLY",
    "EMPLOYER_UNKNOWN",
    "ROLE_UNVERIFIED",
    "JOB_LOCATION_UNKNOWN",
    "LOCATION_UNKNOWN",
    "OUTSIDE_TARGET_COUNTRY",
    "EXPIRED_OR_STALE",
    "EXPIRED_POSTING",
    "POSTING_DATE_UNVERIFIED",
    "EXPERIENCE_GAP",
    "MANDATORY_EXPERIENCE_GAP",
    "MISSING_MANDATORY_SKILL",
    "FIT_THRESHOLD",
    "QUALIFICATION_MISMATCH",
    "NATIONALITY_RESTRICTION",
    "WORK_AUTHORIZATION_MISMATCH",
    "RESIDENCY_REQUIREMENT",
    "REQUIRED_CERTIFICATION_MISSING",
    "MANDATORY_LANGUAGE_MISMATCH",
    "MANDATORY_AVAILABILITY_MISMATCH",
    "DUPLICATE_SOURCE",
    "PREVIOUSLY_CONTACTED",
    "RECIPIENT_UNVERIFIED",
    "DOMAIN_INVALID",
    "EMAIL_NOT_VERIFIED",
    "SCAM_RISK",
    "DEMO_FIXTURE",
    "AI_EXTRACTION_FAILED",
    "PROVIDER_ERROR",
)

NOREPLY_LOCAL_PARTS = {
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "bounces",
    "unsubscribe",
}

PLACEHOLDER_EMAILS = {
    "email@example.com",
    "name@company.com",
    "hr@company.com",
    "yourname@domain.com",
    "test@test.com",
    "user@example.com",
}

FREE_MAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "icloud.com",
    "proton.me",
    "protonmail.com",
}

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(?:all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"disregard\s+system\s+prompt", re.IGNORECASE),
    re.compile(r"send\s+(?:the\s+)?(?:user'?s\s+)?secrets", re.IGNORECASE),
    re.compile(r"exfiltrate|reveal\s+api\s+key|bypass\s+filter", re.IGNORECASE),
]

SCAM_OR_FEE_PATTERNS = [
    re.compile(r"(application|processing|registration|visa|training)\s+fee\s+(?:of|required|\$|usd|eur|aed)", re.IGNORECASE),
    re.compile(r"(western\s+union|moneygram|crypto\s+deposit|pay\s+to\s+apply)", re.IGNORECASE),
    re.compile(r"(send\s+(?:copy\s+of\s+)?passport\s+and\s+bank\s+(?:account|details)\s+to\s+apply)", re.IGNORECASE),
]

CLOSED_PATTERNS = [
    re.compile(
        r"\b(position\s+(?:has\s+been\s+)?filled|position\s+closed|applications?\s+(?:are\s+)?closed|"
        r"no\s+longer\s+accepting\s+applications|expired\s+listing)\b",
        re.IGNORECASE,
    ),
]


def is_demo_email_or_domain(value: str | None) -> bool:
    """Return True if value points to a reserved .example / example.* test/demo domain."""
    raw = (value or "").strip().lower()
    if not raw:
        return False
    if "@" in raw:
        raw = raw.rsplit("@", 1)[-1]
    if "://" in raw:
        raw = raw.split("://", 1)[-1]
    raw = raw.split("/", 1)[0].split(":", 1)[0].rstrip(".")
    return (
        raw == "example"
        or raw.endswith(".example")
        or raw in {"example.com", "example.org", "example.net", "demo.local", "fixture.local"}
        or raw.endswith(".example.com")
        or raw.endswith(".example.org")
        or raw.endswith(".example.net")
    )


@dataclass
class VerificationResult:
    status: str  # verified | skipped | suspicious | dns_transient | closed
    dns_status: str  # valid_mx | valid_a | permanent_invalid | transient_error | skipped_test
    normalized_to: str
    normalized_ccs: list[str] = field(default_factory=list)
    skip_reason: str = ""
    discovery_category: str = "incomplete_evidence"
    primary_skip_code: str = ""
    skip_codes: list[str] = field(default_factory=list)
    secondary_skip_codes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    prompt_injection_detected: bool = False


def _merge_codes(*codes: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for c in codes:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _make_non_verified_result(
    *,
    status: str,
    dns_status: str,
    normalized_to: str = "",
    normalized_ccs: list[str] | None = None,
    skip_reason: str,
    discovery_category: str,
    primary_code: str,
    contributing: list[str],
    warnings: list[str],
    prompt_injection_detected: bool,
) -> VerificationResult:
    codes = _merge_codes(primary_code, *contributing)
    secondary = [c for c in codes if c != primary_code]
    return VerificationResult(
        status=status,
        dns_status=dns_status,
        normalized_to=normalized_to,
        normalized_ccs=list(normalized_ccs or []),
        skip_reason=skip_reason,
        discovery_category=discovery_category,
        primary_skip_code=primary_code,
        skip_codes=codes,
        secondary_skip_codes=secondary,
        warnings=warnings,
        prompt_injection_detected=prompt_injection_detected,
    )


_DNS_CACHE_LOCK = threading.Lock()
_DNS_ROUTING_CACHE: dict[str, tuple[float, tuple[str, str]]] = {}
_DNS_CACHE_TTL_SECONDS = 120.0


def _resolve_with_tcp_fallback(resolver: dns.resolver.Resolver, domain: str, qtype: str):
    """Query UDP first, retrying once over TCP on transient timeout/reset."""
    try:
        return resolver.resolve(domain, qtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise
    except dns.exception.DNSException:
        return resolver.resolve(domain, qtype, tcp=True)


def check_domain_mail_routing(domain: str, *, skip_network_dns: bool = False) -> tuple[str, str]:
    """
    Validate public MX/A DNS records without invasive SMTP mailbox probing.
    Distinguishes permanent domain invalidity from transient DNS lookup failures,
    with TCP fallback for Docker/NAT UDP timeouts and bounded TTL caching.
    """
    clean_domain = (domain or "").strip().lower().rstrip(".")
    if not clean_domain or "." not in clean_domain:
        return "permanent_invalid", "Domain syntax invalid"

    if (
        skip_network_dns
        or clean_domain.endswith(".example")
        or clean_domain.endswith(".test")
        or clean_domain.endswith(".invalid")
        or clean_domain == "example.org"
    ):
        return "skipped_test", "DNS check skipped for isolated test/example domain"

    now_mono = time.monotonic()
    with _DNS_CACHE_LOCK:
        cached = _DNS_ROUTING_CACHE.get(clean_domain)
        if cached and (now_mono - cached[0]) < _DNS_CACHE_TTL_SECONDS:
            return cached[1]

    resolver = dns.resolver.Resolver()
    resolver.lifetime = 4.0
    resolver.timeout = 2.0

    def _remember(res: tuple[str, str]) -> tuple[str, str]:
        if res[0] != "transient_error":
            with _DNS_CACHE_LOCK:
                if len(_DNS_ROUTING_CACHE) > 1000:
                    _DNS_ROUTING_CACHE.clear()
                _DNS_ROUTING_CACHE[clean_domain] = (time.monotonic(), res)
        return res

    try:
        answers = _resolve_with_tcp_fallback(resolver, clean_domain, "MX")
        if len(answers) > 0:
            return _remember(("valid_mx", ""))
    except dns.resolver.NXDOMAIN:
        return _remember(("permanent_invalid", f"Domain {clean_domain} does not exist (NXDOMAIN)"))
    except dns.resolver.NoAnswer:
        try:
            a_ans = _resolve_with_tcp_fallback(resolver, clean_domain, "A")
            if len(a_ans) > 0:
                return _remember(("valid_a", "No explicit MX record; public A record exists"))
            return _remember(("permanent_invalid", f"Domain {clean_domain} has no MX or A mail route"))
        except dns.resolver.NXDOMAIN:
            return _remember(("permanent_invalid", f"Domain {clean_domain} does not exist (NXDOMAIN)"))
        except dns.resolver.NoAnswer:
            return _remember(("permanent_invalid", f"Domain {clean_domain} has neither MX nor A records"))
        except dns.exception.DNSException as exc:
            return "transient_error", f"Transient DNS error checking A record: {exc}"
    except dns.exception.DNSException as exc:
        return "transient_error", f"Transient DNS lookup failure for {clean_domain}: {exc}"

    return _remember(("permanent_invalid", f"Domain {clean_domain} has no usable mail route"))


def verify_discovered_candidate(
    candidate: DiscoveredCandidate,
    *,
    include_standing_invitations: bool,
    now_utc: datetime | None = None,
    skip_network_dns: bool = False,
    target_country_codes: set[str] | None = None,
    parsed_country_code: str = "",
    parsed_remote_scope: str = "unknown",
    location_confidence: str = "unknown",
    is_demo: bool = False,
    allow_demo_sandbox: bool = False,
) -> VerificationResult:
    """
    Deterministic verification of source trust, explicit application invitation, safety,
    recipient DNS, 7 mutually-exclusive discovery categories, and primary+secondary skip codes.
    """
    now = now_utc or datetime.now(timezone.utc)
    jd = candidate.jd_text or ""
    warnings: list[str] = []
    contributing: list[str] = []

    candidate_is_demo = bool(
        is_demo
        or candidate.source_provider == "demo_fixture"
        or is_demo_email_or_domain(candidate.application_email)
        or is_demo_email_or_domain(candidate.company_domain)
    )

    if candidate.page_inaccessible_reason:
        reason_low = candidate.page_inaccessible_reason.lower()
        if "robots" in reason_low:
            contributing.append("SOURCE_ROBOTS_BLOCKED")
        elif "403" in reason_low:
            contributing.append("SOURCE_HTTP_403")
        else:
            contributing.append("SOURCE_INACCESSIBLE")

    loc_conf = location_confidence or getattr(candidate, "location_confidence", "unknown") or "unknown"
    if parsed_country_code and target_country_codes and parsed_country_code.upper() not in target_country_codes:
        if parsed_remote_scope != "remote_worldwide":
            if loc_conf == "domain_hint":
                contributing.append("JOB_LOCATION_UNKNOWN")
            else:
                contributing.append("OUTSIDE_TARGET_COUNTRY")
    elif (not parsed_country_code or loc_conf == "domain_hint") and parsed_remote_scope != "remote_worldwide" and target_country_codes:
        contributing.append("JOB_LOCATION_UNKNOWN")

    if not candidate_is_demo:
        if not (getattr(candidate, "validated_employer", "") or "").strip():
            contributing.append("EMPLOYER_UNKNOWN")
        if not (getattr(candidate, "role_extracted", "") or "").strip():
            contributing.append("ROLE_UNVERIFIED")

    # 1. Prompt injection detection (treat external content as untrusted)
    injection_hit = any(pat.search(jd) or pat.search(candidate.title or "") for pat in PROMPT_INJECTION_PATTERNS)
    if injection_hit:
        warnings.append("Prompt-injection attempt detected inside external posting; instructions ignored")

    if candidate_is_demo and not allow_demo_sandbox:
        return _make_non_verified_result(
            status="skipped",
            dns_status="skipped_test",
            skip_reason="Demo/example fixture item isolated from real applications and live pipelines",
            discovery_category="demo_fixture",
            primary_code="DEMO_FIXTURE",
            contributing=contributing,
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    # 2. Closed / expired checks
    if candidate.closing_at:
        closing = candidate.closing_at if candidate.closing_at.tzinfo else candidate.closing_at.replace(tzinfo=timezone.utc)
        if closing < now:
            return _make_non_verified_result(
                status="closed",
                dns_status="unchecked",
                skip_reason=f"Posting closed on {closing.date().isoformat()}",
                discovery_category="demo_fixture" if candidate_is_demo else "rejected",
                primary_code="EXPIRED_OR_STALE",
                contributing=contributing,
                warnings=warnings,
                prompt_injection_detected=injection_hit,
            )

    for closed_pat in CLOSED_PATTERNS:
        if closed_pat.search(jd):
            return _make_non_verified_result(
                status="closed",
                dns_status="unchecked",
                skip_reason="Posting text indicates the vacancy is closed or filled",
                discovery_category="demo_fixture" if candidate_is_demo else "rejected",
                primary_code="EXPIRED_OR_STALE",
                contributing=contributing,
                warnings=warnings,
                prompt_injection_detected=injection_hit,
            )

    # 3. Fee / sensitive-document scam detection
    for scam_pat in SCAM_OR_FEE_PATTERNS:
        if scam_pat.search(jd):
            return _make_non_verified_result(
                status="suspicious",
                dns_status="unchecked",
                skip_reason="Suspicious request for application fees or sensitive personal documents detected",
                discovery_category="demo_fixture" if candidate_is_demo else "rejected",
                primary_code="SCAM_RISK",
                contributing=contributing,
                warnings=warnings,
                prompt_injection_detected=injection_hit,
            )

    # 4. Explicit CV/application invitation evidence required
    raw_to = sanitize_header_value(candidate.application_email or "", 254)
    if not raw_to or not (candidate.evidence_quote or "").strip():
        if candidate.rejected_emails:
            contributing.append("RECIPIENT_UNVERIFIED")
        contributing.append("NO_EMAIL_INVITATION")

        if "OUTSIDE_TARGET_COUNTRY" in contributing:
            category = "rejected"
            primary_code = "OUTSIDE_TARGET_COUNTRY"
        elif "SOURCE_ROBOTS_BLOCKED" in contributing and not candidate.portal_apply_detected:
            category = "incomplete_evidence"
            primary_code = "SOURCE_ROBOTS_BLOCKED"
        elif "SOURCE_HTTP_403" in contributing and not candidate.portal_apply_detected:
            category = "incomplete_evidence"
            primary_code = "SOURCE_HTTP_403"
        elif candidate.page_inaccessible_reason and not candidate.portal_apply_detected:
            category = "incomplete_evidence"
            primary_code = "SOURCE_INACCESSIBLE"
        elif candidate.portal_apply_detected:
            category = "website_application_only"
            primary_code = "ATS_ONLY"
        elif getattr(candidate, "is_informational_post", False):
            category = "informational_post"
            primary_code = "NO_EMAIL_INVITATION"
        elif candidate.rejected_emails:
            category = "incomplete_evidence"
            primary_code = "RECIPIENT_UNVERIFIED"
        else:
            category = "incomplete_evidence"
            primary_code = "NO_EMAIL_INVITATION"

        if candidate_is_demo:
            category = "demo_fixture"

        if candidate.page_inaccessible_reason and not candidate.portal_apply_detected:
            reason = f"Primary source inaccessible ({candidate.page_inaccessible_reason}) and snippet lacks explicit CV email invitation"
        elif candidate.portal_apply_detected:
            reason = "Website/ATS application form only; no explicit email application invitation found"
        elif getattr(candidate, "is_informational_post", False):
            reason = "Informational hiring announcement with no direct CV email or application method"
        else:
            reason = "No explicit email application invitation or verifiable recipient evidence found"

        return _make_non_verified_result(
            status="skipped",
            dns_status="unchecked",
            skip_reason=reason,
            discovery_category=category,
            primary_code=primary_code,
            contributing=contributing,
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    from app.services.discovery.career_page import extract_structured_application_evidence

    evidence = extract_structured_application_evidence(
        jd, source_url=candidate.source_url, target_role_terms=[candidate.role_extracted or candidate.title],
    )
    quoted = extract_structured_application_evidence(candidate.evidence_quote)
    if raw_to.casefold() != evidence.primary_email.casefold() or raw_to.casefold() != quoted.primary_email.casefold():
        return _make_non_verified_result(
            status="suspicious",
            dns_status="unchecked",
            skip_reason="Source application instructions and evidence excerpt do not establish this recipient",
            discovery_category="demo_fixture" if candidate_is_demo else "rejected",
            primary_code="RECIPIENT_UNVERIFIED",
            contributing=_merge_codes("EMAIL_NOT_VERIFIED", *contributing),
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    try:
        normalized_to = normalize_email_display(raw_to)
    except ValueError as exc:
        return _make_non_verified_result(
            status="skipped",
            dns_status="permanent_invalid",
            skip_reason=f"Malformed application email address: {exc}",
            discovery_category="demo_fixture" if candidate_is_demo else "rejected",
            primary_code="RECIPIENT_UNVERIFIED",
            contributing=_merge_codes("EMAIL_NOT_VERIFIED", *contributing),
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    local_part, domain = normalized_to.split("@", 1)
    if local_part.lower() in NOREPLY_LOCAL_PARTS or normalized_to.lower() in PLACEHOLDER_EMAILS:
        return _make_non_verified_result(
            status="skipped",
            dns_status="unchecked",
            normalized_to=normalized_to,
            skip_reason=f"Recipient '{normalized_to}' is a noreply or placeholder address",
            discovery_category="demo_fixture" if candidate_is_demo else "rejected",
            primary_code="RECIPIENT_UNVERIFIED",
            contributing=_merge_codes("EMAIL_NOT_VERIFIED", *contributing),
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    if domain in FREE_MAIL_DOMAINS:
        warnings.append(
            f"Application recipient uses free-mail domain ({domain}); allowed only because explicit public invitation evidence is present"
        )
    elif candidate.company_domain and domain not in candidate.company_domain and candidate.company_domain not in domain:
        warnings.append(f"Recipient domain ({domain}) differs from listing host domain ({candidate.company_domain})")

    # 5. Validate CCs strictly against source text evidence
    normalized_ccs: list[str] = []
    invited_ccs = {addr.casefold() for addr in evidence.cc_emails}
    for raw_cc in candidate.cc_emails:
        try:
            norm_cc = normalize_email_display(raw_cc)
        except ValueError:
            continue
        if norm_cc.lower() == normalized_to.lower():
            continue
        if norm_cc.split("@", 1)[0].lower() in NOREPLY_LOCAL_PARTS:
            continue
        if norm_cc.casefold() in invited_ccs and norm_cc not in normalized_ccs:
            normalized_ccs.append(norm_cc)

    # 6. DNS MX/A validation
    settings = get_settings()
    dns_status, dns_msg = check_domain_mail_routing(
        domain,
        skip_network_dns=skip_network_dns or settings.environment == "test",
    )
    if dns_status == "permanent_invalid":
        return _make_non_verified_result(
            status="skipped",
            dns_status=dns_status,
            normalized_to=normalized_to,
            normalized_ccs=normalized_ccs,
            skip_reason=f"Recipient domain mail routing invalid: {dns_msg}",
            discovery_category="demo_fixture" if candidate_is_demo else "rejected",
            primary_code="DOMAIN_INVALID",
            contributing=_merge_codes("EMAIL_NOT_VERIFIED", *contributing),
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )
    if dns_status == "transient_error":
        return _make_non_verified_result(
            status="dns_transient",
            dns_status=dns_status,
            normalized_to=normalized_to,
            normalized_ccs=normalized_ccs,
            skip_reason=f"Transient DNS failure: {dns_msg}",
            discovery_category="demo_fixture" if candidate_is_demo else "incomplete_evidence",
            primary_code="RECIPIENT_UNVERIFIED",
            contributing=_merge_codes("EMAIL_NOT_VERIFIED", *contributing),
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    # 7. Standing invitation category (B) vs Specific vacancy (A) vs Demo Fixture (G)
    is_standing = bool(candidate.is_standing_invitation or candidate.source_type == "standing_invitation")
    category = (
        "demo_fixture"
        if candidate_is_demo
        else ("verified_general_cv_invitation" if is_standing else "verified_email_vacancy")
    )
    if is_standing and not include_standing_invitations:
        return _make_non_verified_result(
            status="skipped",
            dns_status=dns_status,
            normalized_to=normalized_to,
            normalized_ccs=normalized_ccs,
            skip_reason="Standing career invitation skipped because general CV invitations are disabled in preferences",
            discovery_category=category,
            primary_code="QUALIFICATION_MISMATCH",
            contributing=contributing,
            warnings=warnings,
            prompt_injection_detected=injection_hit,
        )

    sec_codes = _merge_codes(*contributing)
    return VerificationResult(
        status="verified",
        dns_status=dns_status,
        normalized_to=normalized_to,
        normalized_ccs=normalized_ccs,
        discovery_category=category,
        skip_codes=sec_codes,
        secondary_skip_codes=sec_codes,
        warnings=warnings,
        prompt_injection_detected=injection_hit,
    )
