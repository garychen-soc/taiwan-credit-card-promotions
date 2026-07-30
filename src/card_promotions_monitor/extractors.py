from __future__ import annotations

import calendar
import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .fetch import fetch_json, fetch_text
from .html_tools import clean_inline, parse_page, strip_html, walk_strings
from .models import Alert, Promotion, RegistrationWindow, SourceHealth


TAIPEI = ZoneInfo("Asia/Taipei")
ONLINE_TAG = "cub-tags:camapigns/credit-card/online-shopping"
REGISTRATION_URL_DEFAULTS = {
    "dbs": "https://www.dbs.com.tw/personal-zh/digital-service/cardplus/deeplink/index.html?SERVICE_ID=camp_registration&source=pweb",
    "cathay": "https://www.cathaybk.com.tw/promotion/",
    "ctbc": "https://www.ctbcbank.com/twrbo/zh_tw/onlinecounter_index/cc_service/cc_service_register.html",
    "sinopac": "https://bank.sinopac.com/sinopacBT/personal/credit-card/discount/list.html",
    "scsb": "https://ebank.scsb.com.tw/ternal/ofwbcv05/page#/ofwb/cv/05/01",
}
MONTHS = {
    "1月": 1, "2月": 2, "3月": 3, "4月": 4, "5月": 5, "6月": 6,
    "7月": 7, "8月": 8, "9月": 9, "10月": 10, "11月": 11, "12月": 12,
}


def _now_iso(now: datetime) -> str:
    return now.astimezone(TAIPEI).replace(microsecond=0).isoformat()


def _stable_id(bank_id: str, url: str) -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{bank_id}-{digest}"


def _date_from_iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(TAIPEI).date()
    except ValueError:
        return None


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _lifecycle(start: date, end: date | None, today: date, *, ended: bool = False) -> str:
    if ended or (end and end < today):
        return "ended"
    if start > today:
        return "upcoming"
    return "active"


def _categories(text: str, base: str | None = "網購") -> list[str]:
    values = [base] if base else []
    rules = (
        ("網購", ("網購", "購物網", "線上商城", "電視購物", "電商", "蝦皮", "momo", "PChome")),
        ("行動支付", ("錢包", "LINE Pay", "OPEN POINT", "支付", "Apple Pay", "Google Pay", "台灣Pay", "全支付")),
        ("百貨購物", ("百貨", "3C", "全國電子", "燦坤", "家電")),
        ("生活消費", ("星巴克", "屈臣氏", "全家", "超商")),
        ("旅遊交通", ("旅遊", "交通", "外送")),
        ("餐飲美食", ("餐廳", "美食", "咖啡", "漢堡", "壽司", "foodpanda", "Uber Eats")),
        ("加油交通", ("加油", "捷運", "停車", "高鐵")),
    )
    for label, words in rules:
        if any(word.lower() in text.lower() for word in words):
            values.append(label)
    if "分期" in text:
        values.append("分期")
    return list(dict.fromkeys(values or ["其他優惠"]))


def _reward_values(text: str) -> tuple[float | None, int | None]:
    percents = [
        float(value)
        for value in re.findall(r"(?:最高(?:享)?|加碼)?\s*(\d{1,2}(?:\.\d+)?)\s*%", text)
        if float(value) <= 100
    ]
    amount_patterns = (
        r"(?:最高(?:享|贈|回饋)?|贈)\s*(?:NT\$|\$)\s*([\d,]+)\s*(?:元)?(?:刷卡金|現折|回饋)?",
        r"(?:最高(?:享|贈|回饋)?|贈)\s*([\d,]+)\s*元(?:刷卡金|現折|回饋)",
        r"(?:刷卡金|現折)\s*(?:NT\$|\$)?\s*([\d,]+)",
    )
    amounts: list[int] = []
    for pattern in amount_patterns:
        amounts.extend(int(value.replace(",", "")) for value in re.findall(pattern, text, flags=re.I))
    return (max(percents) if percents else None, max(amounts) if amounts else None)


def _time_parts(hour_text: str, minute_text: str | None, marker: str | None) -> tuple[int, int]:
    hour = int(hour_text)
    minute = int(minute_text or 0)
    if marker == "下午" and hour < 12:
        hour += 12
    if marker == "上午" and hour == 12:
        hour = 0
    return hour, minute


def _registration_windows(
    text: str,
    activity_year: int,
    activity_start: date | None = None,
    activity_end: date | None = None,
) -> list[RegistrationWindow]:
    normalized = clean_inline(text.replace("～", "~").replace("至", "~"))
    windows: list[RegistrationWindow] = []
    seen: set[tuple[str, str]] = set()
    range_spans: list[tuple[int, int]] = []

    range_pattern = re.compile(
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})\s*(上午|下午)?\s*(\d{1,2})[:點](\d{2})?"
        r"\s*~\s*(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})\s*(上午|下午)?\s*(\d{1,2})[:點](\d{2})?"
    )
    for match in range_pattern.finditer(normalized):
        before = normalized[max(0, match.start() - 80): match.start()]
        after = normalized[match.end(): min(len(normalized), match.end() + 100)]
        context = f"{before}{match.group(0)}{after}"
        direct_marker = any(
            marker in context
            for marker in ("登錄期間", "開放登錄", "完成活動登錄", "登錄時間", "波登錄", "檔登錄")
        )
        if not direct_marker and "登錄" not in before[-30:]:
            continue
        sy = int(match.group(1) or activity_year)
        sh, sm = _time_parts(match.group(5), match.group(6), match.group(4))
        ey = int(match.group(7) or sy)
        eh, em = _time_parts(match.group(11), match.group(12), match.group(10))
        try:
            start = datetime(sy, int(match.group(2)), int(match.group(3)), sh, sm, tzinfo=TAIPEI)
            end = datetime(ey, int(match.group(8)), int(match.group(9)), eh, em, tzinfo=TAIPEI)
        except ValueError:
            continue
        key = (start.isoformat(), end.isoformat())
        if end < start:
            continue
        range_spans.append(match.span())
        if key in seen:
            continue
        seen.add(key)
        excerpt = context
        windows.append(RegistrationWindow(*key, "活動登錄期間", excerpt))

    same_day_range_pattern = re.compile(
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})\s*(上午|下午)?\s*(\d{1,2})[:點](\d{2})?"
        r"\s*~\s*(上午|下午)?\s*(\d{1,2})[:點](\d{2})?"
    )
    for match in same_day_range_pattern.finditer(normalized):
        context = normalized[max(0, match.start() - 70): min(len(normalized), match.end() + 90)]
        if not any(marker in context for marker in ("開放登錄", "登錄期間", "完成活動登錄", "波登錄", "檔登錄")):
            continue
        year = int(match.group(1) or activity_year)
        start_hour, start_minute = _time_parts(match.group(5), match.group(6), match.group(4))
        end_hour, end_minute = _time_parts(match.group(8), match.group(9), match.group(7))
        try:
            start = datetime(year, int(match.group(2)), int(match.group(3)), start_hour, start_minute, tzinfo=TAIPEI)
            end = datetime(year, int(match.group(2)), int(match.group(3)), end_hour, end_minute, tzinfo=TAIPEI)
        except ValueError:
            continue
        key = (start.isoformat(), end.isoformat())
        if end < start:
            continue
        range_spans.append(match.span())
        if key in seen:
            continue
        seen.add(key)
        windows.append(RegistrationWindow(*key, "活動登錄期間", context))

    point_pattern = re.compile(
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})(?:\([^)]+\))?\s*"
        r"(上午|下午)?\s*(\d{1,2})(?::|點(?:整)?)\s*(\d{2})?"
    )
    for match in point_pattern.finditer(normalized):
        if any(start <= match.start() < end for start, end in range_spans):
            continue
        context = normalized[max(0, match.start() - 90): min(len(normalized), match.end() + 110)]
        if not any(marker in context for marker in ("開始登錄", "開放登錄", "活動登錄日期")):
            continue
        year = int(match.group(1) or activity_year)
        hour, minute = _time_parts(match.group(5), match.group(6), match.group(4))
        try:
            start = datetime(year, int(match.group(2)), int(match.group(3)), hour, minute, tzinfo=TAIPEI)
        except ValueError:
            continue
        end = start + timedelta(minutes=30)
        key = (start.isoformat(), end.isoformat())
        if key in seen:
            continue
        seen.add(key)
        windows.append(RegistrationWindow(*key, "登錄開放", context))

    month_pattern = re.compile(
        r"(1[0-2]月|[1-9]月)活動.{0,15}?(\d{1,2})/(\d{1,2})(?:\([^)]+\))?\s*"
        r"(上午|下午)?\s*(\d{1,2})(?::|點(?:整)?)\s*(\d{2})?.{0,12}?開始登錄"
    )
    for match in month_pattern.finditer(normalized):
        year = activity_year
        hour, minute = _time_parts(match.group(5), match.group(6), match.group(4))
        try:
            start = datetime(year, int(match.group(2)), int(match.group(3)), hour, minute, tzinfo=TAIPEI)
        except ValueError:
            continue
        end = start + timedelta(minutes=30)
        key = (start.isoformat(), end.isoformat())
        if key in seen:
            continue
        seen.add(key)
        windows.append(RegistrationWindow(*key, f"{match.group(1)}登錄開放", match.group(0)))

    period_start = activity_start or date(activity_year, 1, 1)
    period_end = activity_end or date(activity_year, 12, 31)

    def add_recurring(day: date, hour: int, minute: int, label: str, source_text: str) -> None:
        if day < period_start or day > period_end:
            return
        start = datetime.combine(day, time(hour, minute), tzinfo=TAIPEI)
        end = start + timedelta(minutes=30)
        key = (start.isoformat(), end.isoformat())
        if key in seen:
            return
        seen.add(key)
        windows.append(RegistrationWindow(*key, label, source_text))

    monthly_day_pattern = re.compile(
        r"每月\s*(\d{1,2})\s*[號日]\s*(上午|下午)?\s*(\d{1,2})"
        r"(?::(\d{2})|[點時](?:整)?)\s*(?:起)?(?:開放|開始)?登錄"
    )
    for match in monthly_day_pattern.finditer(normalized):
        day_of_month = int(match.group(1))
        hour, minute = _time_parts(match.group(3), match.group(4), match.group(2))
        year, month = period_start.year, period_start.month
        while (year, month) <= (period_end.year, period_end.month):
            try:
                recurring_day = date(year, month, day_of_month)
            except ValueError:
                recurring_day = None
            if recurring_day:
                add_recurring(recurring_day, hour, minute, "每月登錄開放", match.group(0))
            month += 1
            if month == 13:
                year += 1
                month = 1

    chinese_number = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    weekday_number = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
    nth_weekday_pattern = re.compile(
        r"每月第([一二三四五])(?:個|周)?(?:星期|週)([一二三四五六日天])"
        r"[」』】)\]）]?\s*"
        r"(上午|下午)?\s*(\d{1,2})(?::(\d{2})|[點時](?:整)?)\s*"
        r"(?:起)?(?:開放|開始)?登錄"
    )
    for match in nth_weekday_pattern.finditer(normalized):
        occurrence = chinese_number[match.group(1)]
        weekday = weekday_number[match.group(2)]
        hour, minute = _time_parts(match.group(4), match.group(5), match.group(3))
        year, month = period_start.year, period_start.month
        while (year, month) <= (period_end.year, period_end.month):
            first_weekday, days_in_month = calendar.monthrange(year, month)
            day_of_month = 1 + ((weekday - first_weekday) % 7) + (occurrence - 1) * 7
            if day_of_month <= days_in_month:
                add_recurring(
                    date(year, month, day_of_month),
                    hour,
                    minute,
                    "每月登錄開放",
                    match.group(0),
                )
            month += 1
            if month == 13:
                year += 1
                month = 1

    return sorted(windows, key=lambda item: item.start)


def _registration_excerpt(text: str) -> str:
    lines = [clean_inline(line)[:900] for line in text.splitlines() if "登錄" in line]
    useful = [
        line
        for line in lines
        if any(
            token in line
            for token in (
                "/",
                "開始登錄",
                "開放登錄",
                "登錄期間",
                "活動登錄",
                "完成登錄",
                "需登錄",
                "須登錄",
                "無需登錄",
                "不需登錄",
                "免登錄",
            )
        )
    ]
    timing_tokens = ("/", "每月", "開始登錄", "開放登錄", "登錄期間")
    prioritized = [
        *[line for line in useful if any(token in line for token in timing_tokens)],
        *[line for line in useful if not any(token in line for token in timing_tokens)],
    ]
    return " ".join(dict.fromkeys(prioritized))[:2400]


def _registration_url(links: list[dict[str, str]], bank_id: str) -> str:
    for link in links:
        text = link.get("text", "")
        url = link.get("url", "")
        if "登錄" in text and url.startswith("https://"):
            return url
    return REGISTRATION_URL_DEFAULTS[bank_id]


def _discover_replacement(
    source: dict[str, Any],
    *,
    keywords: tuple[str, ...],
) -> str:
    for discovery_url in source.get("discovery_urls", []):
        try:
            result = fetch_text(discovery_url, source["official_domains"])
        except RuntimeError:
            continue
        page = parse_page(result.text, result.final_url)
        candidates = []
        for link in page.links:
            haystack = f"{link['text']} {link['url']}".lower()
            score = sum(1 for word in keywords if word.lower() in haystack)
            if score and link["url"].startswith("https://"):
                candidates.append((score, link["url"]))
        if candidates:
            return sorted(candidates, reverse=True)[0][1]
    return ""


def extract_dbs(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    alerts: list[Alert] = []
    shopping_url = source["shopping_url"]
    try:
        entry = fetch_text(source["entry_url"], source["official_domains"])
        entry_page = parse_page(entry.text, entry.final_url)
        linked_shopping = next(
            (
                link["url"] for link in entry_page.links
                if "/personal-zh/cards/offers/ce/default.html" in link["url"]
            ),
            "",
        )
        if linked_shopping:
            shopping_url = linked_shopping
    except RuntimeError:
        replacement = _discover_replacement(source, keywords=("信用卡優惠", "刷卡優惠", "cards-offers"))
        if replacement:
            alerts.append(Alert(
                "source_relocated",
                source["bank_name"],
                "使用者指定的活動入口無法讀取，已從銀行官方網站找到候選替代網址。",
                source["entry_url"],
                replacement,
            ))
        else:
            alerts.append(Alert(
                "source_failed",
                source["bank_name"],
                "使用者指定的活動入口無法讀取，尚未找到替代網址；暫以已知官方網購入口更新。",
                source["entry_url"],
            ))

    try:
        listing = fetch_text(shopping_url, source["official_domains"])
    except RuntimeError as exc:
        replacement = _discover_replacement(source, keywords=("網購", "線上購物", "/ce/", "shopping"))
        if not replacement:
            return [], SourceHealth(
                source["id"], source["bank_name"], shopping_url, "", "failed", 0, checked_at, str(exc)
            ), [Alert("source_failed", source["bank_name"], "指定網購入口無法讀取，且未找到替代網址。", shopping_url)]
        alerts.append(Alert(
            "source_relocated",
            source["bank_name"],
            "指定網購入口無法讀取，已從銀行官方網站找到候選替代網址。",
            shopping_url,
            replacement,
        ))
        shopping_url = replacement
        listing = fetch_text(shopping_url, source["official_domains"])

    listing_page = parse_page(listing.text, listing.final_url)
    merchant_links: list[tuple[str, str]] = []
    for link in listing_page.links:
        path = urlsplit(link["url"]).path
        if re.search(r"/mall_[0-9_]+\.html$", path):
            merchant = clean_inline(link["text"])
            if link["url"] not in {url for _, url in merchant_links}:
                merchant_links.append((merchant, link["url"]))

    activities: list[Promotion] = []
    failed_details = 0
    for merchant, url in merchant_links:
        try:
            detail = fetch_text(url, source["official_domains"])
        except RuntimeError:
            failed_details += 1
            continue
        page = parse_page(detail.text, detail.final_url)
        text = page.text
        period = re.search(
            r"活動期間[：:\s]*(?:\n)?\s*(20\d{2})/(\d{1,2})/(\d{1,2})\s*[~～－-]\s*"
            r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})",
            text,
        )
        if period:
            start = date(int(period.group(1)), int(period.group(2)), int(period.group(3)))
            end = date(int(period.group(4) or period.group(1)), int(period.group(5)), int(period.group(6)))
        else:
            start = now.astimezone(TAIPEI).date()
            end = None
        title = page.title.split("|", 1)[0].strip() or f"{merchant} 星展信用卡活動"
        summary = page.headings[:700] or title
        registration_text = _registration_excerpt(text)
        registration_required = "登錄" in text and "全部無需登錄" not in text
        windows = _registration_windows(registration_text or text, start.year, start, end)
        reward_percent, reward_amount = _reward_values(f"{title} {summary}")
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        body_for_tags = f"{title} {summary} {text[:2500]}"
        categories = _categories(body_for_tags)
        tags = [merchant, source["bank_name"], *categories]
        activities.append(Promotion(
            id=_stable_id(source["id"], detail.final_url),
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=title,
            merchant=merchant or title,
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=summary,
            source_url=detail.final_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            registration_url=_registration_url(page.links, "dbs") if registration_required else "",
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, now.astimezone(TAIPEI).date()),
            tags=list(dict.fromkeys(tags)),
            review_required=registration_required and not windows,
        ))

    status = "complete" if failed_details == 0 and activities else ("partial" if activities else "failed")
    message = "" if failed_details == 0 else f"{failed_details} 個購物網站活動頁暫時無法讀取。"
    if alerts:
        message = f"{message} 使用者指定入口需留意。".strip()
    status = "partial" if alerts and status == "complete" else status
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message
    ), alerts


def _find_campaign_properties(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if "campaignTitle" in value and ("startDate" in value or "cpCode" in value):
            return value
        for item in value.values():
            found = _find_campaign_properties(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_campaign_properties(item)
            if found:
                return found
    return {}


def _model_registration_text(model: Any, props: dict[str, Any]) -> str:
    values: list[str] = []
    notice = props.get("cpNotice")
    if isinstance(notice, str) and "登錄" in notice:
        values.append(strip_html(notice))
    for value in walk_strings(model):
        if "登錄" not in value:
            continue
        cleaned = strip_html(value)
        if _has_registration_requirement(cleaned):
            values.append(cleaned)
    return " ".join(dict.fromkeys(value for value in values if value))[:2200]


def _has_registration_requirement(text: str) -> bool:
    if "登錄" not in text:
        return False
    markers = (
        "完成活動登錄始符合資格",
        "完成線上登錄",
        "完成登錄",
        "開放登錄",
        "開始登錄",
        "活動登錄限量",
        "活動登錄名額",
        "需活動登錄",
        "需登錄",
        "須登錄",
        "登錄成功",
        "登錄期間",
        "登錄加碼",
        "月月活動登錄",
        "每波活動須分別登錄",
        "各檔須分別登錄",
        "需於本行網站登錄",
    )
    if any(marker in text for marker in markers):
        return True
    return bool(re.search(r"(?:須|需)於.{0,80}登錄", text) or re.search(r"(?:須|需)至.{0,80}登錄", text))


def _model_links(model: Any, base_url: str) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for value in walk_strings(model):
        if "href=" not in value:
            continue
        page = parse_page(value, base_url)
        links.extend(page.links)
    return links


def extract_cathay(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    alerts: list[Alert] = []
    data_url = source["data_url"]
    resolved_entry = ""
    entry_status = "complete"
    try:
        entry = fetch_text(source["entry_url"].split("#", 1)[0], source["official_domains"])
        resolved_entry = entry.final_url
    except RuntimeError:
        replacement = _discover_replacement(source, keywords=("活動專區", "信用卡", "event/overview"))
        entry_status = "partial"
        if replacement:
            resolved_entry = replacement
            alerts.append(Alert(
                "source_relocated",
                source["bank_name"],
                "使用者指定的活動入口無法讀取，已從銀行官方網站找到候選替代網址。",
                source["entry_url"],
                replacement,
            ))
        else:
            alerts.append(Alert(
                "source_failed",
                source["bank_name"],
                "使用者指定的活動入口無法讀取，尚未找到替代網址；暫以官方結構化資料更新。",
                source["entry_url"],
            ))
    try:
        listing_result, listing = fetch_json(data_url, source["official_domains"])
    except RuntimeError as exc:
        replacement = _discover_replacement(source, keywords=("credit-card.model.list.json", "活動專區", "信用卡"))
        if replacement and replacement.endswith(".json"):
            alerts.append(Alert(
                "source_relocated", source["bank_name"],
                "指定活動資料網址無法讀取，已找到官方候選替代網址。",
                data_url, replacement,
            ))
            data_url = replacement
            listing_result, listing = fetch_json(data_url, source["official_domains"])
        else:
            return [], SourceHealth(
                source["id"], source["bank_name"], data_url, "", "failed", 0, checked_at, str(exc)
            ), [Alert("source_failed", source["bank_name"], "官方活動資料無法讀取，且未找到替代網址。", data_url)]

    today = now.astimezone(TAIPEI).date()
    activities: list[Promotion] = []
    failed_details = 0
    campaigns = listing.get("campaigns", []) if isinstance(listing, dict) else []
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        props = campaign.get("campaignProps", {})
        if not isinstance(props, dict) or ONLINE_TAG not in props.get("campaignFilter", []):
            continue
        start = _date_from_iso(props.get("startDate")) or today
        end = _date_from_iso(props.get("endDate"))
        explicit_ended = "活動已結束" in str(props.get("campaignTitle", ""))
        if end and end < today and not explicit_ended:
            continue
        path = str(campaign.get("campaignPath", ""))
        if not path:
            continue
        public_url = urljoin("https://www.cathay-cube.com.tw", path)
        model_url = public_url[:-5] + ".model.json" if public_url.endswith(".html") else f"{public_url}.model.json"
        model: Any = {}
        detail_props: dict[str, Any] = {}
        detail_links: list[dict[str, str]] = []
        try:
            _, model = fetch_json(model_url, source["official_domains"])
            detail_props = _find_campaign_properties(model)
            detail_links = _model_links(model, public_url)
        except RuntimeError:
            failed_details += 1
        combined_props = {**props, **detail_props}
        title = strip_html(str(
            combined_props.get("campaignTitle")
            or combined_props.get("jcr:title")
            or combined_props.get("pageTitle")
            or "國泰世華網購活動"
        ))
        summary = clean_inline(str(
            combined_props.get("campaignContent")
            or combined_props.get("cpContent")
            or combined_props.get("jcr:description")
            or title
        ))
        registration_text = _model_registration_text(model, combined_props)
        windows = _registration_windows(registration_text, start.year, start, end)
        registration_required = bool(
            _has_registration_requirement(registration_text)
            or windows
            or _has_registration_requirement(f"{title} {summary}")
        )
        reward_percent, reward_amount = _reward_values(f"{title} {summary}")
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(f"{title} {summary}")
        merchant = re.split(r"刷|領券|分期|滿額|最高|2026", title, maxsplit=1)[0].strip(" ，、")
        merchant = merchant or title
        activities.append(Promotion(
            id=_stable_id(source["id"], public_url),
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=title,
            merchant=merchant,
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=summary,
            source_url=public_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            registration_url=_registration_url(detail_links, "cathay") if registration_required else "",
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today, ended=explicit_ended),
            tags=list(dict.fromkeys([merchant, source["bank_name"], *categories])),
            official_status="ended_by_official" if explicit_ended else "published",
            review_required=registration_required and not windows,
        ))

    status = "complete" if failed_details == 0 and activities else ("partial" if activities else "failed")
    if entry_status == "partial" and status == "complete":
        status = "partial"
    message = "" if failed_details == 0 else f"{failed_details} 個活動明細的結構化頁面暫時無法讀取。"
    if entry_status == "partial":
        message = f"{message} 使用者指定入口需留意。".strip()
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], resolved_entry or listing_result.final_url,
        status, len(activities), checked_at, message
    ), alerts


def _html_segments(text: str, marker: str) -> list[str]:
    matches = list(re.finditer(marker, text, flags=re.I))
    return [
        text[match.start(): matches[index + 1].start() if index + 1 < len(matches) else len(text)]
        for index, match in enumerate(matches)
    ]


def _ctbc_card_blocks(text: str) -> list[str]:
    return [
        match.group(0)
        for match in re.finditer(
            r'<li\s+class="card-list__item"[^>]*>.*?</li>',
            text,
            flags=re.I | re.S,
        )
    ]


def _class_text(block: str, class_name: str) -> str:
    match = re.search(
        rf'<(?P<tag>[a-z0-9]+)[^>]*class="[^"]*\b{re.escape(class_name)}\b[^"]*"[^>]*>'
        rf'(?P<body>.*?)</(?P=tag)>',
        block,
        flags=re.I | re.S,
    )
    return strip_html(match.group("body")) if match else ""


def _parse_period(value: str, default_year: int) -> tuple[date, date | None] | None:
    match = re.search(
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})\s*[~～－–—-]\s*"
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})",
        value,
    )
    if not match:
        return None
    start_year = int(match.group(1) or default_year)
    end_year = int(match.group(4) or start_year)
    try:
        return (
            date(start_year, int(match.group(2)), int(match.group(3))),
            date(end_year, int(match.group(5)), int(match.group(6))),
        )
    except ValueError:
        return None


def _compact_period(value: str) -> tuple[date, date | None] | None:
    match = re.fullmatch(r"(\d{14})-(\d{14})", value.strip())
    if not match:
        return None
    try:
        start = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").date()
        end = datetime.strptime(match.group(2), "%Y%m%d%H%M%S").date()
    except ValueError:
        return None
    return start, (None if end.year >= 2999 else end)


def _official_detail_period(
    text: str,
    fallback_start: date,
    fallback_end: date | None,
    today: date,
) -> tuple[date, date | None]:
    labelled = re.search(
        r"(?:活動期間|活動時間|活動日期)[：:\s]*"
        r"((?:(?:20\d{2})/)?\d{1,2}/\d{1,2}\s*[~～－–—-]\s*"
        r"(?:(?:20\d{2})/)?\d{1,2}/\d{1,2})",
        text,
    )
    if labelled:
        parsed = _parse_period(labelled.group(1), fallback_start.year)
        if parsed:
            return parsed
    if fallback_end is None and fallback_start.year < today.year:
        return today, None
    return fallback_start, fallback_end


def _fetch_many(urls: list[str], domains: list[str], workers: int = 6) -> dict[str, Any]:
    def fetch_one(url: str) -> Any:
        try:
            return fetch_text(url, domains)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1))) as executor:
        results = list(executor.map(fetch_one, urls))
    return dict(zip(urls, results, strict=True))


def extract_ctbc(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    try:
        entry = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        replacement = _discover_replacement(
            source,
            keywords=("LINE Pay", "優惠一覽表", "點數生活圈", "store.html"),
        )
        if not replacement:
            return [], SourceHealth(
                source["id"], source["bank_name"], source["entry_url"], "",
                "failed", 0, checked_at, str(exc),
            ), [Alert(
                "source_failed", source["bank_name"],
                "指定的 LINE Pay 卡優惠入口無法讀取，且未找到官方替代網址。",
                source["entry_url"],
            )]
        alerts.append(Alert(
            "source_relocated", source["bank_name"],
            "使用者指定入口無法讀取，已從中國信託官方網站找到候選替代網址。",
            source["entry_url"], replacement,
        ))
        entry = fetch_text(replacement, source["official_domains"])

    entry_page = parse_page(entry.text, entry.final_url)
    category_links: list[tuple[str, str]] = []
    for link in entry_page.links:
        category_url = link["url"].split("#", 1)[0]
        match = re.search(r"/LINEPay/page_(food|shopping|fashion|pet|life|travel)\.html$", urlsplit(category_url).path)
        if match and category_url not in {url for _, url in category_links}:
            category_links.append((match.group(1), category_url))
    if not category_links:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], entry.final_url,
            "failed", 0, checked_at, "官方入口未提供可辨識的優惠分類頁。",
        ), [*alerts, Alert(
            "source_failed", source["bank_name"],
            "官方入口仍可讀取，但優惠分類結構已改變，需檢查擷取規則。",
            source["entry_url"],
        )]

    category_names = {
        "food": "餐飲美食",
        "shopping": "網購",
        "fashion": "百貨購物",
        "pet": "生活消費",
        "life": "生活消費",
        "travel": "旅遊交通",
    }
    fetched = _fetch_many([url for _, url in category_links], source["official_domains"])
    failed_pages = 0
    activities: list[Promotion] = []
    for category_id, requested_url in category_links:
        result = fetched[requested_url]
        if isinstance(result, Exception):
            failed_pages += 1
            continue
        for block in _ctbc_card_blocks(result.text):
            title = _class_text(block, "sr-only")
            if not title:
                alt = re.search(r'<img[^>]+alt="([^"]+)"', block, flags=re.I)
                title = clean_inline(alt.group(1)) if alt else ""
            period_text = _class_text(block, "card__date")
            period = _parse_period(period_text, today.year)
            if not title or not period:
                continue
            start, end = period
            if end and end < today:
                continue
            main_offer = _class_text(block, "card__main")
            body_text = strip_html(block)
            summary = clean_inline("｜".join(item for item in (main_offer, _class_text(block, "card__text")) if item))
            summary = summary or title
            links = parse_page(block, result.final_url).links
            register_link = next(
                (link["url"] for link in links if "登錄" in link.get("text", "") and link["url"].startswith("https://")),
                "",
            )
            registration_text = _registration_excerpt(body_text)
            windows = _registration_windows(registration_text or body_text, start.year, start, end)
            registration_required = bool(register_link or windows or _has_registration_requirement(body_text))
            reward_percent, reward_amount = _reward_values(f"{title} {summary} {body_text}")
            high_return = (
                (reward_percent is not None and reward_percent >= percent_threshold)
                or (reward_amount is not None and reward_amount >= amount_threshold)
            )
            detail_target = re.search(r'data-target="(#[^"]+)"', block)
            public_url = result.final_url.split("#", 1)[0] + (detail_target.group(1) if detail_target else "")
            categories = _categories(body_text, category_names[category_id])
            activities.append(Promotion(
                id=_stable_id(source["id"], f"{public_url}|{title}|{start.isoformat()}|{summary}"),
                bank_id=source["id"],
                bank_name=source["bank_name"],
                title=title,
                merchant=title,
                categories=categories,
                start_date=start.isoformat(),
                end_date=_date_text(end),
                summary=summary[:700],
                source_url=public_url,
                source_entry_url=source["entry_url"],
                observed_at=checked_at,
                registration_required=registration_required,
                registration_text=registration_text,
                registration_url=register_link or (REGISTRATION_URL_DEFAULTS["ctbc"] if registration_required else ""),
                registration_windows=windows,
                max_reward_percent=reward_percent,
                max_reward_amount_twd=reward_amount,
                high_return=high_return,
                featured=registration_required or high_return,
                lifecycle=_lifecycle(start, end, today),
                tags=list(dict.fromkeys([title, source["bank_name"], *categories])),
                review_required=registration_required and not windows,
            ))

    status = "complete" if activities and failed_pages == 0 and not alerts else ("partial" if activities else "failed")
    message = "" if failed_pages == 0 else f"{failed_pages} 個官方優惠分類頁暫時無法讀取。"
    if alerts:
        message = f"{message} 使用者指定入口需留意。".strip()
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], entry.final_url,
        status, len(activities), checked_at, message,
    ), alerts


def extract_sinopac(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        replacement = _discover_replacement(source, keywords=("刷卡享優惠", "discount", "信用卡"))
        if not replacement:
            return [], SourceHealth(
                source["id"], source["bank_name"], source["entry_url"], "",
                "failed", 0, checked_at, str(exc),
            ), [Alert(
                "source_failed", source["bank_name"],
                "指定的刷卡優惠入口無法讀取，且未找到官方替代網址。",
                source["entry_url"],
            )]
        alerts.append(Alert(
            "source_relocated", source["bank_name"],
            "使用者指定入口無法讀取，已從永豐銀行官方網站找到候選替代網址。",
            source["entry_url"], replacement,
        ))
        listing = fetch_text(replacement, source["official_domains"])

    cards: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for block in _html_segments(listing.text, r"<li\s+data-id="):
        href = re.search(r'href="(\./\d+\.html)"', block)
        compact = re.search(r'data-period="([^"]+)"', block)
        title_match = re.search(r"<h2>\s*<a[^>]*>(.*?)</a>\s*</h2>", block, flags=re.I | re.S)
        if not href or not compact or not title_match:
            continue
        period = _compact_period(compact.group(1))
        if not period:
            continue
        start, end = period
        if end and end < today:
            continue
        public_url = urljoin(listing.final_url, href.group(1))
        if public_url in seen_urls:
            continue
        seen_urls.add(public_url)
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        cards.append({
            "url": public_url,
            "title": strip_html(title_match.group(1)),
            "summary": strip_html(paragraphs[0]) if paragraphs else strip_html(title_match.group(1)),
            "start": start,
            "end": end,
        })

    fetched = _fetch_many([item["url"] for item in cards], source["official_domains"])
    activities: list[Promotion] = []
    failed_details = 0
    for card in cards:
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        start, end = _official_detail_period(text, card["start"], card["end"], today)
        registration_text = _registration_excerpt(text)
        windows = _registration_windows(registration_text or text, start.year, start, end)
        registration_required = bool(windows or _has_registration_requirement(text))
        reward_percent, reward_amount = _reward_values(f"{card['title']} {card['summary']} {text[:4500]}")
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(f"{card['title']} {card['summary']} {text[:1800]}", None)
        registration_url = ""
        if registration_required:
            registration_url = _registration_url(page.links, "sinopac") if page else public_url
            if registration_url == REGISTRATION_URL_DEFAULTS["sinopac"]:
                registration_url = public_url
        activities.append(Promotion(
            id=_stable_id(source["id"], public_url),
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=card["title"],
            merchant=card["title"],
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=card["summary"][:700],
            source_url=public_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([card["title"], source["bank_name"], *categories])),
            review_required=registration_required and not windows,
        ))

    status = "complete" if activities and failed_details == 0 and not alerts else ("partial" if activities else "failed")
    message = "" if failed_details == 0 else f"{failed_details} 個官方活動明細暫時無法讀取。"
    if alerts:
        message = f"{message} 使用者指定入口需留意。".strip()
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), alerts


def extract_scsb(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        replacement = _discover_replacement(source, keywords=("熱門活動", "刷卡優惠", "hotList"))
        if not replacement:
            return [], SourceHealth(
                source["id"], source["bank_name"], source["entry_url"], "",
                "failed", 0, checked_at, str(exc),
            ), [Alert(
                "source_failed", source["bank_name"],
                "指定的熱門活動入口無法讀取，且未找到官方替代網址。",
                source["entry_url"],
            )]
        alerts.append(Alert(
            "source_relocated", source["bank_name"],
            "使用者指定入口無法讀取，已從上海商銀官方網站找到候選替代網址。",
            source["entry_url"], replacement,
        ))
        listing = fetch_text(replacement, source["official_domains"])

    cards: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for block in _html_segments(
        listing.text,
        r'<div\s+class="col-md-6"[^>]*data-type-tag="[^"]+"',
    ):
        type_match = re.search(r'data-type-tag="([^"]+)"', block)
        href = re.search(r'<a[^>]+href="([^"]+)"[^>]+class="stretched-link"[^>]*', block, flags=re.I)
        if not href:
            href = re.search(r'<a[^>]+href="([^"]+)"[^>]+title="([^"]+)"', block, flags=re.I)
        period_text = _class_text(block, "date")
        title = _class_text(block, "main_title")
        if not title:
            title_attr = re.search(r'<a[^>]+title="([^"]+)"', block, flags=re.I)
            title = clean_inline(title_attr.group(1)) if title_attr else ""
        period = _parse_period(period_text, today.year)
        if not href or not title or not period:
            continue
        start, end = period
        if end and end < today:
            continue
        public_url = urljoin(listing.final_url, href.group(1))
        if public_url in seen_urls:
            continue
        seen_urls.add(public_url)
        cards.append({
            "url": public_url,
            "title": title,
            "summary": _class_text(block, "sub_title") or title,
            "type": type_match.group(1) if type_match else "",
            "start": start,
            "end": end,
        })

    fetched = _fetch_many([item["url"] for item in cards], source["official_domains"])
    type_categories = {
        "mobilepay": "行動支付",
        "travel": "旅遊交通",
        "installment": "分期",
        "card": "百貨購物",
        "cdcard": "其他優惠",
    }
    activities: list[Promotion] = []
    failed_details = 0
    for card in cards:
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        start, end = _official_detail_period(text, card["start"], card["end"], today)
        registration_text = _registration_excerpt(text)
        windows = _registration_windows(registration_text or text, start.year, start, end)
        registration_required = bool(windows or _has_registration_requirement(text))
        reward_percent, reward_amount = _reward_values(f"{card['title']} {card['summary']} {text[:4500]}")
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(
            f"{card['title']} {card['summary']} {text[:1800]}",
            type_categories.get(card["type"], "其他優惠"),
        )
        registration_url = _registration_url(page.links, "scsb") if registration_required and page else (
            REGISTRATION_URL_DEFAULTS["scsb"] if registration_required else ""
        )
        activities.append(Promotion(
            id=_stable_id(source["id"], public_url),
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=card["title"],
            merchant=card["title"],
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=card["summary"][:700],
            source_url=public_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([card["title"], source["bank_name"], *categories])),
            review_required=registration_required and not windows,
        ))

    status = "complete" if activities and failed_details == 0 and not alerts else ("partial" if activities else "failed")
    message = "" if failed_details == 0 else f"{failed_details} 個官方活動明細暫時無法讀取。"
    if alerts:
        message = f"{message} 使用者指定入口需留意。".strip()
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), alerts
