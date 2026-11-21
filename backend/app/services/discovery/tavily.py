"""Tavily Search API adapter with country boosting, domain filtering, and publication date parsing."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings
from app.services.discovery.base import DiscoveredCandidate, SearchProviderOutcome
from app.services.geo import COUNTRY_QUERY_SHORT_NAMES, infer_location_hint_with_confidence


def _freshness_to_tavily_range(days: int) -> str:
    if days <= 1:
        return "day"
    if days <= 7:
        return "week"
    if days <= 31:
        return "month"
    return "year"


def _country_code_to_tavily_name(country_code: str, country_name: str = "") -> str:
    """Tavily requires full lowercase plain-English country names when filtering `country`."""
    code = (country_code or "").upper().strip()
    if code == "AE":
        return "united arab emirates"
    if code == "SA":
        return "saudi arabia"
    if code == "GB":
        return "united kingdom"
    if code == "US":
        return "united states"
    if country_name:
        return country_name.strip().lower()
    names = COUNTRY_QUERY_SHORT_NAMES.get(code, ())
    return names[-1].lower() if names else ""


def parse_provider_published_date(raw_val: str | None, now: datetime) -> datetime | None:
    if not raw_val:
        return None
    s = str(raw_val).strip()
    # Try ISO date/datetime
    try:
        cleaned = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass
    # Relative dates like "4 days ago", "2 hours ago", "1 week ago"
    m = re.search(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", s, re.IGNORECASE)
    if m:
        qty = int(m.group(1))
        unit = m.group(2).lower()
        mult = {"minute": timedelta(minutes=1), "hour": timedelta(hours=1), "day": timedelta(days=1), "week": timedelta(weeks=1), "month": timedelta(days=30)}
        return now - (mult[unit] * qty)
    return None


class TavilySearchProvider:
    name = "tavily"

    def __init__(self, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.tavily_api_key
        self.cost_per_call = settings.cost_rate_tavily_per_call
        self.transport = transport

    async def search(
        self,
        query: str,
        *,
        freshness_days: int = 14,
        max_results: int = 10,
        country: str = "",
        country_name: str = "",
        page: int = 0,
        exclude_domains: list[str] | None = None,
        include_domains: list[str] | None = None,
        source_group: str = "",
    ) -> SearchProviderOutcome:
        if not self.api_key:
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                error="TAVILY_API_KEY is not configured",
                country_code=country,
                page=page,
                source_group=source_group,
            )

        payload: dict[str, object] = {
            "query": query,
            "search_depth": "advanced",
            "topic": "general",
            "time_range": _freshness_to_tavily_range(freshness_days),
            "max_results": min(max(1, max_results), 20),
            "include_raw_content": False,
        }
        tavily_country = _country_code_to_tavily_name(country, country_name)
        # Do not pair Tavily GeoIP `country` filter with curated `include_domains`: many verified regional
        # .com/.club job boards (e.g. gocareer.club, uaehrs.com) are CDN-hosted outside the target country.
        if tavily_country and not include_domains:
            payload["country"] = tavily_country
        if exclude_domains:
            payload["exclude_domains"] = list(exclude_domains)[:150]
        if include_domains:
            payload["include_domains"] = list(include_domains)[:150]

        try:
            async with httpx.AsyncClient(timeout=get_settings().search_timeout_seconds, transport=self.transport) as client:
                resp = await client.post(
                    "https://api.tavily.com/search",
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                    },
                    json=payload,
                )
            if resp.status_code != 200:
                return SearchProviderOutcome(
                    provider_name=self.name,
                    query=query,
                    error=f"Tavily API returned HTTP {resp.status_code}",
                    estimated_cost_usd=self.cost_per_call,
                    country_code=country,
                    page=page,
                    source_group=source_group,
                )
            data = resp.json()
            now = datetime.now(timezone.utc)
            candidates: list[DiscoveredCandidate] = []
            for item in data.get("results", []):
                url = str(item.get("url", ""))
                domain = urlparse(url).hostname or ""
                title = html.unescape(str(item.get("title", "")))[:240]
                snippet = html.unescape(str(item.get("content", "")))
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
                            "score": item.get("score"),
                            "tavily_published_date": item.get("published_date"),
                            "query_country_code": country,
                            "source_group": source_group,
                        },
                    )
                )
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                candidates=candidates,
                estimated_cost_usd=self.cost_per_call,
                country_code=country,
                page=page,
                source_group=source_group,
            )
        except Exception as exc:
            return SearchProviderOutcome(
                provider_name=self.name,
                query=query,
                error=f"Tavily search request failed: {exc}",
                country_code=country,
                page=page,
                source_group=source_group,
            )
