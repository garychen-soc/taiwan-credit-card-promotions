from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any

from .models import Promotion


CACHE_SCHEMA_VERSION = 2
CACHE_MAX_AGE_DAYS = 30
CACHE_BOUNDARY_REFRESH_DAYS = 3


def activity_cache(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    activities = payload.get("activities", [])
    if not isinstance(activities, list):
        return {}
    return {
        item["id"]: item
        for item in activities
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def new_cache_stats(previous_generated_at: str = "") -> dict[str, Any]:
    return {
        "strategy": "listing_fingerprint",
        "schema_version": CACHE_SCHEMA_VERSION,
        "max_age_days": CACHE_MAX_AGE_DAYS,
        "previous_generated_at": previous_generated_at,
        "reused_activities": 0,
        "cache_misses": 0,
        "detail_requests_avoided": 0,
        "detail_requests_performed": 0,
    }


def source_fingerprint(bank_id: str, value: Any) -> str:
    encoded = json.dumps(
        {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "bank_id": bank_id,
            "value": value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_detail_requests(stats: dict[str, Any], count: int = 1) -> None:
    stats["detail_requests_performed"] = int(stats.get("detail_requests_performed", 0)) + count


def _date_value(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _near_activity_boundary(item: dict[str, Any], today: date) -> bool:
    margin = timedelta(days=CACHE_BOUNDARY_REFRESH_DAYS)
    for key in ("start_date", "end_date"):
        value = _date_value(item.get(key))
        if value is not None and abs(value - today) <= margin:
            return True
    return False


def reuse_cached_promotion(
    cache: dict[str, dict[str, Any]],
    *,
    activity_id: str,
    fingerprint: str,
    now: datetime,
    source_entry_url: str,
    percent_threshold: float,
    amount_threshold: int,
    stats: dict[str, Any],
    avoids_detail_request: bool,
) -> Promotion | None:
    cached = cache.get(activity_id)
    if not cached or cached.get("source_fingerprint") != fingerprint:
        stats["cache_misses"] = int(stats.get("cache_misses", 0)) + 1
        return None

    checked_text = cached.get("last_detail_checked_at")
    try:
        checked_at = datetime.fromisoformat(checked_text) if isinstance(checked_text, str) else None
    except ValueError:
        checked_at = None
    if checked_at is None or now - checked_at > timedelta(days=CACHE_MAX_AGE_DAYS):
        stats["cache_misses"] = int(stats.get("cache_misses", 0)) + 1
        return None
    if _near_activity_boundary(cached, now.date()):
        stats["cache_misses"] = int(stats.get("cache_misses", 0)) + 1
        return None

    promotion = Promotion.from_dict(cached)
    promotion.observed_at = now.replace(microsecond=0).isoformat()
    promotion.source_entry_url = source_entry_url
    promotion.high_return = bool(
        (
            promotion.max_reward_percent is not None
            and promotion.max_reward_percent >= percent_threshold
        )
        or (
            promotion.max_reward_amount_twd is not None
            and promotion.max_reward_amount_twd >= amount_threshold
        )
    )
    promotion.featured = (
        promotion.featured
        or promotion.registration_required
        or promotion.high_return
    )
    start = _date_value(promotion.start_date) or now.date()
    end = _date_value(promotion.end_date)
    if promotion.official_status == "ended_by_official" or (end and end < now.date()):
        promotion.lifecycle = "ended"
    elif start > now.date():
        promotion.lifecycle = "upcoming"
    else:
        promotion.lifecycle = "active"

    stats["reused_activities"] = int(stats.get("reused_activities", 0)) + 1
    if avoids_detail_request:
        stats["detail_requests_avoided"] = int(stats.get("detail_requests_avoided", 0)) + 1
    return promotion
