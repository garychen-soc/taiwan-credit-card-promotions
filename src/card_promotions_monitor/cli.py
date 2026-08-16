from __future__ import annotations

import argparse
import fcntl
import json
import re
import sys
import tempfile
from collections import Counter
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from math import ceil
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache import activity_cache, bookkeeping_payload, new_cache_stats
from .extractors import (
    REGISTRATION_URL_DEFAULTS,
    _promotion_invariants,
    extract_cathay,
    extract_chb,
    extract_ctbc,
    extract_dbs,
    extract_esun,
    extract_first,
    extract_hncb,
    extract_kgi,
    extract_megabank,
    extract_obank,
    extract_scsb,
    extract_sinopac,
    extract_sunny,
    extract_taipei_fubon,
    extract_taishin,
    extract_tcbbank,
    extract_ubot,
    extract_yuanta,
    normalize_registration_url,
)
from .models import Promotion


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 7
PUBLISH_GUARD_EXIT_CODE = 4
UPDATE_ALREADY_RUNNING_EXIT_CODE = 3
DNS_FAILURE_MARKERS = (
    "nodename nor servname provided",
    "name or service not known",
    "temporary failure in name resolution",
    "could not resolve host",
    "getaddrinfo failed",
)


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@contextmanager
def update_lock(path: Path):
    """Prevent scheduled and manual refreshes from writing the same snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("promotion refresh already in progress") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_current_activity(item: dict, today: date) -> bool:
    end_text = str(item.get("end_date") or "")
    if end_text:
        try:
            if date.fromisoformat(end_text) < today:
                return False
        except ValueError:
            pass
    return True


def _lifecycle_for(item: dict, today: date) -> str:
    start_text = str(item.get("start_date") or "")
    end_text = str(item.get("end_date") or "")
    try:
        if start_text and date.fromisoformat(start_text) > today:
            return "upcoming"
        if end_text and date.fromisoformat(end_text) < today:
            return "ended"
    except ValueError:
        return str(item.get("lifecycle") or "active")
    return "active"


def retain_failed_source_activities(
    activities: list[dict],
    health: list[dict],
    previous_payload: dict | None,
    now: datetime,
    cache_stats: dict,
) -> None:
    """Keep still-current verified activities when an entire source is unavailable."""
    if not isinstance(previous_payload, dict):
        return
    failed_source_ids = {
        item.get("id")
        for item in health
        if item.get("status") == "failed" and int(item.get("activity_count") or 0) == 0
    }
    if not failed_source_ids:
        return

    existing_ids = {item.get("id") for item in activities}
    retained_by_source: dict[str, int] = {}
    for cached in previous_payload.get("activities", []):
        if not isinstance(cached, dict):
            continue
        source_id = cached.get("bank_id")
        if source_id not in failed_source_ids or cached.get("id") in existing_ids:
            continue
        if not _is_current_activity(cached, now.date()):
            continue
        retained = deepcopy(cached)
        retained["lifecycle"] = _lifecycle_for(retained, now.date())
        activities.append(retained)
        existing_ids.add(retained.get("id"))
        retained_by_source[source_id] = retained_by_source.get(source_id, 0) + 1

    retained_total = sum(retained_by_source.values())
    cache_stats["source_fallback_activities"] = retained_total
    for item in health:
        retained_count = retained_by_source.get(item.get("id"), 0)
        if not retained_count:
            continue
        item["retained_activity_count"] = retained_count
        suffix = f"沿用上一版 {retained_count} 筆仍在效期內的活動；未視為本次成功讀取。"
        item["message"] = f"{item.get('message', '').strip()} {suffix}".strip()


def assess_publish_guard(payload: dict, previous_payload: dict | None) -> dict:
    health = payload.get("source_health", [])
    source_total = len(health)
    source_failed = sum(1 for item in health if item.get("status") == "failed")
    dns_failures = sum(
        1
        for item in health
        if any(
            marker in str(item.get("message") or "").lower()
            for marker in DNS_FAILURE_MARKERS
        )
    )
    candidate_fallback = int(
        payload.get("cache", {}).get("source_fallback_activities") or 0
    )
    candidate_active = max(
        0,
        int(payload.get("summary", {}).get("active_or_upcoming") or 0)
        - candidate_fallback,
    )
    previous_active = 0
    if isinstance(previous_payload, dict):
        previous_fallback = int(
            previous_payload.get("cache", {}).get("source_fallback_activities") or 0
        )
        previous_active = max(
            0,
            int(previous_payload.get("summary", {}).get("active_or_upcoming") or 0)
            - previous_fallback,
        )
    drop_ratio = (
        max(0.0, 1 - (candidate_active / previous_active))
        if previous_active
        else 0.0
    )

    previous_source_counts = {
        str(item.get("id")): int(item.get("activity_count") or 0)
        for item in (
            previous_payload.get("source_health", [])
            if isinstance(previous_payload, dict)
            else []
        )
        if isinstance(item, dict) and item.get("id")
    }
    has_source_activity_regression = any(
        previous_count > 0
        and (1 - (int(item.get("activity_count") or 0) / previous_count)) > 0.4
        for item in health
        if (previous_count := previous_source_counts.get(str(item.get("id"))))
        is not None
    )

    reason_codes: list[str] = []
    if source_total and dns_failures >= max(3, ceil(source_total * 0.5)):
        reason_codes.append("systemic_dns_failure")
    if source_total and source_failed >= ceil(source_total * 0.8):
        reason_codes.append("catastrophic_source_failure")
    if previous_active and source_failed >= 2 and drop_ratio >= 0.5:
        reason_codes.append("catastrophic_activity_regression")
    if has_source_activity_regression:
        reason_codes.append("source_activity_regression")

    blocked = bool(reason_codes)
    return {
        "status": "blocked" if blocked else "passed",
        "blocked": blocked,
        "reason_codes": reason_codes,
        "source_total": source_total,
        "source_failed": source_failed,
        "dns_failures": dns_failures,
        "candidate_active_or_upcoming": candidate_active,
        "previous_active_or_upcoming": previous_active,
        "activity_drop_percent": round(drop_ratio * 100, 1),
        "published_snapshot_preserved": blocked and isinstance(previous_payload, dict),
    }


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(encoded)
            temporary_path = Path(handle.name)
        temporary_path.replace(path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


INTERNAL_ACTIVITY_FIELDS = {
    "source_entry_url",
    "source_fingerprint",
    "observed_at",
    "last_detail_checked_at",
    "official_status",
}
DERIVED_ACTIVITY_FIELDS = {"lifecycle", "high_return"}
DETAIL_ACTIVITY_FIELDS = {"registration_text", "terms_raw", "terms_sections"}


def _artifact_filename(activity_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", activity_id):
        raise ValueError(f"Unsafe activity id for public artifact: {activity_id}")
    return f"{activity_id}.json"


def _lightweight_activity(activity: dict, detail_ref: str = "") -> dict:
    value = deepcopy(activity)
    for field_name in (
        INTERNAL_ACTIVITY_FIELDS
        | DERIVED_ACTIVITY_FIELDS
        | DETAIL_ACTIVITY_FIELDS
    ):
        value.pop(field_name, None)
    if detail_ref:
        value["detail_ref"] = detail_ref
    return value


def _write_public_artifacts(payload: dict, output_path: Path) -> dict:
    """Write a small registration index plus lazy bank and detail shards."""
    data_root = output_path.parent
    bank_root = data_root / "banks"
    detail_root = data_root / "activities"
    generated_at = str(payload.get("generated_at") or "")
    schema_version = int(payload.get("schema_version") or SCHEMA_VERSION)
    sources = {
        str(item.get("id")): item
        for item in payload.get("sources", [])
        if isinstance(item, dict) and item.get("id")
    }
    activities_by_bank: dict[str, list[dict]] = {}
    detail_names: set[str] = set()
    lightweight: list[dict] = []

    for activity in payload.get("activities", []):
        if not isinstance(activity, dict):
            continue
        activity_id = str(activity.get("id") or "")
        if not activity_id:
            continue
        filename = _artifact_filename(activity_id)
        has_detail = any(activity.get(field) for field in DETAIL_ACTIVITY_FIELDS)
        detail_ref = f"activities/{filename}" if has_detail else ""
        light = _lightweight_activity(activity, detail_ref)
        lightweight.append(light)
        bank_id = str(light.get("bank_id") or "unknown")
        activities_by_bank.setdefault(bank_id, []).append(light)
        if not has_detail:
            continue
        detail_names.add(filename)
        write_json_atomic(detail_root / filename, {
            "schema_version": schema_version,
            "generated_at": generated_at,
            "activity_id": activity_id,
            "bank_id": activity.get("bank_id", ""),
            "title": activity.get("title", ""),
            "source_url": activity.get("source_url", ""),
            "registration_text": activity.get("registration_text", ""),
            "terms_raw": activity.get("terms_raw", ""),
            "terms_sections": activity.get("terms_sections", {}),
        })

    bank_files: dict[str, str] = {}
    bank_names: set[str] = set()
    for bank_id, activities in sorted(activities_by_bank.items()):
        filename = f"{bank_id}.json"
        bank_files[bank_id] = f"banks/{filename}"
        bank_names.add(filename)
        source = sources.get(bank_id, {})
        write_json_atomic(bank_root / filename, {
            "schema_version": schema_version,
            "generated_at": generated_at,
            "bank_id": bank_id,
            "bank_name": source.get("bank_name")
            or (activities[0].get("bank_name") if activities else ""),
            "activity_count": len(activities),
            "activities": activities,
        })

    public_payload = deepcopy(payload)
    public_payload["activities"] = [
        activity for activity in lightweight
        if activity.get("registration_required")
    ]
    public_payload["catalog"] = {
        "default_filter": "registration",
        "activity_count": len(lightweight),
        "registration_index_count": len(public_payload["activities"]),
        "bank_files": bank_files,
        "categories": sorted({
            category
            for activity in lightweight
            for category in activity.get("categories", [])
            if isinstance(category, str) and category
        }),
    }
    write_json_atomic(output_path, public_payload)

    for directory, expected in ((bank_root, bank_names), (detail_root, detail_names)):
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            if path.name not in expected:
                path.unlink()
    return public_payload


def load_previous_public_payload(output_path: Path) -> dict | None:
    """Rehydrate a layered public snapshot for cache reuse and guard comparison."""
    if not output_path.exists():
        return None
    try:
        payload = load_json(output_path)
    except (json.JSONDecodeError, OSError):
        return None
    catalog = payload.get("catalog")
    if not isinstance(catalog, dict) or not isinstance(catalog.get("bank_files"), dict):
        return payload

    activities: list[dict] = []
    for reference in catalog["bank_files"].values():
        if not isinstance(reference, str):
            continue
        bank_path = output_path.parent / reference
        try:
            bank_payload = load_json(bank_path)
        except (json.JSONDecodeError, OSError):
            continue
        for light in bank_payload.get("activities", []):
            if not isinstance(light, dict):
                continue
            activity = dict(light)
            detail_ref = activity.pop("detail_ref", "")
            if isinstance(detail_ref, str) and detail_ref:
                try:
                    detail = load_json(output_path.parent / detail_ref)
                except (json.JSONDecodeError, OSError):
                    detail = {}
                for field_name in DETAIL_ACTIVITY_FIELDS:
                    if field_name in detail:
                        activity[field_name] = detail[field_name]
            activities.append(activity)
    if activities:
        payload["activities"] = activities
    return payload


def classify_registration_urls(activities: list[dict]) -> None:
    counts = Counter(
        (item.get("bank_id", ""), item.get("registration_url", ""))
        for item in activities
        if item.get("registration_required") and item.get("registration_url")
    )
    for activity in activities:
        url = activity.get("registration_url", "")
        if not activity.get("registration_required") or not url:
            activity["registration_url_kind"] = "unknown"
        elif url in REGISTRATION_URL_DEFAULTS.values() or counts[(activity.get("bank_id", ""), url)] > 1:
            activity["registration_url_kind"] = "bank_portal"
        elif url != activity.get("source_url"):
            activity["registration_url_kind"] = "activity_specific"
        else:
            activity["registration_url_kind"] = "unknown"


def persist_payload(
    payload: dict,
    previous_payload: dict | None,
    output_path: Path,
    report_path: Path,
    cache_path: Path | None = None,
) -> int:
    guard = assess_publish_guard(payload, previous_payload)
    payload["publish_guard"] = guard
    write_json_atomic(report_path, payload)
    if guard["blocked"]:
        print(json.dumps({"summary": payload["summary"], "publish_guard": guard}, ensure_ascii=False))
        return PUBLISH_GUARD_EXIT_CODE
    _write_public_artifacts(payload, output_path)
    if cache_path is not None:
        write_json_atomic(
            cache_path,
            bookkeeping_payload(payload.get("activities", []), payload.get("generated_at", "")),
        )
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if all(item["status"] != "failed" for item in payload["source_health"]) else 2


def build_payload(
    config: dict,
    now: datetime,
    previous_payload: dict | None = None,
    cache_ledger: dict | None = None,
) -> dict:
    thresholds = config["high_return"]
    previous_schema = (
        int(previous_payload.get("schema_version") or 0)
        if isinstance(previous_payload, dict)
        else 0
    )
    cached_activities = (
        activity_cache(previous_payload, cache_ledger)
        if previous_schema >= 5
        else {}
    )
    cache_stats = new_cache_stats(
        previous_payload.get("generated_at", "")
        if isinstance(previous_payload, dict)
        else ""
    )
    activities = []
    health = []
    alerts = []
    for source in config["sources"]:
        adapter = source["adapter"]
        kwargs = {
            "now": now,
            "percent_threshold": float(thresholds["percent_at_least"]),
            "amount_threshold": int(thresholds["amount_twd_at_least"]),
            "activity_cache": cached_activities,
            "cache_stats": cache_stats,
        }
        if adapter == "dbs_shopping":
            found, source_health, source_alerts = extract_dbs(source, **kwargs)
        elif adapter == "cathay_online_shopping":
            found, source_health, source_alerts = extract_cathay(source, **kwargs)
        elif adapter == "ctbc_linepay":
            found, source_health, source_alerts = extract_ctbc(source, **kwargs)
        elif adapter == "sinopac_discounts":
            found, source_health, source_alerts = extract_sinopac(source, **kwargs)
        elif adapter == "scsb_hotlist":
            found, source_health, source_alerts = extract_scsb(source, **kwargs)
        elif adapter == "obank_debit_card":
            found, source_health, source_alerts = extract_obank(source, **kwargs)
        elif adapter == "yuanta_promotions":
            found, source_health, source_alerts = extract_yuanta(source, **kwargs)
        elif adapter == "esun_discounts":
            found, source_health, source_alerts = extract_esun(source, **kwargs)
        elif adapter == "sunny_card_promotions":
            found, source_health, source_alerts = extract_sunny(source, **kwargs)
        elif adapter == "tcbbank_card_promotions":
            found, source_health, source_alerts = extract_tcbbank(source, **kwargs)
        elif adapter == "kgi_card_campaigns":
            found, source_health, source_alerts = extract_kgi(source, **kwargs)
        elif adapter == "hncb_credit_card_tab":
            found, source_health, source_alerts = extract_hncb(source, **kwargs)
        elif adapter == "taipei_fubon_wicket_promotions":
            found, source_health, source_alerts = extract_taipei_fubon(source, **kwargs)
        elif adapter == "taishin_offer_categories":
            found, source_health, source_alerts = extract_taishin(source, **kwargs)
        elif adapter == "firstbank_touch_promotions":
            found, source_health, source_alerts = extract_first(source, **kwargs)
        elif adapter == "chb_credit_card_categories":
            found, source_health, source_alerts = extract_chb(source, **kwargs)
        elif adapter == "ubot_reward_categories":
            found, source_health, source_alerts = extract_ubot(source, **kwargs)
        elif adapter == "megabank_registration_promotions":
            found, source_health, source_alerts = extract_megabank(source, **kwargs)
        else:
            raise ValueError(f"Unknown adapter: {adapter}")
        activities.extend(item.to_dict() for item in found)
        health.append(source_health.to_dict())
        alerts.extend(item.to_dict() for item in source_alerts)

    retain_failed_source_activities(
        activities,
        health,
        previous_payload,
        now,
        cache_stats,
    )
    for activity in activities:
        promotion = Promotion.from_dict(activity)
        _promotion_invariants(promotion)
        activity.update(promotion.to_dict())
        if activity.get("registration_required"):
            activity["registration_url"] = normalize_registration_url(
                str(activity.get("bank_id") or ""),
                str(activity.get("registration_url") or ""),
            )
        else:
            activity["registration_url"] = ""

    classify_registration_urls(activities)

    activities.sort(
        key=lambda item: (
            0 if item["registration_required"] else 1,
            0 if item["lifecycle"] == "upcoming" else 1,
            item["end_date"] or "9999-12-31",
            item["bank_name"],
            item["title"],
        )
    )
    active = [item for item in activities if item["lifecycle"] in {"active", "upcoming"}]
    today = now.date()
    tomorrow = today + timedelta(days=1)

    def agenda_for(day: date) -> list[dict]:
        prefix = day.isoformat()
        values: list[dict] = []
        for item in active:
            for window in item["registration_windows"]:
                if not window["start"].startswith(prefix):
                    continue
                values.append({
                    "activity_id": item["id"],
                    "bank_name": item["bank_name"],
                    "title": item["title"],
                    "merchant": item["merchant"],
                    "start": window["start"],
                    "end": window["end"],
                    "label": window["label"],
                    "registration_url": item["registration_url"],
                    "source_url": item["source_url"],
                })
        return sorted(values, key=lambda item: (item["start"], item["bank_name"], item["title"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "timezone": config["timezone"],
        "thresholds": thresholds,
        "sources": [
            {
                "id": source["id"],
                "bank_name": source["bank_name"],
                "source_entry_url": source["entry_url"],
            }
            for source in config["sources"]
        ],
        "cache": cache_stats,
        "summary": {
            "total": len(activities),
            "active_or_upcoming": len(active),
            "registration_required": sum(1 for item in active if item["registration_required"]),
            "registration_times_confirmed": sum(
                1 for item in active if item["registration_windows"]
            ),
            "high_return": sum(1 for item in active if item["high_return"]),
            "alerts": len(alerts),
        },
        "source_health": health,
        "alerts": alerts,
        "registration_agenda": {
            "today": {
                "date": today.isoformat(),
                "items": agenda_for(today),
            },
            "tomorrow": {
                "date": tomorrow.isoformat(),
                "items": agenda_for(tomorrow),
            },
        },
        "activities": activities,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refresh official credit-card promotions.")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "sources.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "data" / "promotions.json")
    parser.add_argument("--report", type=Path, default=ROOT / "reports" / "latest.json")
    parser.add_argument("--cache", type=Path, default=ROOT / "data" / "activity_cache.json")
    parser.add_argument("--lock", type=Path, default=ROOT / "reports" / "update.lock")
    args = parser.parse_args(argv)

    try:
        with update_lock(args.lock):
            config = load_json(args.config)
            previous_payload = load_previous_public_payload(args.output)
            cache_ledger = None
            if args.cache.exists():
                try:
                    cache_ledger = load_json(args.cache)
                except (json.JSONDecodeError, OSError):
                    cache_ledger = None
            now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Taipei")))
            payload = build_payload(config, now, previous_payload, cache_ledger)
            return persist_payload(
                payload, previous_payload, args.output, args.report, args.cache,
            )
    except RuntimeError as exc:
        if str(exc) != "promotion refresh already in progress":
            raise
        print(json.dumps({
            "status": "skipped",
            "reason": "update_already_running",
            "message": "已有信用卡活動更新正在執行，本次未讀取或寫入資料。",
        }, ensure_ascii=False))
        return UPDATE_ALREADY_RUNNING_EXIT_CODE


if __name__ == "__main__":
    sys.exit(main())
