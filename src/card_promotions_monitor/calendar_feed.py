from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone


CALENDAR_EVENT_MINUTES = 15
CALENDAR_REMINDER_MINUTES = 10


def _escape_ics(value: object) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(",", "\\,")
        .replace(";", "\\;")
    )


def _utc_ics(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _stable_uid(activity_id: str, start: datetime, suffix: str = "") -> str:
    identity = f"{activity_id}|{start.isoformat()}|{suffix}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{digest}@card-promotion-radar"


def _fold_ics_line(line: str) -> str:
    """Fold content lines to the RFC 5545 limit without splitting UTF-8."""
    physical: list[str] = []
    current = ""
    for character in line:
        candidate = current + character
        if current and len(candidate.encode("utf-8")) > 75:
            physical.append(current)
            current = " " + character
        else:
            current = candidate
    physical.append(current)
    return "\r\n".join(physical)


def _consecutive_monthly(starts: list[datetime]) -> bool:
    if len(starts) < 2:
        return False
    first = starts[0]
    if any(
        (
            item.day,
            item.hour,
            item.minute,
            item.second,
            item.utcoffset(),
        )
        != (
            first.day,
            first.hour,
            first.minute,
            first.second,
            first.utcoffset(),
        )
        for item in starts[1:]
    ):
        return False
    for previous, current in zip(starts, starts[1:]):
        expected_year = previous.year + (1 if previous.month == 12 else 0)
        expected_month = 1 if previous.month == 12 else previous.month + 1
        if (current.year, current.month) != (expected_year, expected_month):
            return False
    return True


def _event_lines(
    activity: dict,
    start: datetime,
    generated_at: datetime,
    *,
    recurrence_count: int | None = None,
) -> list[str]:
    title = f"[登錄] {activity.get('bank_name', '')}｜{activity.get('title', '')}"
    official_url = str(activity.get("source_url") or "")
    registration_url = str(activity.get("registration_url") or official_url)
    timing_note = (
        f"每期需重新登錄；此循環包含 {recurrence_count} 個已解析的官方時點。"
        if recurrence_count
        else "登錄提醒事件固定為開始後 15 分鐘。"
    )
    description = (
        f"{timing_note}\n"
        f"官方活動頁：{official_url}\n"
        f"登錄入口：{registration_url}"
    )
    end = start + timedelta(minutes=CALENDAR_EVENT_MINUTES)
    lines = [
        "BEGIN:VEVENT",
        f"UID:{_stable_uid(str(activity.get('id') or ''), start, 'monthly' if recurrence_count else 'single')}",
        f"DTSTAMP:{_utc_ics(generated_at)}",
        f"DTSTART:{_utc_ics(start)}",
        f"DTEND:{_utc_ics(end)}",
        f"SUMMARY:{_escape_ics(title)}",
        f"DESCRIPTION:{_escape_ics(description)}",
        f"URL:{_escape_ics(registration_url)}",
    ]
    if recurrence_count:
        lines.append(f"RRULE:FREQ=MONTHLY;COUNT={recurrence_count}")
    lines.extend([
        "BEGIN:VALARM",
        f"TRIGGER:-PT{CALENDAR_REMINDER_MINUTES}M",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape_ics(title)}",
        "END:VALARM",
        "END:VEVENT",
    ])
    return lines


def build_registration_feed(payload: dict) -> str:
    """Build a stable subscription feed from confirmed official registration times."""
    generated_at = _parse_datetime(payload.get("generated_at")) or datetime.now(timezone.utc)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Card Promotion Radar//Registration Feed//ZH-TW",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:信用卡活動登錄提醒",
        "X-WR-TIMEZONE:Asia/Taipei",
        "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for activity in payload.get("activities", []):
        if not isinstance(activity, dict) or not activity.get("registration_required"):
            continue
        windows: list[tuple[dict, datetime, datetime | None]] = []
        for window in activity.get("registration_windows", []):
            if not isinstance(window, dict):
                continue
            start = _parse_datetime(window.get("start"))
            end = _parse_datetime(window.get("end"))
            if start:
                windows.append((window, start, end))
        windows.sort(key=lambda item: item[1])
        starts = [item[1] for item in windows]
        is_monthly = (
            "per_period_reregister"
            in activity.get("registration_timing_contracts", [])
            and _consecutive_monthly(starts)
        )
        if is_monthly:
            if starts[-1] >= generated_at:
                lines.extend(_event_lines(
                    activity,
                    starts[0],
                    generated_at,
                    recurrence_count=len(starts),
                ))
            continue
        for _, start, _ in windows:
            if start < generated_at:
                continue
            lines.extend(_event_lines(activity, start, generated_at))
    lines.extend(["END:VCALENDAR", ""])
    return "\r\n".join(_fold_ics_line(line) for line in lines)
