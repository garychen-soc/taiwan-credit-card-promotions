from __future__ import annotations

import hashlib
import re
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


def _categories(text: str) -> list[str]:
    values = ["網購"]
    rules = (
        ("行動支付", ("錢包", "LINE Pay", "OPEN POINT", "支付")),
        ("百貨購物", ("百貨", "3C", "全國電子", "燦坤", "家電")),
        ("生活消費", ("星巴克", "屈臣氏", "全家", "超商")),
        ("旅遊交通", ("旅遊", "交通", "外送")),
    )
    for label, words in rules:
        if any(word.lower() in text.lower() for word in words):
            values.append(label)
    if "分期" in text:
        values.append("分期")
    return values


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


def _registration_windows(text: str, activity_year: int) -> list[RegistrationWindow]:
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

    return sorted(windows, key=lambda item: item.start)


def _registration_excerpt(text: str) -> str:
    lines = [clean_inline(line) for line in text.splitlines() if "登錄" in line]
    useful = [
        line for line in lines
        if any(token in line for token in ("/", "開始登錄", "開放登錄", "登錄期間", "無需登錄", "不需登錄"))
    ]
    return " ".join(dict.fromkeys(useful))[:1600]


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
        windows = _registration_windows(registration_text or text, start.year)
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
        "開放登錄",
        "開始登錄",
        "活動登錄限量",
        "活動登錄名額",
        "登錄成功",
        "登錄期間",
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
        windows = _registration_windows(registration_text, start.year)
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
