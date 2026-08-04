from __future__ import annotations

import calendar
import hashlib
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .cache import record_detail_requests, reuse_cached_promotion, source_fingerprint
from .fetch import (
    PersistentHTTPSession,
    SystemCurlSession,
    fetch_json,
    fetch_text,
    is_allowed_url,
)
from .html_tools import clean_inline, parse_page, strip_html, walk_strings
from .models import Alert, Promotion, RegistrationWindow, SourceHealth


TAIPEI = ZoneInfo("Asia/Taipei")
ONLINE_TAG = "cub-tags:camapigns/credit-card/online-shopping"
REGISTRATION_URL_DEFAULTS = {
    "dbs": "https://www.dbs.com.tw/personal-zh/digital-service/cardplus/announcement#/?redirect=Btn_Campaign&source=pweb",
    "cathay": "https://www.cathaybk.com.tw/promotion/",
    "ctbc": "https://www.ctbcbank.com/twrbo/zh_tw/onlinecounter_index/cc_service/cc_service_register.html",
    "sinopac": "https://bank.sinopac.com/sinopacBT/personal/credit-card/discount/list.html",
    "scsb": "https://ebank.scsb.com.tw/ternal/ofwbcv05/page#/ofwb/cv/05/01",
    "obank": "https://www.o-bank.com/retail/event/event-compaign",
    "yuanta": "https://www.yuantabank.com.tw/bank/creditCard/productActivityMember/list.do",
    "esun": "https://www.esunbank.com/zh-tw/personal/credit-card/discount/shops",
    "sunny": "https://www.sunnybank.com.tw/portal/pt/pt01002/PT01002Index.xhtml",
    "tcbbank": "https://www.tcbbank.com.tw/CreditCard/RegQuery2/Act_login.aspx",
    "kgi": "https://www.kgibank.com/creditcard/campaign/registrationlist",
    "hncb": "https://netbank.hncb.com.tw/netbank/servlet/TrxDispatcher?trx=com.lb.wibc.trx.CardPromoteOverall_RWD&state=prompt",
    "taipei_fubon": "https://www.fubon.com/banking/event/credit_card/20170718A/index.html",
    "taishin": "https://mkpcard.taishinbank.com.tw/tscccms/signin",
    "first": "https://card.firstbank.com.tw/sites/card/touch/1565690686288",
    "chb": "https://www.bankchb.com/frontend/CampaignLog.html",
    "ubot": "https://card.ubot.com.tw/eCard/activity_login/register_activity.aspx",
}
STRICT_REGISTRATION_URL_BANKS = frozenset({
    "dbs",
    "cathay",
    "ctbc",
    "scsb",
    "yuanta",
    "tcbbank",
    "kgi",
    "hncb",
    "taipei_fubon",
    "taishin",
    "chb",
})
REGISTRATION_URL_ALLOWED_HOSTS = {
    "sinopac": ("bank.sinopac.com",),
    "obank": ("o-bank.com",),
    "esun": ("esunbank.com.tw", "esun.co"),
    "sunny": ("sunnybank.com.tw",),
    "ubot": ("card.ubot.com.tw", "cardweb.ubot.com.tw"),
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


def _normalize_roc_dates(value: str) -> str:
    return re.sub(
        r"(?<!\d)(1\d{2})(?=(?:年|/)\d{1,2}(?:月|/)\d{1,2})",
        lambda match: str(int(match.group(1)) + 1911),
        value,
    )


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
        ("生活消費", ("星巴克", "屈臣氏", "全家", "超商", "藥妝", "生活")),
        ("旅遊交通", ("旅遊", "交通", "外送", "航空", "飯店", "旅行社")),
        ("餐飲美食", ("餐廳", "美食", "咖啡", "漢堡", "壽司", "foodpanda", "Uber Eats")),
        ("加油交通", ("加油", "捷運", "停車", "高鐵")),
        ("海外消費", ("海外", "日本", "韓國", "泰國", "中港澳")),
        ("繳費稅款", ("繳費", "繳稅", "學費", "公共事業")),
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
    if marker == "中午" and hour < 11:
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
    normalized_text = unicodedata.normalize("NFKC", text)
    normalized_text = (
        normalized_text.replace("〜", "~")
        .replace("～", "~")
        .replace("至", "~")
        .replace("—", "~")
        .replace("–", "~")
        .replace("－", "~")
    )
    normalized_text = re.sub(
        r"(?<=\d):(\d{2}):\d{2}(?=\D|$)",
        r":\1",
        normalized_text,
    )
    normalized = _normalize_roc_dates(
        clean_inline(normalized_text)
    )
    windows: list[RegistrationWindow] = []
    seen: set[tuple[str, str | None]] = set()
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
        r"(上午|下午|中午)?\s*(\d{1,2})\s*(?::|點(?:整)?)\s*(\d{2})?"
    )
    for match in point_pattern.finditer(normalized):
        if any(start <= match.start() < end for start, end in range_spans):
            continue
        context = normalized[max(0, match.start() - 90): min(len(normalized), match.end() + 110)]
        if not any(marker in context for marker in ("開始登錄", "開放登錄", "活動登錄日期")):
            continue
        nearby = normalized[max(0, match.start() - 25): min(len(normalized), match.end() + 20)]
        if (
            "登錄期限" in nearby
            or re.search(r"(?:截止|最晚).{0,20}$", nearby[: match.start() - max(0, match.start() - 25)])
            or re.match(r"\s*(?:止|前|截止)", normalized[match.end():])
        ):
            continue
        year = int(match.group(1) or activity_year)
        hour, minute = _time_parts(match.group(5), match.group(6), match.group(4))
        try:
            start = datetime(year, int(match.group(2)), int(match.group(3)), hour, minute, tzinfo=TAIPEI)
        except ValueError:
            continue
        key = (start.isoformat(), None)
        if key in seen:
            continue
        seen.add(key)
        windows.append(RegistrationWindow(*key, "登錄開放", context))

    deadline_pattern = re.compile(
        r"(?:登錄期限(?:[~為])?|最晚(?:須)?於|須於)"
        r"\s*(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})"
        r"(?:\([^)]+\))?\s*(上午|下午)?\s*(\d{1,2})[:點](\d{2})?"
        r"(?:\s*(?:前|止))?"
    )
    for match in deadline_pattern.finditer(normalized):
        if any(match.start() < end and match.end() > start for start, end in range_spans):
            continue
        context = normalized[max(0, match.start() - 30): min(len(normalized), match.end() + 60)]
        if "登錄期限" not in match.group(0) and "登錄" not in context:
            continue
        year = int(match.group(1) or activity_year)
        hour, minute = _time_parts(match.group(5), match.group(6), match.group(4))
        try:
            deadline = datetime(
                year,
                int(match.group(2)),
                int(match.group(3)),
                hour,
                minute,
                tzinfo=TAIPEI,
            )
        except ValueError:
            continue
        key = (deadline.isoformat(), None)
        if key in seen:
            continue
        seen.add(key)
        windows.append(RegistrationWindow(*key, "登錄截止", context))

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
        key = (start.isoformat(), None)
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
        key = (start.isoformat(), None)
        if key in seen:
            return
        seen.add(key)
        windows.append(RegistrationWindow(*key, label, source_text))

    explicit_dates_found = False
    for explicit in re.finditer(r"登錄日期如下.{0,180}", normalized):
        before = normalized[max(0, explicit.start() - 180):explicit.start()]
        time_match = re.search(
            r"(上午|下午|中午)?\s*(\d{1,2})\s*(?::|點)\s*(\d{2})",
            before,
        )
        if not time_match:
            continue
        hour, minute = _time_parts(time_match.group(2), time_match.group(3), time_match.group(1))
        for date_match in re.finditer(
            r"\d{1,2}月\s*[:：]\s*(\d{1,2})/(\d{1,2})",
            explicit.group(0),
        ):
            try:
                day = date(activity_year, int(date_match.group(1)), int(date_match.group(2)))
            except ValueError:
                continue
            add_recurring(day, hour, minute, "指定月份登錄開放", explicit.group(0))
            explicit_dates_found = True

    monthly_day_pattern = re.compile(
        r"每月\s*(\d{1,2})\s*[號日]\s*(上午|下午|中午)?\s*(\d{1,2})"
        r"(?:\s*:\s*(\d{2})|[點時](?:整)?)\s*(?:起)?(?:開放|開始)?登錄"
    )
    for match in ([] if explicit_dates_found else monthly_day_pattern.finditer(normalized)):
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

    by_start: dict[str, RegistrationWindow] = {}
    for window in sorted(windows, key=lambda item: (item.start, item.end or "")):
        existing = by_start.get(window.start)
        if existing is None or (existing.end is None and window.end is not None):
            by_start[window.start] = window
    return sorted(by_start.values(), key=lambda item: item.start)


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


TERMS_SECTION_RULES = (
    ("period", ("活動期間", "優惠期間", "活動日期", "活動時間")),
    ("eligibility", ("參加資格", "適用對象", "適用卡別", "活動資格")),
    ("offer", ("優惠內容", "回饋內容", "活動內容")),
    ("method", ("活動辦法", "參加辦法", "活動方式", "消費門檻")),
    ("registration", ("登錄辦法", "登錄方式", "登錄時間", "登錄期間")),
    ("installment", ("分期辦法", "分期")),
    ("quota", ("限量名額", "限量", "名額", "額滿")),
    ("notes", ("注意事項", "重要事項", "活動注意事項")),
)
PUBLIC_TERMS_SECTION_LIMIT = 1800
PUBLIC_TERMS_TOTAL_LIMIT = 6000
def _terms_content(text: str) -> tuple[str, dict[str, str]]:
    """Keep full report text and compact original-language sections for the UI."""
    raw = text.strip()
    if not raw:
        return "", {}
    anchors = tuple(
        anchor
        for _, values in TERMS_SECTION_RULES
        for anchor in values
    )
    expanded = re.sub(
        rf"(?<!\n)(?=(?:{'|'.join(map(re.escape, anchors))})[：:])",
        "\n",
        raw,
    )
    grouped: dict[str, list[str]] = {}
    current = "overview"
    for source_line in expanded.splitlines():
        line = clean_inline(source_line)
        if not line:
            continue
        heading_key = next(
            (
                key
                for key, values in TERMS_SECTION_RULES
                if any(
                    re.match(rf"^[※＊*【\[(（]?\s*{re.escape(anchor)}", line)
                    for anchor in values
                )
            ),
            "",
        )
        if heading_key:
            current = heading_key
        elif current == "overview":
            inferred_key = next(
                (
                    key
                    for key, values in TERMS_SECTION_RULES
                    if any(anchor in line for anchor in values)
                ),
                "overview",
            )
            current = inferred_key
        grouped.setdefault(current, []).append(line)

    sections: dict[str, str] = {}
    remaining = PUBLIC_TERMS_TOTAL_LIMIT
    for key in ("period", "eligibility", "offer", "method", "registration", "installment", "quota", "notes", "overview"):
        value = "\n".join(dict.fromkeys(grouped.get(key, []))).strip()
        if not value or remaining <= 0:
            continue
        clipped = value[: min(PUBLIC_TERMS_SECTION_LIMIT, remaining)]
        sections[key] = clipped
        remaining -= len(clipped)
    return raw, sections


SUBACTIVITY_HEADING = re.compile(
    r"(?m)^(?:【((?:活動[一二三四五六七八九十]|玉山卡|玉山\s*Pi|玉山Unicard)[^】]{0,45})】([^\n]{0,80})"
    r"|(活動[一二三四五六七八九十]))\s*$"
)


def _subactivity_blocks(text: str) -> list[tuple[str, str]]:
    """Split only pages with two or more reliable, repeated activity headings."""
    primary = text.split("\n注意事項\n", 1)[0]
    matches = list(SUBACTIVITY_HEADING.finditer(primary))
    if len(matches) < 2:
        return []
    blocks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for index, match in enumerate(matches):
        heading = clean_inline(
            f"{match.group(1) or ''} {match.group(2) or ''}"
            if match.group(1) else (match.group(3) or "")
        )
        if heading in seen:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(primary)
        block = primary[match.start():end].strip()
        if not re.search(r"活動(?:期間|日期)[：:]", block):
            continue
        seen.add(heading)
        blocks.append((heading, block))
    return blocks if len(blocks) >= 2 else []


def _subactivity_period(
    text: str,
    fallback_start: date,
    fallback_end: date | None,
    today: date,
) -> tuple[date, date | None]:
    periods = _activity_periods(text, fallback_start.year)
    if periods:
        return periods[0]["start"], periods[-1]["end"]
    parsed = _official_detail_period(text, fallback_start, fallback_end, today)
    single = re.search(
        r"活動日期[：:\s]*(20\d{2})/(\d{1,2})/(\d{1,2})",
        _normalize_roc_dates(text),
    )
    if single:
        try:
            value = date(int(single.group(1)), int(single.group(2)), int(single.group(3)))
            return value, value
        except ValueError:
            pass
    return parsed


def _activity_periods(text: str, default_year: int) -> list[dict[str, Any]]:
    normalized = _normalize_roc_dates(text)
    pattern = re.compile(
        r"(?m)^(第一波|第二波|第三波)?\s*"
        r"((?:20\d{2}/)?\d{1,2}/\d{1,2}\s*(?:[~～－–—-]|至)\s*"
        r"(?:20\d{2}/)?\d{1,2}/\d{1,2})\s*$"
    )
    values: list[dict[str, Any]] = []
    for match in pattern.finditer(normalized):
        parsed = _parse_period(match.group(2), default_year)
        if not parsed:
            continue
        start, end = parsed
        value = {"start": start, "end": end or start, "label": match.group(1) or "活動期間"}
        if value not in values:
            values.append(value)
    return sorted(values, key=lambda item: item["start"])


def _reward_tiers(text: str) -> list[dict[str, int]]:
    tiers: list[dict[str, int]] = []
    pattern = re.compile(
        r"(?m)^([\d,]+)元\s*\n([\d,]+)元\s*\n([\d,]+)元\s*\n(\d+)名\s*$"
    )
    for match in pattern.finditer(text):
        tiers.append({
            "spend_amount_twd": int(match.group(1).replace(",", "")),
            "reward_amount_twd": int(match.group(2).replace(",", "")),
            "installment_reward_amount_twd": int(match.group(3).replace(",", "")),
            "quota": int(match.group(4)),
        })
    return tiers


def _promotion_invariants(promotion: Promotion) -> None:
    start = _date_from_iso(promotion.start_date)
    end = _date_from_iso(promotion.end_date)
    if not promotion.activity_periods and start and promotion.terms_raw:
        parsed_periods = _activity_periods(promotion.terms_raw, start.year)
        promotion.activity_periods = [
            {"start": item["start"].isoformat(), "end": item["end"].isoformat(), "label": item["label"]}
            for item in parsed_periods
        ]
        if len(parsed_periods) > 1:
            start = parsed_periods[0]["start"]
            end = parsed_periods[-1]["end"]
            promotion.start_date = start.isoformat()
            promotion.end_date = end.isoformat()
    issues: list[str] = []
    ordered = sorted(
        promotion.registration_windows,
        key=lambda item: item.start,
    )
    for window in ordered:
        window_start = datetime.fromisoformat(window.start)
        window_end = datetime.fromisoformat(window.end) if window.end else None
        if window_end and window_end <= window_start:
            issues.append("登錄截止時間不晚於開始時間")
        if start and window_start.date() < start:
            issues.append("登錄時間早於活動期間")
        if end and window_start.date() > end:
            issues.append("登錄時間晚於活動期間")
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = datetime.fromisoformat(previous.end) if previous.end else None
        current_start = datetime.fromisoformat(current.start)
        if previous_end and current_start < previous_end:
            issues.append("同一子活動的登錄視窗互相重疊")
            break
    if issues:
        promotion.needs_review = True
        promotion.review_required = True
        promotion.review_message = "；".join(dict.fromkeys(issues)) + "，請至官方頁確認對應的登錄時間。"


def _registration_url(links: list[dict[str, str]], bank_id: str) -> str:
    if bank_id in STRICT_REGISTRATION_URL_BANKS:
        return REGISTRATION_URL_DEFAULTS[bank_id]
    for link in links:
        text = link.get("text", "")
        url = clean_inline(link.get("url", ""))
        if "登錄" in text and url.startswith("https://"):
            return normalize_registration_url(bank_id, url)
    return REGISTRATION_URL_DEFAULTS[bank_id]


def normalize_registration_url(bank_id: str, candidate: str = "") -> str:
    """Return a verified bank registration portal instead of a text-match link."""
    default = REGISTRATION_URL_DEFAULTS.get(bank_id, "")
    if bank_id in STRICT_REGISTRATION_URL_BANKS:
        return default
    value = clean_inline(candidate)
    if not value.startswith("https://"):
        return default
    hostname = (urlsplit(value).hostname or "").lower()
    allowed_hosts = REGISTRATION_URL_ALLOWED_HOSTS.get(bank_id, ())
    if allowed_hosts and any(
        hostname == allowed or hostname.endswith(f".{allowed}")
        for allowed in allowed_hosts
    ):
        if (
            bank_id == "ubot"
            and hostname == "card.ubot.com.tw"
            and urlsplit(value).path == "/eCard/activity_login/register_activity.aspx"
        ):
            return default
        return value
    return default


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
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    alerts: list[Alert] = []
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
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
    invalid_urls: list[str] = []
    for merchant, url in merchant_links:
        activity_id = _stable_id(source["id"], url)
        fingerprint = source_fingerprint(
            source["id"],
            {
                "listing_hash": listing.content_hash,
                "merchant": merchant,
                "url": url,
            },
        )
        cached = reuse_cached_promotion(
            cache,
            activity_id=activity_id,
            fingerprint=fingerprint,
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if cached:
            cached_start = _date_from_iso(cached.start_date) or today
            cached_end = _date_from_iso(cached.end_date)
            cached.registration_windows = _registration_windows(
                cached.registration_text,
                cached_start.year,
                cached_start,
                cached_end,
            )
            cached.registration_required = bool(
                cached.registration_windows
                or _has_registration_requirement(cached.registration_text)
            )
            cached.review_required = (
                cached.registration_required and not cached.registration_windows
            )
            cached.featured = cached.registration_required or cached.high_return
            activities.append(cached)
            continue
        record_detail_requests(stats)
        try:
            detail = fetch_text(url, source["official_domains"])
        except Exception as exc:
            failed_details += 1
            invalid_url = _invalid_detail_url(exc)
            if invalid_url:
                invalid_urls.append(invalid_url)
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
        terms_raw, terms_sections = _terms_content(text)
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
            id=activity_id,
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
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            reward_tiers=_reward_tiers(text),
            registration_url=_registration_url(page.links, "dbs") if registration_required else "",
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, now.astimezone(TAIPEI).date()),
            tags=list(dict.fromkeys(tags)),
            review_required=registration_required and not windows,
            source_fingerprint=fingerprint,
            last_detail_checked_at=checked_at,
        ))

    status = "complete" if failed_details == 0 and activities else ("partial" if activities else "failed")
    message = _detail_failure_message(failed_details, "購物網站活動頁", invalid_urls)
    alerts.extend(_invalid_url_alerts(source, invalid_urls))
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
    text = re.sub(r"(?:不須|不需|無需|免)(?:活動)?登錄", "", text)
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
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    alerts: list[Alert] = []
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
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
    invalid_urls: list[str] = []
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
        activity_id = _stable_id(source["id"], public_url)
        fingerprint = source_fingerprint(source["id"], campaign)
        cached = reuse_cached_promotion(
            cache,
            activity_id=activity_id,
            fingerprint=fingerprint,
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if cached:
            activities.append(cached)
            continue
        model: Any = {}
        detail_props: dict[str, Any] = {}
        detail_links: list[dict[str, str]] = []
        record_detail_requests(stats)
        try:
            _, model = fetch_json(model_url, source["official_domains"])
            detail_props = _find_campaign_properties(model)
            detail_links = _model_links(model, public_url)
        except Exception as exc:
            failed_details += 1
            invalid_url = _invalid_detail_url(exc)
            if invalid_url:
                invalid_urls.append(invalid_url)
                continue
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
        terms_text = "\n".join(
            dict.fromkeys(
                clean_inline(strip_html(value))
                for value in (
                    title,
                    summary,
                    registration_text,
                    *(
                        text
                        for text in walk_strings(model)
                        if any(
                            token in text
                            for token in (
                                "活動期間", "參加資格", "活動辦法", "登錄",
                                "注意事項", "分期", "限量", "回饋",
                            )
                        )
                    ),
                )
                if clean_inline(strip_html(value))
            )
        )
        terms_raw, terms_sections = _terms_content(terms_text)
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
            id=activity_id,
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
            terms_sections=terms_sections,
            terms_raw=terms_raw,
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
            source_fingerprint=fingerprint,
            last_detail_checked_at=checked_at,
        ))

    status = "complete" if failed_details == 0 and activities else ("partial" if activities else "failed")
    if entry_status == "partial" and status == "complete":
        status = "partial"
    message = _detail_failure_message(
        failed_details,
        "活動明細的結構化頁面",
        invalid_urls,
    )
    alerts.extend(_invalid_url_alerts(source, invalid_urls))
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
    normalized = (
        _normalize_roc_dates(value).replace("年", "/")
        .replace("月", "/")
        .replace("日", "")
        .replace(".", "/")
        .replace("起", "")
        .replace("止", "")
    )
    match = re.search(
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})\s*(?:[~～－–—-]|至)\s*"
        r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})",
        normalized,
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
    text = _normalize_roc_dates(text)
    labelled = re.search(
        r"(?:活動期間|活動時間|活動日期)[：:\s]*"
        r"((?:(?:20\d{2})[年/])?\d{1,2}[月/]\d{1,2}日?\s*(?:起)?"
        r"(?:[~～－–—-]|至)\s*(?:(?:20\d{2})[年/])?\d{1,2}[月/]\d{1,2}日?(?:止)?)",
        text,
    )
    if labelled:
        parsed = _parse_period(labelled.group(1), fallback_start.year)
        if parsed:
            return parsed
    if fallback_end is None and fallback_start.year < today.year:
        return today, None
    return fallback_start, fallback_end


INVALID_DETAIL_URL_MARKERS = (
    "URL is outside official domains:",
    "Redirected outside official domains:",
    "Final URL is outside official domains:",
)


def _invalid_detail_url(error: BaseException) -> str:
    """Return the rejected URL from a fetch exception chain, if present."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current)
        for marker in INVALID_DETAIL_URL_MARKERS:
            if marker not in message:
                continue
            value = message.split(marker, 1)[1].strip()
            match = re.match(r"https?://[^\s]+", value)
            if match:
                return match.group(0).rstrip(".,;:)]}>'\"")
        current = current.__cause__ or current.__context__
    return ""


def _detail_failure_message(
    count: int,
    noun: str,
    invalid_urls: list[str],
) -> str:
    parts = [f"{count} 個{noun}暫時無法讀取"] if count else []
    if invalid_urls:
        parts.append(
            "官方頁輸出不允許的明細 URL，已拒絕並跳過："
            + "、".join(dict.fromkeys(invalid_urls))
        )
    return "；".join(parts)


def _invalid_url_alerts(
    source: dict[str, Any],
    invalid_urls: list[str],
) -> list[Alert]:
    return [
        Alert(
            "source_emitted_invalid_url",
            source["bank_name"],
            "官方活動頁輸出不符合安全規則的明細 URL；該筆已跳過，未放寬官方網域白名單。",
            url,
        )
        for url in dict.fromkeys(invalid_urls)
    ]


def _fetch_many(urls: list[str], domains: list[str], workers: int = 6) -> dict[str, Any]:
    def fetch_one(url: str) -> Any:
        try:
            return fetch_text(url, domains)
        except Exception as exc:
            # A single detail page must not abort every other source. The caller
            # classifies rejected URLs separately and records them in health/alerts.
            return exc

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(urls) or 1))) as executor:
        results = list(executor.map(fetch_one, urls))
    return dict(zip(urls, results, strict=True))


def _fetch_form_pages(
    url: str,
    domains: list[str],
    payloads: list[dict[str, str]],
    workers: int = 6,
) -> list[Any]:
    def fetch_one(data: dict[str, str]) -> Any:
        try:
            return fetch_text(url, domains, data=data)
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(payloads) or 1))) as executor:
        return list(executor.map(fetch_one, payloads))


def extract_ctbc(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
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
            detail_target = re.search(r'data-target="(#[^"]+)"', block)
            public_url = result.final_url.split("#", 1)[0] + (detail_target.group(1) if detail_target else "")
            activity_id = _stable_id(
                source["id"],
                f"{public_url}|{title}|{start.isoformat()}|{summary}",
            )
            fingerprint = source_fingerprint(source["id"], block)
            cached = reuse_cached_promotion(
                cache,
                activity_id=activity_id,
                fingerprint=fingerprint,
                now=now,
                source_entry_url=source["entry_url"],
                percent_threshold=percent_threshold,
                amount_threshold=amount_threshold,
                stats=stats,
                avoids_detail_request=False,
            )
            if cached:
                activities.append(cached)
                continue
            links = parse_page(block, result.final_url).links
            register_link = next(
                (link["url"] for link in links if "登錄" in link.get("text", "") and link["url"].startswith("https://")),
                "",
            )
            registration_text = _registration_excerpt(body_text)
            terms_raw, terms_sections = _terms_content(body_text)
            windows = _registration_windows(registration_text or body_text, start.year, start, end)
            registration_required = bool(register_link or windows or _has_registration_requirement(body_text))
            reward_percent, reward_amount = _reward_values(f"{title} {summary} {body_text}")
            high_return = (
                (reward_percent is not None and reward_percent >= percent_threshold)
                or (reward_amount is not None and reward_amount >= amount_threshold)
            )
            categories = _categories(body_text, category_names[category_id])
            activities.append(Promotion(
                id=activity_id,
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
                terms_sections=terms_sections,
                terms_raw=terms_raw,
                registration_url=register_link or (REGISTRATION_URL_DEFAULTS["ctbc"] if registration_required else ""),
                registration_windows=windows,
                max_reward_percent=reward_percent,
                max_reward_amount_twd=reward_amount,
                high_return=high_return,
                featured=registration_required or high_return,
                lifecycle=_lifecycle(start, end, today),
                tags=list(dict.fromkeys([title, source["bank_name"], *categories])),
                review_required=registration_required and not windows,
                source_fingerprint=fingerprint,
                last_detail_checked_at=checked_at,
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
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
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
        title = strip_html(title_match.group(1))
        summary = strip_html(paragraphs[0]) if paragraphs else title
        cards.append({
            "url": public_url,
            "id": _stable_id(source["id"], public_url),
            "title": title,
            "summary": summary,
            "start": start,
            "end": end,
            "fingerprint": source_fingerprint(
                source["id"],
                {
                    "url": public_url,
                    "title": title,
                    "summary": summary,
                    "period": compact.group(1),
                },
            ),
        })

    urls_to_fetch: list[str] = []
    for card in cards:
        card["cached"] = reuse_cached_promotion(
            cache,
            activity_id=card["id"],
            fingerprint=card["fingerprint"],
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if not card["cached"]:
            urls_to_fetch.append(card["url"])
    record_detail_requests(stats, len(urls_to_fetch))
    fetched = _fetch_many(urls_to_fetch, source["official_domains"])
    activities: list[Promotion] = []
    failed_details = 0
    invalid_urls: list[str] = []
    for card in cards:
        if card["cached"]:
            cached = card["cached"]
            cached_start = _date_from_iso(cached.start_date) or today
            cached_end = _date_from_iso(cached.end_date)
            cached.registration_windows = _registration_windows(
                cached.registration_text,
                cached_start.year,
                cached_start,
                cached_end,
            )
            cached.registration_required = bool(
                cached.registration_windows
                or _has_registration_requirement(cached.registration_text)
            )
            cached.review_required = (
                cached.registration_required and not cached.registration_windows
            )
            cached.featured = cached.registration_required or cached.high_return
            activities.append(cached)
            continue
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            invalid_url = _invalid_detail_url(result)
            if invalid_url:
                invalid_urls.append(invalid_url)
                continue
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        start, end = _official_detail_period(text, card["start"], card["end"], today)
        registration_text = _registration_excerpt(text)
        terms_raw, terms_sections = _terms_content(text)
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
        promotion = Promotion(
            id=card["id"],
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
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            reward_tiers=_reward_tiers(text),
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([card["title"], source["bank_name"], *categories])),
            review_required=registration_required and not windows,
            source_fingerprint=card["fingerprint"],
            last_detail_checked_at=checked_at,
        )
        _promotion_invariants(promotion)
        activities.append(promotion)

    status = "complete" if activities and failed_details == 0 and not alerts else ("partial" if activities else "failed")
    message = _detail_failure_message(failed_details, "官方活動明細", invalid_urls)
    alerts.extend(_invalid_url_alerts(source, invalid_urls))
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
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    alerts: list[Alert] = []
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
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
        summary = _class_text(block, "sub_title") or title
        type_tag = type_match.group(1) if type_match else ""
        cards.append({
            "url": public_url,
            "id": _stable_id(source["id"], public_url),
            "title": title,
            "summary": summary,
            "type": type_tag,
            "start": start,
            "end": end,
            "fingerprint": source_fingerprint(
                source["id"],
                {
                    "url": public_url,
                    "title": title,
                    "summary": summary,
                    "type": type_tag,
                    "period": period_text,
                },
            ),
        })

    urls_to_fetch = []
    for card in cards:
        card["cached"] = reuse_cached_promotion(
            cache,
            activity_id=card["id"],
            fingerprint=card["fingerprint"],
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if not card["cached"]:
            urls_to_fetch.append(card["url"])
    record_detail_requests(stats, len(urls_to_fetch))
    fetched = _fetch_many(urls_to_fetch, source["official_domains"])
    type_categories = {
        "mobilepay": "行動支付",
        "travel": "旅遊交通",
        "installment": "分期",
        "card": "百貨購物",
        "cdcard": "其他優惠",
    }
    activities: list[Promotion] = []
    failed_details = 0
    invalid_urls: list[str] = []
    for card in cards:
        if card["cached"]:
            activities.append(card["cached"])
            continue
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            invalid_url = _invalid_detail_url(result)
            if invalid_url:
                invalid_urls.append(invalid_url)
                continue
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        start, end = _official_detail_period(text, card["start"], card["end"], today)
        registration_text = _registration_excerpt(text)
        terms_raw, terms_sections = _terms_content(text)
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
        promotion = Promotion(
            id=card["id"],
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
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            reward_tiers=_reward_tiers(text),
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([card["title"], source["bank_name"], *categories])),
            review_required=registration_required and not windows,
            source_fingerprint=card["fingerprint"],
            last_detail_checked_at=checked_at,
        )
        _promotion_invariants(promotion)
        activities.append(promotion)

    status = "complete" if activities and failed_details == 0 and not alerts else ("partial" if activities else "failed")
    message = _detail_failure_message(failed_details, "官方活動明細", invalid_urls)
    alerts.extend(_invalid_url_alerts(source, invalid_urls))
    if alerts:
        message = f"{message} 使用者指定入口需留意。".strip()
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), alerts


def _period_from_text(text: str, today: date) -> tuple[date, date | None]:
    parsed = _parse_period(text, today.year)
    if parsed:
        return parsed
    open_ended = re.search(
        r"即日起\s*(?:[~～－–—-]|至)\s*(20\d{2})[年/](\d{1,2})[月/](\d{1,2})",
        text,
    )
    if open_ended:
        try:
            return today, date(
                int(open_ended.group(1)),
                int(open_ended.group(2)),
                int(open_ended.group(3)),
            )
        except ValueError:
            pass
    return today, None


def extract_obank(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed", source["bank_name"],
            "王道銀行指定入口暫時無法讀取；未改用其他產品分頁。",
            source["entry_url"],
        )]

    debit_start = re.search(
        r'<div[^>]+id="[^"]*Content_divContent_1"[^>]*>',
        listing.text,
        flags=re.I,
    )
    debit_end = re.search(
        r'<div[^>]+id="[^"]*Content_divContent_2"[^>]*>',
        listing.text,
        flags=re.I,
    )
    if not debit_start or not debit_end or debit_end.start() <= debit_start.start():
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], listing.final_url,
            "failed", 0, checked_at,
            "找不到簽帳金融卡專屬內容容器 Content_divContent_1。",
        ), [Alert(
            "source_structure_changed", source["bank_name"],
            "王道銀行頁面仍存在，但簽帳金融卡分頁結構已變更；為避免誤收存錢、貸款或投資理財活動，本次停止擷取。",
            source["entry_url"],
        )]

    debit_html = listing.text[debit_start.start():debit_end.start()]
    blocks = re.findall(
        r'<article[^>]+class="[^"]*\bo-article\b[^"]*"[^>]*>.*?</article>',
        debit_html,
        flags=re.I | re.S,
    )
    activities: list[Promotion] = []
    for block in blocks:
        title = _class_text(block, "heading")
        description = _class_text(block, "description")
        if not title:
            continue
        body_text = clean_inline(f"{title} {description}")
        start, end = _period_from_text(body_text, today)
        if end and end < today:
            continue
        page = parse_page(block, listing.final_url)
        detail_url = next(
            (
                link["url"]
                for link in page.links
                if "/retail/event/event-announce/" in link["url"]
            ),
            listing.final_url,
        )
        activity_id = _stable_id(source["id"], detail_url if detail_url != listing.final_url else title)
        fingerprint = source_fingerprint(source["id"], block)
        cached = reuse_cached_promotion(
            cache,
            activity_id=activity_id,
            fingerprint=fingerprint,
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=False,
        )
        if cached:
            cached_start = _date_from_iso(cached.start_date) or today
            cached_end = _date_from_iso(cached.end_date)
            cached.registration_windows = _registration_windows(
                cached.registration_text,
                cached_start.year,
                cached_start,
                cached_end,
            )
            cached.registration_required = bool(
                cached.registration_windows
                or _has_registration_requirement(cached.registration_text)
            )
            cached.review_required = (
                cached.registration_required and not cached.registration_windows
            )
            cached.featured = cached.registration_required or cached.high_return
            activities.append(cached)
            continue
        registration_text = _registration_excerpt(description)
        terms_raw, terms_sections = _terms_content(description)
        windows = _registration_windows(registration_text or description, start.year, start, end)
        registration_required = bool(windows or _has_registration_requirement(description))
        reward_percent, reward_amount = _reward_values(body_text)
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(body_text, None)
        registration_url = (
            _registration_url(page.links, "obank")
            if registration_required
            else ""
        )
        if registration_required and registration_url == REGISTRATION_URL_DEFAULTS["obank"]:
            registration_url = detail_url
        activities.append(Promotion(
            id=activity_id,
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=title,
            merchant=title,
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=(description or title)[:700],
            source_url=detail_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([title, source["bank_name"], "簽帳金融卡", *categories])),
            review_required=registration_required and not windows,
            source_fingerprint=fingerprint,
            last_detail_checked_at=checked_at,
        ))

    status = "complete" if activities else "failed"
    message = (
        "僅讀取簽帳金融卡分頁 Content_divContent_1；已排除存錢、貸款及投資理財分頁。"
        if activities
        else "簽帳金融卡分頁目前沒有可辨識且未結束的活動。"
    )
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), []


def _yuanta_cards(html: str, base_url: str, bank_id: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for block in re.findall(
        r'<li>\s*<div[^>]+class="pic"[^>]*>.*?</li>',
        html,
        flags=re.I | re.S,
    ):
        href = re.search(
            r'href="([^"]*/promotionActivity/in\.do\?id=[^"]+)"',
            block,
            flags=re.I,
        )
        title_match = re.search(r"<h6[^>]*>(.*?)</h6>", block, flags=re.I | re.S)
        if not href or not title_match:
            continue
        public_url = urljoin(base_url, href.group(1))
        title = strip_html(title_match.group(1))
        summary_match = re.search(r"<p[^>]*>(.*?)</p>", block, flags=re.I | re.S)
        summary = strip_html(summary_match.group(1)) if summary_match else title
        cards.append({
            "url": public_url,
            "id": _stable_id(bank_id, public_url),
            "title": title,
            "summary": summary,
            "fingerprint": source_fingerprint(bank_id, block),
        })
    return cards


def extract_yuanta(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
    try:
        first_page = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed", source["bank_name"],
            "元大銀行優惠活動清單暫時無法讀取。",
            source["entry_url"],
        )]

    page_count_match = re.search(r'name="pA"\s+value="(\d+)"', first_page.text, flags=re.I)
    item_count_match = re.search(r'name="iA"\s+value="(\d+)"', first_page.text, flags=re.I)
    page_count = max(1, min(int(page_count_match.group(1)) if page_count_match else 1, 30))
    item_count = int(item_count_match.group(1)) if item_count_match else 0
    page_results: list[Any] = [first_page]
    if page_count > 1:
        payloads = [
            {"pN": str(page), "pA": str(page_count), "iA": str(item_count)}
            for page in range(2, page_count + 1)
        ]
        page_results.extend(
            _fetch_form_pages(
                source["entry_url"],
                source["official_domains"],
                payloads,
                workers=4,
            )
        )

    failed_listing_pages = sum(isinstance(result, Exception) for result in page_results)
    cards: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result in page_results:
        if isinstance(result, Exception):
            continue
        for card in _yuanta_cards(result.text, result.final_url, source["id"]):
            if card["url"] not in seen_urls:
                seen_urls.add(card["url"])
                cards.append(card)

    urls_to_fetch: list[str] = []
    for card in cards:
        card["cached"] = reuse_cached_promotion(
            cache,
            activity_id=card["id"],
            fingerprint=card["fingerprint"],
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if not card["cached"]:
            urls_to_fetch.append(card["url"])
    record_detail_requests(stats, len(urls_to_fetch))
    fetched = _fetch_many(urls_to_fetch, source["official_domains"], workers=8)

    activities: list[Promotion] = []
    failed_details = 0
    invalid_urls: list[str] = []
    for card in cards:
        if card["cached"]:
            cached = card["cached"]
            cached_start = _date_from_iso(cached.start_date) or today
            cached_end = _date_from_iso(cached.end_date)
            cached.registration_windows = _registration_windows(
                cached.registration_text,
                cached_start.year,
                cached_start,
                cached_end,
            )
            cached.registration_required = bool(
                cached.registration_windows
                or _has_registration_requirement(cached.registration_text)
            )
            cached.review_required = (
                cached.registration_required and not cached.registration_windows
            )
            cached.featured = cached.registration_required or cached.high_return
            if cached.lifecycle != "ended":
                activities.append(cached)
            continue
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            invalid_url = _invalid_detail_url(result)
            if invalid_url:
                invalid_urls.append(invalid_url)
                continue
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        start, end = _period_from_text(text, today)
        if end and end < today:
            continue
        registration_text = _registration_excerpt(text)
        terms_raw, terms_sections = _terms_content(text)
        windows = _registration_windows(registration_text or text, start.year, start, end)
        registration_required = bool(windows or _has_registration_requirement(text))
        reward_percent, reward_amount = _reward_values(
            f"{card['title']} {card['summary']} {text[:6000]}"
        )
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(
            f"{card['title']} {card['summary']} {text[:2200]}",
            None,
        )
        registration_url = (
            _registration_url(page.links, "yuanta")
            if registration_required and page
            else (REGISTRATION_URL_DEFAULTS["yuanta"] if registration_required else "")
        )
        activities.append(Promotion(
            id=card["id"],
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
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([card["title"], source["bank_name"], *categories])),
            review_required=registration_required and not windows,
            source_fingerprint=card["fingerprint"],
            last_detail_checked_at=checked_at,
        ))

    status = (
        "complete"
        if activities and failed_listing_pages == 0 and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if failed_listing_pages:
        issues.append(f"{failed_listing_pages} 個清單分頁暫時無法讀取")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "活動明細", invalid_urls))
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], first_page.final_url,
        status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


def _esun_cards(html: str, base_url: str, bank_id: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for block in _html_segments(html, r'<div[^>]+class="[^"]*\bpaginationList\b'):
        href = re.search(r'href="([^"]*shopInfo\?sno=[^"]+)"', block, flags=re.I)
        title_match = re.search(
            r'class="[^"]*l-cardDiscountAllContent__discount--title[^"]*"[^>]*>(.*?)</p>',
            block,
            flags=re.I | re.S,
        )
        if not href or not title_match:
            continue
        summary_match = re.search(
            r'class="[^"]*l-cardDiscountAllContent__discount--word[^"]*"[^>]*>(.*?)</p>',
            block,
            flags=re.I | re.S,
        )
        public_url = urljoin(base_url, href.group(1))
        title = strip_html(title_match.group(1))
        summary = strip_html(summary_match.group(1)) if summary_match else title
        cards.append({
            "url": public_url,
            "id": _stable_id(bank_id, public_url),
            "title": title,
            "summary": summary,
            "fingerprint": source_fingerprint(
                bank_id,
                {"url": public_url, "title": title, "summary": summary},
            ),
        })
    return cards


def extract_esun(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
    try:
        entry = fetch_text(source["entry_url"], source["official_domains"])
        api_first = fetch_text(
            source["data_url"],
            source["official_domains"],
            data={
                "itemID": source["item_id"],
                "rootUrl": source["detail_root"],
                "currentPage": "1",
                "category": "",
                "keywords": "",
            },
        )
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed", source["bank_name"],
            "玉山銀行刷卡優惠入口或公開優惠 API 暫時無法讀取。",
            source["entry_url"],
        )]

    total_match = re.search(r'id="total"\s+value="(\d+)"', api_first.text, flags=re.I)
    total = int(total_match.group(1)) if total_match else 0
    page_size = max(1, len(_esun_cards(api_first.text, entry.final_url, source["id"])))
    page_count = max(1, min((total + page_size - 1) // page_size if total else 1, 60))
    page_results: list[Any] = [api_first]
    if page_count > 1:
        payloads = [
            {
                "itemID": source["item_id"],
                "rootUrl": source["detail_root"],
                "currentPage": str(page),
                "category": "",
                "keywords": "",
            }
            for page in range(2, page_count + 1)
        ]
        page_results.extend(
            _fetch_form_pages(
                source["data_url"],
                source["official_domains"],
                payloads,
                workers=8,
            )
        )

    failed_listing_pages = sum(isinstance(result, Exception) for result in page_results)
    cards: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result in page_results:
        if isinstance(result, Exception):
            continue
        for card in _esun_cards(result.text, entry.final_url, source["id"]):
            if card["url"] not in seen_urls:
                seen_urls.add(card["url"])
                cards.append(card)

    urls_to_fetch: list[str] = []
    for card in cards:
        cached_ids = [
            activity_id
            for activity_id, item in cache.items()
            if item.get("parent_activity_id") == card["id"]
        ] or ([card["id"]] if card["id"] in cache else [])
        cached_items = [
            value
            for activity_id in cached_ids
            if (value := reuse_cached_promotion(
                cache,
                activity_id=activity_id,
                fingerprint=card["fingerprint"],
                now=now,
                source_entry_url=source["entry_url"],
                percent_threshold=percent_threshold,
                amount_threshold=amount_threshold,
                stats=stats,
                avoids_detail_request=True,
            )) is not None
        ]
        card["cached"] = cached_items if cached_ids and len(cached_items) == len(cached_ids) else []
        if not card["cached"]:
            urls_to_fetch.append(card["url"])
    record_detail_requests(stats, len(urls_to_fetch))
    fetched = _fetch_many(urls_to_fetch, source["official_domains"], workers=10)

    activities: list[Promotion] = []
    failed_details = 0
    invalid_urls: list[str] = []
    for card in cards:
        if card["cached"]:
            for cached in card["cached"]:
                _promotion_invariants(cached)
                if cached.lifecycle != "ended":
                    activities.append(cached)
            continue
        result = fetched[card["url"]]
        if isinstance(result, Exception):
            failed_details += 1
            invalid_url = _invalid_detail_url(result)
            if invalid_url:
                invalid_urls.append(invalid_url)
                continue
            page = None
            text = f"{card['title']} {card['summary']}"
            public_url = card["url"]
        else:
            page = parse_page(result.text, result.final_url)
            text = page.text
            public_url = result.final_url
        parent_start, parent_end = _period_from_text(text, today)
        blocks = _subactivity_blocks(text)
        candidates = blocks or [(card["title"], text)]
        for heading, activity_text in candidates:
            start, end = _subactivity_period(
                activity_text, parent_start, parent_end, today,
            )
            if end and end < today:
                continue
            registration_text = _registration_excerpt(activity_text)
            terms_raw, terms_sections = _terms_content(activity_text)
            windows = _registration_windows(
                registration_text or activity_text, start.year, start, end,
            )
            registration_required = bool(
                windows or _has_registration_requirement(activity_text)
            )
            title = f"{card['title']}｜{heading}" if blocks else card["title"]
            reward_text = (
                f"{title} {card['summary']} {activity_text[:4500]}"
                if blocks else f"{card['title']} {card['summary']}"
            )
            reward_percent, reward_amount = _reward_values(reward_text)
            high_return = (
                (reward_percent is not None and reward_percent >= percent_threshold)
                or (reward_amount is not None and reward_amount >= amount_threshold)
            )
            categories = _categories(
                f"{title} {card['summary']} {activity_text[:2200]}", None,
            )
            registration_url = (
                _registration_url(page.links, "esun")
                if registration_required and page
                else (public_url if registration_required else "")
            )
            promotion = Promotion(
                id=(
                    _stable_id(source["id"], f"{public_url}|{heading}|{start.isoformat()}")
                    if blocks else card["id"]
                ),
                parent_activity_id=card["id"],
                activity_periods=[
                    {"start": item["start"].isoformat(), "end": item["end"].isoformat(), "label": item["label"]}
                    for item in _activity_periods(activity_text, start.year)
                ],
                bank_id=source["id"], bank_name=source["bank_name"],
                title=title, merchant=card["title"], categories=categories,
                start_date=start.isoformat(), end_date=_date_text(end),
                summary=(clean_inline(activity_text.split("\n", 2)[-1]) or card["summary"])[:700],
                source_url=public_url, source_entry_url=source["entry_url"],
                observed_at=checked_at,
                registration_required=registration_required,
                registration_text=registration_text,
                terms_sections=terms_sections, terms_raw=terms_raw,
                registration_url=registration_url, registration_windows=windows,
                reward_tiers=_reward_tiers(activity_text),
                max_reward_percent=reward_percent,
                max_reward_amount_twd=reward_amount,
                high_return=high_return,
                featured=registration_required or high_return,
                lifecycle=_lifecycle(start, end, today),
                tags=list(dict.fromkeys([card["title"], heading, source["bank_name"], *categories])),
                review_required=registration_required and not windows,
                source_fingerprint=card["fingerprint"],
                last_detail_checked_at=checked_at,
            )
            _promotion_invariants(promotion)
            activities.append(promotion)

    status = (
        "complete"
        if activities and failed_listing_pages == 0 and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if failed_listing_pages:
        issues.append(f"{failed_listing_pages} 個 API 清單分頁暫時無法讀取")
    if failed_details:
        issues.append(_detail_failure_message(
            failed_details,
            "優惠明細轉往非玉山網域或",
            invalid_urls,
        ))
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], entry.final_url,
        status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


def _roc_period(value: str, today: date) -> tuple[date, date | None]:
    normalized = clean_inline(value)
    match = re.search(
        r"(?:(\d{3})/)?(\d{1,2})/(\d{1,2})\s*(?:[~～－–—-]|至)\s*"
        r"(?:(\d{3})/)?(\d{1,2})/(\d{1,2})",
        normalized,
    )
    if match:
        start_year = int(match.group(1) or str(today.year - 1911)) + 1911
        end_year = int(match.group(4) or str(start_year - 1911)) + 1911
        try:
            return (
                date(start_year, int(match.group(2)), int(match.group(3))),
                date(end_year, int(match.group(5)), int(match.group(6))),
            )
        except ValueError:
            pass
    end_only = re.search(r"即日起\s*(?:[~～－–—-]|至)\s*(\d{3})/(\d{1,2})/(\d{1,2})", normalized)
    if end_only:
        try:
            return today, date(
                int(end_only.group(1)) + 1911,
                int(end_only.group(2)),
                int(end_only.group(3)),
            )
        except ValueError:
            pass
    return today, None


def extract_sunny(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
    entry = None
    access_error = ""
    try:
        entry = fetch_text(source["entry_url"], source["official_domains"], attempts=1)
    except RuntimeError as exc:
        access_error = str(exc)

    activities: list[Promotion] = []
    for item in source.get("fallback_activities", []):
        title = item["title"]
        public_url = item.get("url") or source["entry_url"]
        start, end = _roc_period(item.get("period", ""), today)
        if end and end < today:
            continue
        activity_id = _stable_id(source["id"], f"{public_url}|{title}")
        fingerprint = source_fingerprint(source["id"], item)
        cached = reuse_cached_promotion(
            cache,
            activity_id=activity_id,
            fingerprint=fingerprint,
            now=now,
            source_entry_url=source["entry_url"],
            percent_threshold=percent_threshold,
            amount_threshold=amount_threshold,
            stats=stats,
            avoids_detail_request=True,
        )
        if cached:
            activities.append(cached)
            continue
        registration_required = bool(item.get("registration_required"))
        registration_text = item.get("registration_text", "")
        terms_raw, terms_sections = _terms_content(
            f"{title}\n{item.get('summary', '')}\n{registration_text}"
        )
        windows = _registration_windows(registration_text, start.year, start, end)
        text = f"{title} {item.get('summary', '')}"
        reward_percent, reward_amount = _reward_values(text)
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(text, None)
        activities.append(Promotion(
            id=activity_id,
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=title,
            merchant=title,
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=item.get("summary", title)[:700],
            source_url=public_url,
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            registration_url=(
                item.get("registration_url")
                or (REGISTRATION_URL_DEFAULTS["sunny"] if registration_required else "")
            ),
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([title, source["bank_name"], *categories])),
            review_required=registration_required and not windows,
            source_fingerprint=fingerprint,
            last_detail_checked_at=checked_at,
        ))

    if access_error:
        message = (
            "指定入口存在，但 Cloudflare 對自動化請求回傳 403；"
            "本次沿用已驗證的官方索引快照，待官方解除限制後恢復即時讀取。"
        )
        alert = Alert(
            "source_access_blocked",
            source["bank_name"],
            message,
            source["entry_url"],
        )
        status = "partial" if activities else "failed"
        return activities, SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            status, len(activities), checked_at, message,
        ), [alert]

    message = (
        "官方入口可讀取；活動內容使用已驗證的官方索引快照，"
        "入口內容變動時將列入人工覆核。"
    )
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        entry.final_url if entry else source["entry_url"],
        "partial" if activities else "failed", len(activities), checked_at, message,
    ), [Alert(
        "source_review_required",
        source["bank_name"],
        "陽信活動入口可讀取，但目前仍使用官方索引快照；請覆核是否有新增活動。",
        source["entry_url"],
    )]


def _end_date_from_text(value: str, today: date) -> date | None:
    normalized = _normalize_roc_dates(value)
    match = re.search(
        r"(?:至|到)\s*(20\d{2})年(\d{1,2})月(\d{1,2})日",
        normalized,
    )
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _tcbbank_cards(
    html: str,
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for block in _html_segments(html, r'<div\s+class="admin-btn"'):
        title = _class_text(block, "img_btn_text")
        period_text = _class_text(block, "sm_p")
        href = re.search(
            r'<a[^>]+href="([^"]*/Promotion/Info\.html\?ID=\d+)"',
            block,
            flags=re.I,
        )
        if not title or not href:
            continue
        public_url = urljoin(base_url, href.group(1))
        key = (title, public_url)
        if key in seen:
            continue
        seen.add(key)
        end = _end_date_from_text(period_text, today)
        cards.append({
            "id": _stable_id(bank_id, f"{public_url}|{title}"),
            "title": title,
            "summary": period_text or title,
            "url": public_url,
            "start": today,
            "end": end,
            "fingerprint": source_fingerprint(
                bank_id,
                {"title": title, "period": period_text, "url": public_url},
            ),
            "fetch_detail": True,
        })
    return cards


def _tcbbank_api_cards(
    rows: list[dict[str, Any]],
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in rows:
        title = clean_inline(str(item.get("Title") or ""))
        summary = clean_inline(str(item.get("SubTitle") or ""))
        raw_url = clean_inline(str(item.get("Url") or ""))
        if not title or not raw_url:
            continue
        public_url = urljoin(base_url, raw_url)
        key = (title, public_url)
        if key in seen:
            continue
        seen.add(key)
        try:
            start = datetime.fromisoformat(str(item.get("StartDate") or "")).date()
        except ValueError:
            start = today
        end = _end_date_from_text(summary, today)
        cards.append({
            "id": _stable_id(bank_id, f"{public_url}|{title}"),
            "title": title,
            "summary": summary or title,
            "url": public_url,
            "start": start,
            "end": end,
            "fingerprint": source_fingerprint(
                bank_id,
                {
                    "title": title,
                    "summary": summary,
                    "url": public_url,
                    "start": item.get("StartDate"),
                    "end": item.get("EndDate"),
                },
            ),
            "fetch_detail": True,
        })
    return cards


def _kgi_cards(
    html: str,
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for block in _html_segments(
        html,
        r'<div\s+class="fs-h3 fs-lg-h3 color-primary-blue"',
    ):
        title = _class_text(block, "color-primary-blue")
        summary = _class_text(block, "kgibStatic011__item-title")
        href = re.search(
            r'<a[^>]+href="([^"]*/personal/promotion/card-campaign/[^"?#]+)"',
            block,
            flags=re.I,
        )
        if not title or not href:
            continue
        public_url = urljoin(base_url, href.group(1))
        period = _parse_period(summary, today.year)
        start, end = period if period else (today, None)
        cards.append({
            "id": _stable_id(bank_id, f"{public_url}|{title}"),
            "title": title,
            "summary": summary or title,
            "url": public_url,
            "start": start,
            "end": end,
            "fingerprint": source_fingerprint(
                bank_id,
                {"title": title, "summary": summary, "url": public_url},
            ),
            "fetch_detail": True,
        })
    return cards


def _hncb_credit_card_cards(
    html: str,
    base_url: str,
    bank_id: str,
    official_domains: list[str],
    today: date,
) -> tuple[list[dict[str, Any]], str]:
    tab_match = re.search(
        r'<a[^>]+aria-controls="([^"]+)"[^>]+role="tab"[^>]+aria-label="信用卡"[^>]*>',
        html,
        flags=re.I,
    )
    if not tab_match:
        tab_match = re.search(
            r'<a[^>]+role="tab"[^>]+aria-label="信用卡"[^>]+aria-controls="([^"]+)"[^>]*>',
            html,
            flags=re.I,
        )
    if not tab_match:
        return [], ""
    panel_id = tab_match.group(1)
    scope = next(
        (
            segment
            for segment in _html_segments(
                html,
                r'<div\s+class="tab-pane[^"]*"',
            )
            if re.search(
                rf'<div[^>]+id="{re.escape(panel_id)}"[^>]+role="tabpanel"',
                segment[:1000],
                flags=re.I,
            )
        ),
        "",
    )
    if not scope:
        return [], panel_id

    cards: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for match in re.finditer(
        r'<div\s+class="feature-title"[^>]*>(.*?)</div>',
        scope,
        flags=re.I | re.S,
    ):
        anchor = re.search(
            r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            match.group(1),
            flags=re.I | re.S,
        )
        if not anchor:
            continue
        title = strip_html(anchor.group(2))
        public_url = urljoin(base_url, clean_inline(anchor.group(1)).lstrip("+"))
        if not title or not public_url.startswith("https://"):
            continue
        key = (title, public_url)
        if key in seen:
            continue
        seen.add(key)
        cards.append({
            "id": _stable_id(bank_id, f"{public_url}|{title}"),
            "title": title,
            "summary": title,
            "url": public_url,
            "start": today,
            "end": None,
            "fingerprint": source_fingerprint(
                bank_id,
                {"panel": panel_id, "title": title, "url": public_url},
            ),
            "fetch_detail": is_allowed_url(public_url, official_domains),
        })
    return cards, panel_id


def _listing_promotions(
    source: dict[str, Any],
    cards: list[dict[str, Any]],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None,
    cache_stats: dict[str, Any] | None,
) -> tuple[list[Promotion], int, list[str]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cache = activity_cache or {}
    stats = cache_stats if cache_stats is not None else {}
    urls_to_fetch: list[str] = []

    for card in cards:
        cached_ids = [
            activity_id
            for activity_id, item in cache.items()
            if item.get("parent_activity_id") == card["id"]
        ] or ([card["id"]] if card["id"] in cache else [])
        cached_items = [
            value
            for activity_id in cached_ids
            if (value := reuse_cached_promotion(
                cache,
                activity_id=activity_id,
                fingerprint=card["fingerprint"],
                now=now,
                source_entry_url=source["entry_url"],
                percent_threshold=percent_threshold,
                amount_threshold=amount_threshold,
                stats=stats,
                avoids_detail_request=bool(card["fetch_detail"]),
            )) is not None
        ]
        card["cached"] = cached_items if cached_ids and len(cached_items) == len(cached_ids) else []
        if not card["cached"] and card["fetch_detail"]:
            urls_to_fetch.append(card["url"])

    unique_urls = list(dict.fromkeys(urls_to_fetch))
    record_detail_requests(stats, len(unique_urls))
    fetched = _fetch_many(unique_urls, source["official_domains"], workers=6)
    activities: list[Promotion] = []
    failed_details = 0
    invalid_urls: list[str] = []

    for card in cards:
        if card["cached"]:
            for cached in card["cached"]:
                if card.get("featured"):
                    cached.featured = True
                _promotion_invariants(cached)
                activities.append(cached)
            continue
        page = None
        text = f"{card['title']} {card['summary']}"
        result = fetched.get(card["url"])
        if card["fetch_detail"]:
            if isinstance(result, Exception) or result is None:
                failed_details += 1
                invalid_url = _invalid_detail_url(result) if isinstance(result, Exception) else ""
                if invalid_url:
                    invalid_urls.append(invalid_url)
                    continue
            else:
                page = parse_page(result.text, result.final_url)
                text = page.text
        start, end = _official_detail_period(
            text,
            card["start"],
            card["end"],
            today,
        )
        if end and end < today:
            continue
        summary = card["summary"]
        if summary == card["title"] and page and page.headings:
            summary = page.headings
        registration_text = (
            _registration_excerpt(text)
            or clean_inline(str(card.get("registration_text") or ""))
        )
        terms_raw, terms_sections = _terms_content(text)
        windows = _registration_windows(
            registration_text or text,
            start.year,
            start,
            end,
        )
        registration_required = bool(
            card.get("registration_required")
            or windows
            or _has_registration_requirement(text)
            or _has_registration_requirement(summary)
        )
        reward_percent, reward_amount = _reward_values(
            f"{card['title']} {summary} {text[:4500]}"
        )
        high_return = (
            (reward_percent is not None and reward_percent >= percent_threshold)
            or (reward_amount is not None and reward_amount >= amount_threshold)
        )
        categories = _categories(
            f"{card['title']} {summary} {text[:2200]}",
            clean_inline(str(card.get("base_category") or "")) or None,
        )
        if card.get("featured_category"):
            categories.append(clean_inline(str(card["featured_category"])))
            categories = list(dict.fromkeys(categories))
        registration_url = ""
        if registration_required:
            registration_url = (
                _registration_url(page.links, source["id"])
                if page
                else REGISTRATION_URL_DEFAULTS[source["id"]]
            )
        promotion = Promotion(
            id=card["id"],
            bank_id=source["id"],
            bank_name=source["bank_name"],
            title=card["title"],
            merchant=card["title"],
            categories=categories,
            start_date=start.isoformat(),
            end_date=_date_text(end),
            summary=summary[:700],
            source_url=card["url"],
            source_entry_url=source["entry_url"],
            observed_at=checked_at,
            registration_required=registration_required,
            registration_text=registration_text,
            terms_sections=terms_sections,
            terms_raw=terms_raw,
            reward_tiers=_reward_tiers(text),
            registration_url=registration_url,
            registration_windows=windows,
            max_reward_percent=reward_percent,
            max_reward_amount_twd=reward_amount,
            high_return=high_return,
            featured=bool(card.get("featured")) or registration_required or high_return,
            lifecycle=_lifecycle(start, end, today),
            tags=list(dict.fromkeys([
                card["title"],
                source["bank_name"],
                *(
                    [clean_inline(str(card["official_category"]))]
                    if card.get("official_category")
                    else []
                ),
                *categories,
            ])),
            review_required=registration_required and not windows,
            source_fingerprint=card["fingerprint"],
            last_detail_checked_at=checked_at,
        )
        blocks = _subactivity_blocks(text)
        if blocks and source["id"] == "ubot":
            for heading, activity_text in blocks:
                child = Promotion.from_dict(promotion.to_dict())
                child.id = _stable_id(
                    source["id"], f"{card['url']}|{heading}",
                )
                child.parent_activity_id = card["id"]
                child.activity_periods = [
                    {"start": item["start"].isoformat(), "end": item["end"].isoformat(), "label": item["label"]}
                    for item in _activity_periods(activity_text, start.year)
                ]
                child.title = f"{card['title']}｜{heading}"
                child.start_date, child.end_date = (
                    value.isoformat() if value else None
                    for value in _subactivity_period(
                        activity_text, start, end, today,
                    )
                )
                child.summary = clean_inline(activity_text)[:700]
                child.registration_text = _registration_excerpt(activity_text)
                child.registration_windows = _registration_windows(
                    child.registration_text or activity_text,
                    (_date_from_iso(child.start_date) or today).year,
                    _date_from_iso(child.start_date),
                    _date_from_iso(child.end_date),
                )
                child.registration_required = bool(
                    child.registration_windows
                    or _has_registration_requirement(activity_text)
                )
                child.terms_raw, child.terms_sections = _terms_content(activity_text)
                child.reward_tiers = _reward_tiers(activity_text)
                child.max_reward_percent, child.max_reward_amount_twd = _reward_values(activity_text)
                child.high_return = bool(
                    (child.max_reward_percent is not None and child.max_reward_percent >= percent_threshold)
                    or (child.max_reward_amount_twd is not None and child.max_reward_amount_twd >= amount_threshold)
                )
                child.featured = child.registration_required or child.high_return
                child.lifecycle = _lifecycle(
                    _date_from_iso(child.start_date) or today,
                    _date_from_iso(child.end_date),
                    today,
                )
                child.review_required = child.registration_required and not child.registration_windows
                _promotion_invariants(child)
                activities.append(child)
            continue
        if blocks:
            promotion.needs_review = True
            promotion.review_required = True
            promotion.review_message = (
                f"本頁含 {len(blocks)} 個活動，請至官方頁確認對應的登錄時間。"
            )
        _promotion_invariants(promotion)
        activities.append(promotion)
    return activities, failed_details, invalid_urls


def extract_tcbbank(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed",
            source["bank_name"],
            "台中銀行指定信用卡優惠入口暫時無法讀取。",
            source["entry_url"],
        )]
    try:
        _, payload = fetch_json(source["data_url"], source["official_domains"])
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], listing.final_url,
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed",
            source["bank_name"],
            "台中銀行信用卡入口存在，但其官方活動清單端點暫時無法讀取。",
            source["entry_url"],
        )]
    rows = payload.get("row", []) if isinstance(payload, dict) else []
    cards = [
        card
        for card in _tcbbank_api_cards(
            rows if isinstance(rows, list) else [],
            listing.final_url,
            source["id"],
            today,
        )
        if not card["end"] or card["end"] >= today
    ]
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = "complete" if activities and failed_details == 0 else ("partial" if activities else "failed")
    message = _detail_failure_message(failed_details, "官方活動明細", invalid_urls)
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), _invalid_url_alerts(source, invalid_urls)


def extract_kgi(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    pages = []
    seen_page_signatures: set[tuple[str, ...]] = set()
    failed_pages = 0
    for page_number in range(1, int(source.get("max_listing_pages", 10)) + 1):
        url = source["entry_url"] if page_number == 1 else f"{source['entry_url']}?p={page_number}"
        try:
            result = fetch_text(url, source["official_domains"])
        except RuntimeError:
            failed_pages += 1
            break
        cards = _kgi_cards(result.text, result.final_url, source["id"], today)
        signature = tuple(card["id"] for card in cards)
        if not cards or signature in seen_page_signatures:
            break
        seen_page_signatures.add(signature)
        pages.append((result, cards))
    if not pages:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, "凱基銀行信用卡活動清單暫時無法讀取。",
        ), [Alert(
            "source_failed",
            source["bank_name"],
            "凱基銀行指定信用卡活動入口暫時無法讀取。",
            source["entry_url"],
        )]

    cards = []
    seen_ids: set[str] = set()
    for _, page_cards in pages:
        for card in page_cards:
            if card["id"] in seen_ids:
                continue
            seen_ids.add(card["id"])
            if not card["end"] or card["end"] >= today:
                cards.append(card)
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = (
        "complete"
        if activities and failed_pages == 0 and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if failed_pages:
        issues.append(f"{failed_pages} 個清單分頁暫時無法讀取")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "官方活動明細", invalid_urls))
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], pages[0][0].final_url,
        status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


def extract_hncb(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed",
            source["bank_name"],
            "華南銀行指定熱門優惠入口暫時無法讀取；未改用其他分頁。",
            source["entry_url"],
        )]
    cards, panel_id = _hncb_credit_card_cards(
        listing.text,
        listing.final_url,
        source["id"],
        source["official_domains"],
        today,
    )
    if not panel_id or not cards:
        message = "找不到 aria-label=信用卡 對應的 tabpanel；為避免混入其他金融商品，本次停止擷取。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], listing.final_url,
            "failed", 0, checked_at, message,
        ), [Alert(
            "source_structure_changed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = "complete" if activities and failed_details == 0 else ("partial" if activities else "failed")
    message = (
        f"僅讀取信用卡 tab 對應容器 {panel_id}；已排除存款/外匯、基金/投資、保險、貸款與其他分頁。"
    )
    if failed_details:
        message += " " + _detail_failure_message(
            failed_details,
            "華南官方活動明細",
            invalid_urls,
        )
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"], listing.final_url,
        status, len(activities), checked_at, message,
    ), _invalid_url_alerts(source, invalid_urls)


def _fubon_cards(
    html: str,
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for block in _html_segments(html, r'<div\s+class="discount-card"'):
        title = _class_text(block, "discount-name")
        period_text = _class_text(block, "discount-date")
        summary = _class_text(block, "discount-text")
        href = re.search(
            r'<a[^>]+href="([^"]*Detail\?sn=[A-Z]\d+)"',
            block,
            flags=re.I,
        )
        if not title or not href:
            continue
        public_url = urljoin(base_url, href.group(1))
        period = _parse_period(period_text, today.year)
        start, end = period if period else (today, None)
        cards.append({
            "id": _stable_id(bank_id, public_url),
            "title": title,
            "summary": summary or period_text or title,
            "url": public_url,
            "start": start,
            "end": end,
            "fingerprint": source_fingerprint(
                bank_id,
                {
                    "title": title,
                    "period": period_text,
                    "summary": summary,
                    "url": public_url,
                },
            ),
            "fetch_detail": True,
        })
    return cards


def _fubon_has_next(html: str) -> bool:
    return bool(re.search(
        r'<a[^>]+href="[^"]*fmList-divSearchResult-nav-next[^"]*"',
        html,
        flags=re.I,
    ))


def _fubon_page_listeners(html: str, base_url: str) -> dict[int, str]:
    listeners: dict[int, str] = {}
    for match in re.finditer(
        r'<a[^>]+href="([^"]*fmList-divSearchResult-nav-navigation-'
        r'\d+-pageLink)"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        label = strip_html(match.group(2))
        if not label.isdigit():
            continue
        href = re.sub(r"(\d+-1)\.-", r"\1.0-", match.group(1))
        listeners[int(label)] = urljoin(base_url, href)
    return listeners


def _fubon_page_listener(html: str, base_url: str, page_number: int) -> str:
    return _fubon_page_listeners(html, base_url).get(page_number, "")


def _fubon_ajax_redirect(html: str, base_url: str) -> str:
    match = re.search(
        r"<redirect>\s*<!\[CDATA\[(.*?)\]\]>\s*</redirect>",
        html,
        flags=re.I | re.S,
    )
    return urljoin(base_url, match.group(1).strip()) if match else ""


def extract_taipei_fubon(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    with PersistentHTTPSession(
        source["official_domains"],
        user_agent=str(source.get("session_user_agent") or ""),
    ) as session:
        try:
            listing = session.fetch_text(source["entry_url"])
        except RuntimeError as exc:
            return [], SourceHealth(
                source["id"], source["bank_name"], source["entry_url"], "",
                "failed", 0, checked_at, str(exc),
            ), [Alert(
                "source_failed",
                source["bank_name"],
                "台北富邦指定卡友優惠入口暫時無法讀取。",
                source["entry_url"],
            )]

        first_cards = _fubon_cards(
            listing.text,
            listing.final_url,
            source["id"],
            today,
        )
        if not first_cards:
            message = "指定入口存在，但找不到卡友優惠清單；可能是官方頁面結構已變更。"
            return [], SourceHealth(
                source["id"], source["bank_name"], source["entry_url"],
                listing.final_url, "failed", 0, checked_at, message,
            ), [Alert(
                "source_structure_changed",
                source["bank_name"],
                message,
                source["entry_url"],
            )]

        cards = list(first_cards)
        seen_signatures = {tuple(card["id"] for card in first_cards)}
        page_html = listing.text
        page_failures = 0
        max_pages = int(source.get("max_listing_pages", 80))
        referer_url = listing.final_url
        cache_buster = int(datetime.now().timestamp() * 1000)
        for page_number in range(1, max_pages):
            if not _fubon_has_next(page_html):
                break
            page_cards: list[dict[str, Any]] = []
            target_page = page_number + 1
            for navigation_attempt in range(200):
                listeners = _fubon_page_listeners(
                    page_html,
                    listing.final_url,
                )
                listener_url = listeners.get(target_page, "")
                requested_page = target_page
                if not listener_url:
                    recovery_pages = [
                        value for value in listeners
                        if value < target_page
                    ]
                    if not recovery_pages:
                        break
                    requested_page = max(recovery_pages)
                    listener_url = listeners[requested_page]
                try:
                    result = session.fetch_text(
                        f"{listener_url}"
                        f"&_={cache_buster + page_number * 100 + navigation_attempt}",
                        headers={
                            "Accept": "application/xml, text/xml, */*; q=0.01",
                            "Cache-Control": "no-cache",
                            "Referer": referer_url,
                            "Wicket-Ajax": "true",
                            "Wicket-Ajax-BaseURL": "promotion/Result",
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                except RuntimeError:
                    break
                redirect_url = _fubon_ajax_redirect(
                    result.text,
                    listing.final_url,
                )
                if redirect_url:
                    try:
                        refreshed = session.fetch_text(redirect_url)
                    except RuntimeError:
                        break
                    page_html = refreshed.text
                    referer_url = refreshed.final_url
                    continue
                landed_cards = _fubon_cards(
                    result.text,
                    listing.final_url,
                    source["id"],
                    today,
                )
                if not landed_cards:
                    break
                page_html = result.text
                if requested_page == target_page:
                    page_cards = landed_cards
                    break
            signature = tuple(card["id"] for card in page_cards)
            if not page_cards or signature in seen_signatures:
                page_failures += 1
                break
            seen_signatures.add(signature)
            cards.extend(page_cards)
        else:
            if _fubon_has_next(page_html):
                page_failures += 1

    unique_cards = []
    seen_ids: set[str] = set()
    for card in cards:
        if card["id"] in seen_ids:
            continue
        seen_ids.add(card["id"])
        if not card["end"] or card["end"] >= today:
            unique_cards.append(card)
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        unique_cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    fallback_count = 0
    if page_failures and activity_cache:
        known_ids = {activity.id for activity in activities}
        for cached in activity_cache.values():
            if (
                not isinstance(cached, dict)
                or cached.get("bank_id") != source["id"]
                or cached.get("id") in known_ids
            ):
                continue
            cached_end = _date_from_iso(str(cached.get("end_date") or ""))
            if cached_end and cached_end < today:
                continue
            promotion = Promotion.from_dict(cached)
            promotion.observed_at = checked_at
            promotion.source_entry_url = source["entry_url"]
            activities.append(promotion)
            known_ids.add(promotion.id)
            fallback_count += 1
    status = (
        "complete"
        if activities and page_failures == 0 and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if page_failures:
        issues.append("官方 Wicket 清單未能完整翻至最後一頁")
    if fallback_count:
        issues.append(f"沿用上一版 {fallback_count} 筆有效活動，避免清單暫時縮減")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "官方活動明細", invalid_urls))
    if not issues:
        issues.append(f"已讀取 {len(seen_signatures)} 個官方清單分頁")
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        listing.final_url, status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


def _taishin_cards(
    rows: list[dict[str, Any]],
    base_url: str,
    bank_id: str,
    category: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for row in rows:
        promotion_id = clean_inline(str(row.get("promotionId") or ""))
        title = clean_inline(str(row.get("promotionName") or ""))
        if not promotion_id or not title:
            continue
        summary = clean_inline(str(row.get("promotionBrief") or "")) or title
        start = _date_from_iso(str(row.get("promotionStartDate") or "")) or today
        end = _date_from_iso(str(row.get("promotionEndDate") or ""))
        public_url = urljoin(base_url, f"/tscccms/promotion/detail/{promotion_id}")
        registration_required = str(row.get("regRequired") or "").upper() == "Y"
        cards.append({
            "id": _stable_id(bank_id, promotion_id),
            "title": title,
            "summary": summary,
            "url": public_url,
            "start": start,
            "end": end,
            "registration_required": registration_required,
            "fingerprint": source_fingerprint(
                bank_id,
                {
                    "promotion_id": promotion_id,
                    "title": title,
                    "summary": summary,
                    "start": row.get("promotionStartDate"),
                    "end": row.get("promotionEndDate"),
                    "registration_required": registration_required,
                    "category": category,
                },
            ),
            "fetch_detail": True,
        })
    return cards


def extract_taishin(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    cards: list[dict[str, Any]] = []
    failed_categories = 0
    failed_pages = 0
    resolved_url = ""
    max_pages = int(source.get("max_pages_per_category", 50))

    with SystemCurlSession(source["official_domains"]) as session:
        for category in source.get("category_codes", list("ABCDEFGHI")):
            category_url = source["category_url_template"].format(category=category)
            try:
                listing = session.fetch_text(category_url)
            except RuntimeError:
                failed_categories += 1
                continue
            resolved_url = resolved_url or listing.final_url
            token_match = re.search(
                r'name="_csrf"\s+value="([^"]+)"',
                listing.text,
                flags=re.I,
            )
            total_match = re.search(
                r'id="totalPage"[^>]+value="(\d+)"',
                listing.text,
                flags=re.I,
            )
            if not token_match or not total_match:
                failed_categories += 1
                continue
            total_pages = min(int(total_match.group(1)), max_pages)
            token = token_match.group(1)
            for page_number in range(1, total_pages + 1):
                try:
                    _, payload = session.fetch_json(
                        source["page_data_url"],
                        data={
                            "_csrf": token,
                            "categoryId": category,
                            "queryStoreType": "null",
                            "queryRegionId": "null",
                            "queryKeyWord": "",
                            "queryOrderAscDesc": "Desc",
                            "queryPage": str(page_number),
                        },
                        headers={
                            "Accept": "application/json, text/javascript, */*; q=0.01",
                            "Referer": category_url,
                            "X-Requested-With": "XMLHttpRequest",
                        },
                    )
                except RuntimeError:
                    failed_pages += 1
                    break
                if not isinstance(payload, list):
                    failed_pages += 1
                    break
                cards.extend(_taishin_cards(
                    [row for row in payload if isinstance(row, dict)],
                    category_url,
                    source["id"],
                    category,
                    today,
                ))
            if int(total_match.group(1)) > max_pages:
                failed_pages += 1

    if not cards:
        message = "台新 A–I 信用卡優惠分類暫時無法取得活動資料。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            resolved_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_failed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]

    unique_cards = []
    seen_ids: set[str] = set()
    for card in cards:
        if card["id"] in seen_ids:
            continue
        seen_ids.add(card["id"])
        if not card["end"] or card["end"] >= today:
            unique_cards.append(card)
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        unique_cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = (
        "complete"
        if activities
        and failed_categories == 0
        and failed_pages == 0
        and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if failed_categories:
        issues.append(f"{failed_categories} 個 A–I 分類清單暫時無法讀取")
    if failed_pages:
        issues.append(f"{failed_pages} 個分類分頁暫時無法讀取")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "官方活動明細", invalid_urls))
    if not issues:
        issues.append("已讀取 A–I 九個信用卡優惠分類")
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        resolved_url, status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


def _firstbank_cards(
    html: str,
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<a[^>]+href="([^"]*/sites/card/(?:touch/)?\d+[^"]*)"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        public_url = urljoin(base_url, match.group(1))
        title = strip_html(match.group(2))
        if public_url == base_url or len(title) < 4 or public_url in seen:
            continue
        seen.add(public_url)
        cards.append({
            "id": _stable_id(bank_id, public_url),
            "title": title,
            "summary": title,
            "url": public_url,
            "start": today,
            "end": None,
            "fingerprint": source_fingerprint(
                bank_id,
                {"title": title, "url": public_url},
            ),
            "fetch_detail": True,
        })
    return cards


def extract_first(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        if "403" in str(exc):
            message = (
                "指定網址存在，但第一銀行的存取防護拒絕自動化 GET；"
                "本次不改用其他網址，待官方解除限制後再讀取。"
            )
            alert_type = "source_access_blocked"
        else:
            message = "第一銀行指定信用卡優惠入口暫時無法讀取；未改用其他網址。"
            alert_type = "source_failed"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, message,
        ), [Alert(
            alert_type,
            source["bank_name"],
            message,
            source["entry_url"],
        )]
    if re.search(r"Access Denied|You don't have permission", listing.text, re.I):
        message = (
            "指定網址存在，但第一銀行的存取防護拒絕自動化 GET；"
            "本次不改用其他網址，待官方解除限制後再讀取。"
        )
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            listing.final_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_access_blocked",
            source["bank_name"],
            message,
            source["entry_url"],
        )]
    cards = _firstbank_cards(
        listing.text,
        listing.final_url,
        source["id"],
        today,
    )
    if not cards:
        message = "指定入口可讀取，但找不到信用卡活動連結；可能是官方頁面結構已變更。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            listing.final_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_structure_changed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = "complete" if activities and failed_details == 0 else ("partial" if activities else "failed")
    message = _detail_failure_message(failed_details, "官方活動明細", invalid_urls)
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        listing.final_url, status, len(activities), checked_at, message,
    ), _invalid_url_alerts(source, invalid_urls)


def _chb_cards(
    html: str,
    base_url: str,
    bank_id: str,
    today: date,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r'<a[^>]+class="[^"]*\beditor_link\b[^"]*"[^>]+'
        r'href="([^"]*bonusDetail\.jsp\?id=\d+)"[^>]*>(.*?)</a>',
        html,
        flags=re.I | re.S,
    ):
        title = strip_html(match.group(2))
        public_url = urljoin(base_url, match.group(1))
        if not title.startswith("【") or public_url in seen:
            continue
        seen.add(public_url)
        cards.append({
            "id": _stable_id(bank_id, public_url),
            "title": title,
            "summary": title,
            "url": public_url,
            "start": today,
            "end": None,
            "fingerprint": source_fingerprint(
                bank_id,
                {"title": title, "url": public_url},
            ),
            "fetch_detail": True,
        })
    return cards


def extract_chb(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
    except RuntimeError as exc:
        message = "彰化銀行指定信用卡優惠入口暫時無法讀取。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]

    category_ids = [str(value) for value in source.get("category_ids", [])]
    missing_ids = [
        category_id
        for category_id in category_ids
        if not re.search(
            rf'bonusDetail\.jsp\?id={re.escape(category_id)}(?:["&])',
            listing.text,
        )
    ]
    if missing_ids:
        message = (
            "指定入口的信用卡分類結構已變更；"
            f"找不到分類 {', '.join(missing_ids)}，本次停止擷取以避免混入其他內容。"
        )
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            listing.final_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_structure_changed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]

    category_urls = [
        urljoin(listing.final_url, f"bonusDetail.jsp?id={category_id}")
        for category_id in category_ids
    ]
    fetched = _fetch_many(category_urls, source["official_domains"], workers=4)
    cards: list[dict[str, Any]] = []
    failed_categories = 0
    for category_url in category_urls:
        result = fetched.get(category_url)
        if isinstance(result, Exception) or result is None:
            failed_categories += 1
            continue
        cards.extend(_chb_cards(
            result.text,
            result.final_url,
            source["id"],
            today,
        ))
    unique_cards = []
    seen_ids: set[str] = set()
    for card in cards:
        if card["id"] in seen_ids:
            continue
        seen_ids.add(card["id"])
        unique_cards.append(card)
    if not unique_cards:
        message = "彰化銀行四個信用卡優惠分類可讀取，但找不到活動明細連結。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            listing.final_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_structure_changed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        unique_cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = (
        "complete"
        if activities and failed_categories == 0 and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if failed_categories:
        issues.append(f"{failed_categories} 個信用卡分類暫時無法讀取")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "官方活動明細", invalid_urls))
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        listing.final_url, status, len(activities), checked_at, "；".join(issues),
    ), _invalid_url_alerts(source, invalid_urls)


UBOT_CATEGORY_MAP = {
    "卡片優惠": "卡片權益",
    "百貨零售": "百貨購物",
    "旅遊優惠": "旅遊交通",
    "交通汽修": "加油交通",
    "網購數位": "網購",
    "生活繳費": "繳費稅款",
    "購物娛樂": "生活消費",
}


def _ubot_cards(
    rows: list[dict[str, Any]],
    bank_id: str,
    today: date,
    official_domains: list[str],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        title = clean_inline(str(row.get("title") or ""))
        summary = clean_inline(str(row.get("desc") or "")) or title
        public_url = clean_inline(str(row.get("url") or ""))
        official_category = clean_inline(str(row.get("catalog") or ""))
        if (
            not title
            or official_category not in UBOT_CATEGORY_MAP
            or not is_allowed_url(public_url, official_domains)
        ):
            continue
        identity = f"{official_category}|{title}|{public_url}"
        if identity in seen:
            continue
        seen.add(identity)
        try:
            start = date.fromisoformat(str(row.get("sdt") or "")[:10])
        except ValueError:
            start = today
        try:
            end = date.fromisoformat(str(row.get("edt") or "")[:10])
        except ValueError:
            end = None
        is_featured = row.get("hotOrder") is not None
        cards.append({
            "id": _stable_id(bank_id, identity),
            "title": title,
            "summary": summary,
            "url": public_url,
            "start": start,
            "end": end,
            "base_category": UBOT_CATEGORY_MAP[official_category],
            "official_category": official_category,
            "featured": is_featured,
            "featured_category": "強打優惠" if is_featured else "",
            "fingerprint": source_fingerprint(
                bank_id,
                {
                    "title": title,
                    "summary": summary,
                    "url": public_url,
                    "official_category": official_category,
                    "order": row.get("order"),
                    "hot_order": row.get("hotOrder"),
                    "start": row.get("sdt"),
                    "end": row.get("edt"),
                },
            ),
            "fetch_detail": True,
        })
    return cards


def extract_ubot(
    source: dict[str, Any],
    *,
    now: datetime,
    percent_threshold: float,
    amount_threshold: int,
    activity_cache: dict[str, dict[str, Any]] | None = None,
    cache_stats: dict[str, Any] | None = None,
) -> tuple[list[Promotion], SourceHealth, list[Alert]]:
    checked_at = _now_iso(now)
    today = now.astimezone(TAIPEI).date()
    try:
        listing = fetch_text(source["entry_url"], source["official_domains"])
        api_result, payload = fetch_json(
            source["api_url"],
            source["official_domains"],
        )
    except RuntimeError as exc:
        message = "聯邦銀行指定活動入口或官方 Rewards 資料暫時無法讀取。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"], "",
            "failed", 0, checked_at, str(exc),
        ), [Alert(
            "source_failed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]

    rows = (
        payload.get("info", [])
        if isinstance(payload, dict) and payload.get("rtnCode") == "0000"
        else []
    )
    rows = [row for row in rows if isinstance(row, dict)]
    cards = _ubot_cards(
        rows,
        source["id"],
        today,
        source["official_domains"],
    )
    if not cards:
        message = "聯邦銀行 Rewards API 可讀取，但找不到有效的官方活動資料。"
        return [], SourceHealth(
            source["id"], source["bank_name"], source["entry_url"],
            api_result.final_url, "failed", 0, checked_at, message,
        ), [Alert(
            "source_structure_changed",
            source["bank_name"],
            message,
            source["entry_url"],
        )]

    observed_categories = {
        clean_inline(str(row.get("catalog") or ""))
        for row in rows
        if row.get("catalog")
    }
    expected_categories = {
        clean_inline(str(value))
        for value in source.get("data_categories", [])
    }
    missing_categories = sorted(expected_categories - observed_categories)
    activities, failed_details, invalid_urls = _listing_promotions(
        source,
        cards,
        now=now,
        percent_threshold=percent_threshold,
        amount_threshold=amount_threshold,
        activity_cache=activity_cache,
        cache_stats=cache_stats,
    )
    status = (
        "complete"
        if activities and not missing_categories and failed_details == 0
        else ("partial" if activities else "failed")
    )
    issues = []
    if missing_categories:
        issues.append(f"官方資料缺少分類：{', '.join(missing_categories)}")
    if failed_details:
        issues.append(_detail_failure_message(failed_details, "官方活動明細", invalid_urls))
    if not issues:
        hot_count = sum(
            1 for activity in activities
            if "強打優惠" in activity.categories
        )
        issues.append(
            f"已讀取 7 個資料分類；API 共 {len(cards)} 筆，"
            f"目前有效 {len(activities)} 筆；"
            f"另以 hotOrder 標記 {hot_count} 筆強打優惠"
        )
    alerts = []
    if missing_categories:
        alerts.append(Alert(
            "source_structure_changed",
            source["bank_name"],
            issues[0],
            source["entry_url"],
        ))
    alerts.extend(_invalid_url_alerts(source, invalid_urls))
    return activities, SourceHealth(
        source["id"], source["bank_name"], source["entry_url"],
        listing.final_url, status, len(activities), checked_at, "；".join(issues),
    ), alerts
