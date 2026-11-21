"""Country-neutral salary modeling, pay-period normalization rules, and dated FX conversion."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.core.config import get_settings

VALID_PERIODS = ("hourly", "daily", "weekly", "monthly", "annual", "unknown")
VALID_BASES = ("gross", "net", "unknown")


@dataclass(frozen=True)
class FxRateQuote:
    base_currency: str
    quote_currency: str
    rate: float
    rate_date: str
    provider: str


@dataclass(frozen=True)
class SalaryComparisonResult:
    status: str  # "meets_minimum" | "below_minimum" | "unknown"
    reason: str
    original_amount: float | None = None
    original_currency: str = ""
    original_period: str = "unknown"
    normalized_amount: float | None = None
    normalized_currency: str = ""
    normalized_period: str = ""
    fx_rate: float | None = None
    fx_rate_date: str = ""


def parse_salary_from_text(text: str) -> tuple[float | None, float | None, str, str, str]:
    """Extract stated salary range, currency, pay period, and gross/net basis without guessing."""
    if not text:
        return None, None, "", "unknown", "unknown"

    currency_map = {
        "$": "USD",
        "usd": "USD",
        "€": "EUR",
        "eur": "EUR",
        "£": "GBP",
        "gbp": "GBP",
        "aed": "AED",
        "sar": "SAR",
        "egp": "EGP",
        "cad": "CAD",
        "aud": "AUD",
        "chf": "CHF",
        "sgd": "SGD",
        "jpy": "JPY",
        "¥": "JPY",
    }

    pattern = re.compile(
        r"(?P<curr>USD|EUR|GBP|AED|SAR|EGP|CAD|AUD|CHF|SGD|JPY|\$|€|£|¥)\s*"
        r"(?P<min>\d[\d,]*(?:\.\d+)?)(?P<min_k>[kK])?"
        r"(?:\s*[-–to]+\s*(?:USD|EUR|GBP|AED|SAR|EGP|CAD|AUD|CHF|SGD|JPY|\$|€|£|¥)?\s*"
        r"(?P<max>\d[\d,]*(?:\.\d+)?)(?P<max_k>[kK])?)?"
        r"(?:\s*(?:/|per)?\s*(?P<period>hour|hr|day|daily|week|weekly|month|monthly|mo|year|yr|annual|annually))?",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None, None, "", "unknown", "unknown"

    raw_curr = match.group("curr").lower()
    currency = currency_map.get(raw_curr, raw_curr.upper())
    val_min = float(match.group("min").replace(",", "")) * (1000.0 if match.group("min_k") else 1.0)
    val_max = None
    if match.group("max"):
        val_max = float(match.group("max").replace(",", "")) * (1000.0 if match.group("max_k") else 1.0)

    raw_period = (match.group("period") or "").lower()
    period_map = {
        "hour": "hourly",
        "hr": "hourly",
        "day": "daily",
        "daily": "daily",
        "week": "weekly",
        "weekly": "weekly",
        "month": "monthly",
        "mo": "monthly",
        "monthly": "monthly",
        "year": "annual",
        "yr": "annual",
        "annual": "annual",
        "annually": "annual",
    }
    period = period_map.get(raw_period, "unknown")

    basis = "unknown"
    window = text[max(0, match.start() - 40): min(len(text), match.end() + 40)].lower()
    if "gross" in window or "before tax" in window:
        basis = "gross"
    elif "net" in window or "after tax" in window or "take-home" in window:
        basis = "net"

    return val_min, val_max, currency, period, basis


def convert_period_without_assumed_hours(
    amount: float,
    from_period: str,
    to_period: str,
    weekly_hours: float | None = None,
) -> float | None:
    """
    Convert between pay periods ONLY when conversion is exact or explicitly bounded by stated hours.

    Never silently assume 40h/week or 2000h/year to compare hourly/daily against monthly/annual.
    """
    if from_period == to_period:
        return amount
    if from_period == "unknown" or to_period == "unknown":
        return None
    if from_period in ("monthly", "annual") and to_period in ("monthly", "annual"):
        return amount * 12.0 if (from_period == "monthly" and to_period == "annual") else amount / 12.0
    if from_period == "weekly" and to_period == "annual":
        return amount * 52.0
    if from_period == "annual" and to_period == "weekly":
        return amount / 52.0
    if from_period == "hourly" and weekly_hours and weekly_hours > 0:
        if to_period == "weekly":
            return amount * weekly_hours
        if to_period == "annual":
            return amount * weekly_hours * 52.0
        if to_period == "monthly":
            return (amount * weekly_hours * 52.0) / 12.0
    return None


def get_dated_fx_rate(
    from_currency: str,
    to_currency: str,
    static_rates: dict[str, float] | None = None,
) -> FxRateQuote | None:
    """Retrieve a dated exchange rate from configured FX provider, or None if unavailable."""
    src = (from_currency or "").upper()
    dst = (to_currency or "").upper()
    if not src or not dst:
        return None
    if src == dst:
        return FxRateQuote(
            base_currency=src,
            quote_currency=dst,
            rate=1.0,
            rate_date=datetime.now(timezone.utc).date().isoformat(),
            provider="identity",
        )

    settings = get_settings()
    rates_map = static_rates
    if rates_map is None and settings.fx_static_rates_json:
        try:
            rates_map = json.loads(settings.fx_static_rates_json)
        except Exception:
            rates_map = None

    if rates_map:
        pair_key = f"{src}_{dst}"
        if pair_key in rates_map:
            return FxRateQuote(
                base_currency=src,
                quote_currency=dst,
                rate=float(rates_map[pair_key]),
                rate_date=str(rates_map.get("_date", datetime.now(timezone.utc).date().isoformat())),
                provider="static_config",
            )
        inv_key = f"{dst}_{src}"
        if inv_key in rates_map and float(rates_map[inv_key]) > 0:
            return FxRateQuote(
                base_currency=src,
                quote_currency=dst,
                rate=1.0 / float(rates_map[inv_key]),
                rate_date=str(rates_map.get("_date", datetime.now(timezone.utc).date().isoformat())),
                provider="static_config",
            )

    if settings.fx_provider == "frankfurter":
        try:
            resp = httpx.get(
                f"{settings.fx_api_url.rstrip('/')}/latest",
                params={"from": src, "to": dst},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                rate_val = data.get("rates", {}).get(dst)
                if rate_val:
                    return FxRateQuote(
                        base_currency=src,
                        quote_currency=dst,
                        rate=float(rate_val),
                        rate_date=str(data.get("date", "")),
                        provider="frankfurter",
                    )
        except Exception:
            return None
    return None


def compare_salary_to_minimum(
    *,
    job_min: float | None,
    job_max: float | None,
    job_currency: str,
    job_period: str,
    job_weekly_hours: float | None,
    user_min: float | None,
    user_currency: str,
    user_period: str,
    static_fx_rates: dict[str, float] | None = None,
) -> SalaryComparisonResult:
    """Compare stated job compensation against user minimum without fabricating hours or FX rates."""
    if user_min is None:
        return SalaryComparisonResult(status="meets_minimum", reason="No minimum salary configured")

    job_amt = job_max if job_max is not None else job_min
    if job_amt is None or not job_currency:
        return SalaryComparisonResult(status="unknown", reason="Job posting does not state salary amount or currency")

    converted_period_amt = convert_period_without_assumed_hours(
        job_amt,
        job_period,
        user_period,
        weekly_hours=job_weekly_hours,
    )
    if converted_period_amt is None:
        return SalaryComparisonResult(
            status="unknown",
            reason=f"Cannot compare {job_period} pay against {user_period} minimum without stated working hours",
            original_amount=job_amt,
            original_currency=job_currency,
            original_period=job_period,
        )

    fx = get_dated_fx_rate(job_currency, user_currency or job_currency, static_rates=static_fx_rates)
    if fx is None:
        return SalaryComparisonResult(
            status="unknown",
            reason=f"No trustworthy FX rate configured to convert {job_currency} to {user_currency}",
            original_amount=job_amt,
            original_currency=job_currency,
            original_period=job_period,
        )

    normalized_amt = round(converted_period_amt * fx.rate, 2)
    meets = normalized_amt >= user_min
    return SalaryComparisonResult(
        status="meets_minimum" if meets else "below_minimum",
        reason=(
            f"Normalized estimate {normalized_amt} {fx.quote_currency}/{user_period} "
            f"(from {job_amt} {job_currency}/{job_period} @ {fx.rate:.4f} on {fx.rate_date})"
        ),
        original_amount=job_amt,
        original_currency=job_currency,
        original_period=job_period,
        normalized_amount=normalized_amt,
        normalized_currency=fx.quote_currency,
        normalized_period=user_period,
        fx_rate=fx.rate,
        fx_rate_date=fx.rate_date,
    )
