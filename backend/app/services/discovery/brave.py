"""Brave Web Search API adapter with country targeting, offset pagination, and age parsing."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import pycountry

from app.core.config import get_settings
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.discovery.tavily import parse_provider_published_date
from app.services.geo import infer_location_hint_with_confidence

# GET /res/v1/web/search country enum, distinct from the ISO list used by the location headers.
# https://api-dashboard.search.brave.com/api-reference/web/search/get
BRAVE_COUNTRIES = frozenset(
    "AR AU AT BE BR CA CL DK FI FR DE GR HK IN ID IT JP KR MY MX NL NZ NO CN PL PT PH RU SA ZA ES SE CH TW TR GB US ALL".split()
)
MAX_QUERY_CHARS = 600
MAX_QUERY_WORDS = 75


def _query_for_worldwide_search(query: str, country: str) -> str:
    """Keep the target in the text when Brave cannot apply its country filter."""
    target = pycountry.countries.get(alpha_2=country) if len(country) == 2 else None
    if not target:
        return query
    name = getattr(target, "common_name", target.name)
    terms = [name, target.name]
    if country == "AE":
        terms.extend(("UAE", "U.A.E.", "الإمارات"))
        name = "UAE"
    if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", query, re.IGNORECASE) for term in terms):
        return query
    return f"{query} {name}"


def _http_error(response: httpx.Response) -> str:
    message = f"Brave Search API returned HTTP {response.status_code}"
    if response.status_code != 422:
        return message
    # Provider messages, inputs, headers and ctx can contain user data or credentials.
    # Only translate recognized field names and validation types into local text.
    try:
        body = response.json()
    except (ValueError, UnicodeError):
        return message
    error = body.get("error") if isinstance(body, dict) else None
    meta = error.get("meta") if isinstance(error, dict) else None
    issues = meta.get("errors") if isinstance(meta, dict) else None
    details = []
    reasons = {"enum": "unsupported value", "string_too_long": "too long", "string_too_short": "too short",
               "missing": "missing", "greater_than_equal": "below minimum", "less_than_equal": "above maximum"}
    for issue in issues if isinstance(issues, list) else ():
        loc = issue.get("loc") if isinstance(issue, dict) else None
        if not isinstance(loc, list) or len(loc) != 2 or loc[0] != "query" or loc[1] not in ("country", "q", "count", "offset", "freshness"):
            continue
        kind = issue.get("type")
        reason = reasons.get(kind, "invalid value") if isinstance(kind, str) else "invalid value"
        detail = f"query.{loc[1]} ({reason})"
        if detail not in details:
            details.append(detail)
    return f"{message}: {', '.join(details)}" if details else message


def _freshness_to_brave(days: int) -> str:
    if days <= 1:
        return "pd"
    if days <= 7:
        return "pw"
    if days <= 31:
        return "pm"
    return "py"


class BraveSearchProvider:
    name = "brave"

    def __init__(self, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.brave_api_key
        self.cost_per_call = settings.cost_rate_brave_per_call
        self.transport = transport

    async def search(
        self,
        query: str,
        *,
        freshness_days: int = 14,
        max_results: int = 10,
        country: str = "",
        page: int = 0,
        source_group: str = "",
    ) -> SearchProviderOutcome:
        if not self.api_key:
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                error="BRAVE_API_KEY is not configured",
                country_code=country,
                page=page,
                source_group=source_group,
            )

        cc = (country or "").upper().strip()
        filter_country = cc if cc in BRAVE_COUNTRIES else "ALL"
        query = query.strip()
        if query and cc and cc not in BRAVE_COUNTRIES:
            query = _query_for_worldwide_search(query, cc)
        if not query or len(query) > MAX_QUERY_CHARS or len(query.split()) > MAX_QUERY_WORDS:
            return SearchProviderOutcome(
                provider_name=self.name, query=query, country_code=cc, page=page, source_group=source_group,
                error=f"Brave query.q requires non-empty text of at most {MAX_QUERY_CHARS} characters and {MAX_QUERY_WORDS} words",
            )
        params = {
            "q": query,
            "country": filter_country,
            "count": min(max(1, max_results), 20),
            "offset": min(max(0, page), 9),
            "freshness": _freshness_to_brave(freshness_days),
        }

        try:
            async with httpx.AsyncClient(timeout=get_settings().search_timeout_seconds, transport=self.transport) as client:
                resp = await client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    headers={
                        "Accept": "application/json",
                        "X-Subscription-Token": self.api_key,
                    },
                    params=params,
                )
            if resp.status_code != 200:
                return SearchProviderOutcome(
                    provider_name=self.name,
                    query=query,
                    error=_http_error(resp),
                    estimated_cost_usd=self.cost_per_call,
                    country_code=cc,
                    page=page,
                    source_group=source_group,
                )
            data = resp.json()
            now = datetime.now(timezone.utc)
            candidates: list[DiscoveredCandidate] = []
            for item in data.get("web", {}).get("results", []):
                url = str(item.get("url", ""))
                domain = urlparse(url).hostname or ""
                title = html.unescape(str(item.get("title", "")))[:240]
                desc = html.unescape(str(item.get("description", "")))
                extra = html.unescape(" ".join(item.get("extra_snippets", []) or []))
                snippet = f"{desc}\n{extra}".strip()
                # A web page's age is not the date its vacancy was posted.
                pub_dt = parse_provider_published_date(item.get("published_date"), now)
                loc_hint, loc_conf = infer_location_hint_with_confidence(title=title, url=url, jd_text=snippet)
                candidates.append(
                    DiscoveredCandidate(
                        source_provider=self.name,
                        source_url=url,
                        source_type="job_post",
                        title=title,
                        company_name="",
                        company_domain=domain.lower(),
                        jd_text=snippet,
                        location_raw=loc_hint,
                        location_confidence=loc_conf,
                        retrieved_at=now,
                        posted_at=pub_dt,
                        date_source="provider_metadata" if pub_dt else "unknown",
                        search_query=query,
                        raw_metadata={
                            "age": item.get("age"),
                            "page_age": item.get("page_age"),
                            "query_country_code": cc,
                            "brave_country_filter": filter_country,
                            "source_group": source_group,
                        },
                    )
                )
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                candidates=candidates,
                estimated_cost_usd=self.cost_per_call,
                country_code=cc,
                page=page,
                source_group=source_group,
            )
        except Exception as exc:
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                error="Brave search request timed out" if isinstance(exc, httpx.TimeoutException) else "Brave search request failed",
                country_code=cc,
                page=page,
                source_group=source_group,
            )
