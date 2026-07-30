#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "docs" / "data" / "promotions.json"
DEFAULT_SITE_URL = "https://garychen-soc.github.io/taiwan-credit-card-promotions/"


def _agenda_lines(label: str, agenda: dict) -> list[str]:
    items = agenda.get("items", [])
    lines = [f"*{label}登錄｜{agenda.get('date', '—')}｜{len(items)} 個時點*"]
    if not items:
        return [*lines, "• 無已確認的登錄時點"]
    for item in items:
        start = datetime.fromisoformat(item["start"])
        lines.append(
            f"• {start:%H:%M}｜{item['bank_name']}｜{item['merchant']}"
        )
    return lines


def build_summary(data: dict, site_url: str) -> str:
    summary = data.get("summary", {})
    sources = data.get("source_health", [])
    agenda = data.get("registration_agenda", {})
    alerts = data.get("alerts", [])
    cache = data.get("cache", {})
    generated = datetime.fromisoformat(data["generated_at"])
    health = "、".join(
        f"{source['bank_name']} {source['status']}"
        for source in sources
    ) or "無來源狀態"

    lines = [
        f"*信用卡活動更新｜{generated:%Y-%m-%d %H:%M}*",
        (
            f"官方活動 {summary.get('total', 0)} 筆｜"
            f"需登錄 {summary.get('registration_required', 0)} 筆｜"
            f"高回饋 {summary.get('high_return', 0)} 筆"
        ),
        f"來源：{health}",
        (
            f"快取：沿用 {cache.get('reused_activities', 0)} 筆｜"
            f"省略明細讀取 {cache.get('detail_requests_avoided', 0)} 次｜"
            f"實際明細讀取 {cache.get('detail_requests_performed', 0)} 次"
        ),
        "",
        *_agenda_lines("今日", agenda.get("today", {})),
        "",
        *_agenda_lines("明日", agenda.get("tomorrow", {})),
    ]

    if alerts:
        lines.extend(["", f"*來源警示｜{len(alerts)} 則*"])
        for alert in alerts:
            detail = alert.get("message", "來源需檢查")
            new_url = alert.get("new_url")
            lines.append(
                f"• {alert.get('bank_name', '銀行')}｜{detail}"
                + (f"｜候選：{new_url}" if new_url else "")
            )

    lines.extend(["", f"<{site_url}|開啟刷卡活動登錄雷達>"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="輸出 Slack 可直接傳送的精簡更新摘要")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--site-url", default=DEFAULT_SITE_URL)
    args = parser.parse_args()
    data = json.loads(args.data.read_text(encoding="utf-8"))
    print(build_summary(data, args.site_url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
