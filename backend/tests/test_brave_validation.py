import httpx
import pytest

from app.services.discovery.brave import BraveSearchProvider


@pytest.mark.asyncio
@pytest.mark.parametrize(("target", "query", "wire_country", "wire_query"), [
    ("AE", 'site:.ae "Operations Analyst" United Arab Emirates', "ALL", 'site:.ae "Operations Analyst" United Arab Emirates'),
    (" ae ", '"CAFM Planner" UAE -site:linkedin.com', "ALL", '"CAFM Planner" UAE -site:linkedin.com'),
    ("AE", "analyst Dubai", "ALL", "analyst Dubai UAE"),
    ("QA", "engineer Doha", "ALL", "engineer Doha Qatar"),
    ("sa", "مهندس الرياض", "SA", "مهندس الرياض"),
    ("", "office coordinator", "ALL", "office coordinator"),
    ("ALL", "global vacancies", "ALL", "global vacancies"),
])
async def test_brave_market_filter_preserves_target(target, query, wire_country, wire_query):
    requests = []

    def search(request):
        requests.append(request)
        # Brave rejects unsupported country enum values even for otherwise valid searches.
        if request.url.params["country"] not in ("ALL", "SA"):
            return httpx.Response(422)
        return httpx.Response(200, json={"web": {"results": [{"title": "Role", "url": "https://jobs.test"}]}})

    provider = BraveSearchProvider("fake", httpx.MockTransport(search))
    out = await provider.search(query, country=target, page=1, max_results=10, freshness_days=14)
    assert len(requests) == 1 and not out.error
    assert dict(requests[0].url.params) == {"q": wire_query, "country": wire_country, "count": "10", "offset": "1", "freshness": "pm"}
    assert out.country_code == out.candidates[0].raw_metadata["query_country_code"] == target.strip().upper()
    assert out.query == out.candidates[0].search_query == wire_query
    assert out.candidates[0].raw_metadata["brave_country_filter"] == wire_country


@pytest.mark.asyncio
@pytest.mark.parametrize(("q", "country", "valid"), [
    ("x" * 600, "US", True), ("x" * 601, "US", False),
    ("jobs " * 75, "", True), ("jobs " * 76, "", False),
    ("x" * 600, "AE", False), ("   ", "AE", False),
])
async def test_brave_checks_final_wire_query_limits(q, country, valid):
    calls = []

    def search(request):
        calls.append(request)
        return httpx.Response(200, json={})

    out = await BraveSearchProvider("fake", httpx.MockTransport(search)).search(q, country=country, max_results=99, page=12)
    assert bool(calls) == valid
    if valid:
        assert not out.error and (calls[0].url.params["count"], calls[0].url.params["offset"]) == ("20", "9")
    else:
        assert "600 characters and 75 words" in out.error and out.estimated_cost_usd == 0


@pytest.mark.asyncio
async def test_brave_validation_only_exposes_known_fields_and_reasons():
    secret = "token-that-must-never-appear"
    errors = [
        {"loc": ["query", "country"], "type": "enum", "msg": secret, "input": secret, "ctx": {"expected": secret}},
        {"loc": ["query", "q"], "type": "string_too_long"},
        {"loc": ["header", "X-Subscription-Token"], "input": secret},
        {"loc": ["query", secret], "type": secret},
    ]
    transport = httpx.MockTransport(lambda _: httpx.Response(422, json={"error": {"detail": secret, "meta": {"errors": errors}}}))
    out = await BraveSearchProvider(secret, transport).search("Dubai jobs")
    assert out.error == "Brave Search API returned HTTP 422: query.country (unsupported value), query.q (too long)"


@pytest.mark.asyncio
async def test_brave_unstructured_errors_do_not_echo_credentials():
    secret = "token-that-must-never-appear"

    def disconnected(_):
        raise httpx.ConnectError(secret)

    for handler in (disconnected, lambda _: httpx.Response(422, text=secret), lambda _: httpx.Response(403, text=secret)):
        out = await BraveSearchProvider(secret, httpx.MockTransport(handler)).search("Dubai jobs")
        assert out.error and secret not in out.error
