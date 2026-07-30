from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache import activity_cache, new_cache_stats
from .extractors import extract_cathay, extract_ctbc, extract_dbs, extract_scsb, extract_sinopac


ROOT = Path(__file__).resolve().parents[2]


def load_json(path: Path):
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def build_payload(config: dict, now: datetime, previous_payload: dict | None = None) -> dict:
    thresholds = config["high_return"]
    cached_activities = activity_cache(previous_payload)
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
        else:
            raise ValueError(f"Unknown adapter: {adapter}")
        activities.extend(item.to_dict() for item in found)
        health.append(source_health.to_dict())
        alerts.extend(item.to_dict() for item in source_alerts)

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
        "schema_version": 2,
        "generated_at": now.replace(microsecond=0).isoformat(),
        "timezone": config["timezone"],
        "thresholds": thresholds,
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
    args = parser.parse_args(argv)

    config = load_json(args.config)
    previous_payload = None
    if args.output.exists():
        try:
            previous_payload = load_json(args.output)
        except (json.JSONDecodeError, OSError):
            previous_payload = None
    now = datetime.now(ZoneInfo(config.get("timezone", "Asia/Taipei")))
    payload = build_payload(config, now, previous_payload)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    args.report.write_text(encoded, encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if all(item["status"] != "failed" for item in payload["source_health"]) else 2


if __name__ == "__main__":
    sys.exit(main())
