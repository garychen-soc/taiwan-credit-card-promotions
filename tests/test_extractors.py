from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from card_promotions_monitor.extractors import (
    _class_text,
    _chb_cards,
    _compact_period,
    _categories,
    _ctbc_card_blocks,
    _firstbank_cards,
    _fubon_cards,
    _fubon_page_listener,
    _has_registration_requirement,
    _html_segments,
    _hncb_credit_card_cards,
    _kgi_cards,
    _lifecycle,
    _parse_period,
    _roc_period,
    _registration_windows,
    _reward_values,
    _terms_content,
    _taishin_cards,
    _tcbbank_api_cards,
    _ubot_cards,
    _yuanta_cards,
    _esun_cards,
    extract_chb,
    normalize_registration_url,
)
from card_promotions_monitor.fetch import FetchResult


class RegistrationWindowTests(unittest.TestCase):
    def test_preserves_non_registration_terms_in_sections(self) -> None:
        raw, sections = _terms_content(
            "活動期間：2026/8/1 至 2026/8/31\n"
            "參加資格：限正卡持卡人\n"
            "活動辦法：單筆滿 10,000 元享刷卡金\n"
            "分期辦法：須分 12 期以上\n"
            "登錄辦法：8/7 17:00 開放登錄\n"
            "注意事項：限量 600 名，額滿為止"
        )

        self.assertIn("限正卡持卡人", raw)
        self.assertIn("單筆滿 10,000 元", sections["method"])
        self.assertIn("須分 12 期以上", sections["installment"])
        self.assertIn("8/7 17:00", sections["registration"])
        self.assertIn("限量 600 名", sections["notes"])

    def test_uses_verified_central_registration_portals(self) -> None:
        self.assertEqual(
            normalize_registration_url(
                "kgi",
                "https://www.kgibank.com.tw/zh-tw/personal/promotion/card-campaign?category=All",
            ),
            "https://www.kgibank.com/creditcard/campaign/registrationlist",
        )
        self.assertEqual(
            normalize_registration_url(
                "chb",
                "https://www.bankchb.com/frontend/mashup.jsp?funcId=116a6c4815",
            ),
            "https://www.bankchb.com/frontend/CampaignLog.html",
        )
        self.assertEqual(
            normalize_registration_url(
                "hncb",
                "https://www.hncb.com.tw/wps/portal/HNCB/card/+https:/invalid.example",
            ),
            "https://netbank.hncb.com.tw/netbank/servlet/TrxDispatcher?trx=com.lb.wibc.trx.CardPromoteOverall_RWD&state=prompt",
        )

    def test_keeps_verified_activity_specific_registration_url(self) -> None:
        value = "https://cardweb.ubot.com.tw/register_extra"
        self.assertEqual(normalize_registration_url("ubot", value), value)
        self.assertEqual(
            normalize_registration_url(
                "ubot",
                "https://card.ubot.com.tw/eCard/activity_login/register_activity.aspx?_gl=tracking",
            ),
            "https://card.ubot.com.tw/eCard/activity_login/register_activity.aspx",
        )

    def test_rejects_external_registration_link(self) -> None:
        self.assertEqual(
            normalize_registration_url("obank", "https://merchant.example/register"),
            "https://www.o-bank.com/retail/event/event-compaign",
        )

    def test_parses_exact_range(self) -> None:
        values = _registration_windows(
            "須於2026/8/28 16:00~8/31 23:59於官網完成活動登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-28T16:00:00+08:00")
        self.assertEqual(values[0].end, "2026-08-31T23:59:00+08:00")
        self.assertEqual(values[0].reminder_minutes, 10)

    def test_duplicate_range_does_not_create_endpoint_event(self) -> None:
        values = _registration_windows(
            "2026/7/30 16:00至2026/7/31 23:59完成活動登錄。"
            "2026/7/30 16:00至2026/7/31 23:59開放登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].end, "2026-07-31T23:59:00+08:00")

    def test_parses_second_precision_range(self) -> None:
        values = _registration_windows(
            "登錄時間：2026/8/7 17:00:00~2026/8/31 23:59:00",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-07T17:00:00+08:00")
        self.assertEqual(values[0].end, "2026-08-31T23:59:00+08:00")

    def test_normalizes_fullwidth_time_and_roc_year_range(self) -> None:
        values = _registration_windows(
            "登錄時間：115/8/7 17：00：00～115/8/31 23：59：00",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-07T17:00:00+08:00")
        self.assertEqual(values[0].end, "2026-08-31T23:59:00+08:00")

    def test_parses_start_only(self) -> None:
        values = _registration_windows(
            "刷卡金活動於2026/7/31 16:00開始登錄，額滿即止。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-07-31T16:00:00+08:00")
        self.assertIsNone(values[0].end)

    def test_parses_taiwan_time_words(self) -> None:
        values = _registration_windows(
            "7月活動於7/24下午3點整開始登錄；8月活動於8/24下午3點整開始登錄。",
            2026,
        )
        starts = [item.start for item in values]
        self.assertIn("2026-07-24T15:00:00+08:00", starts)
        self.assertIn("2026-08-24T15:00:00+08:00", starts)

    def test_parses_noon_and_explicit_adjusted_registration_dates(self) -> None:
        values = _registration_windows(
            "本活動於每月1日中午12:00起開放登錄，"
            "登錄日期如下，7月：07/01、8月：08/03、9月：09/01。",
            2026,
            date(2026, 7, 1),
            date(2026, 9, 30),
        )
        self.assertEqual(
            [item.start for item in values],
            [
                "2026-07-01T12:00:00+08:00",
                "2026-08-03T12:00:00+08:00",
                "2026-09-01T12:00:00+08:00",
            ],
        )
        self.assertTrue(all(item.end is None for item in values))

    def test_negative_registration_wording_is_not_requirement(self) -> None:
        self.assertFalse(_has_registration_requirement("活動登錄專區（本活動不須登錄）"))

    def test_parses_same_day_range(self) -> None:
        values = _registration_windows(
            "於2026/9/9 16:00至23:59開放登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-09-09T16:00:00+08:00")
        self.assertEqual(values[0].end, "2026-09-09T23:59:00+08:00")

    def test_parses_em_dash_range(self) -> None:
        values = _registration_windows(
            "登錄時間:2026/07/29 17:00 — 2026/07/31 23:59",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-07-29T17:00:00+08:00")
        self.assertEqual(values[0].end, "2026-07-31T23:59:00+08:00")

    def test_parses_registration_deadline(self) -> None:
        values = _registration_windows(
            "登錄期限至2026/10/31 23:59止，逾期恕不受理。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].label, "登錄截止")
        self.assertEqual(values[0].start, "2026-10-31T23:59:00+08:00")
        self.assertIsNone(values[0].end)

    def test_ignores_malformed_range_endpoint(self) -> None:
        values = _registration_windows(
            "須於2026/08/03當日16:00至07/20 23:59完成活動登錄始符合資格。",
            2026,
        )
        self.assertEqual(values, [])

    def test_generic_legal_wording_is_not_requirement(self) -> None:
        self.assertFalse(_has_registration_requirement("如持卡人登錄之資料有遲延，本行不負責。"))
        self.assertTrue(_has_registration_requirement("須於2026/8/28完成活動登錄始符合資格。"))

    def test_parses_recurring_month_day_registration(self) -> None:
        values = _registration_windows(
            "每月6號下午1:00開放登錄",
            2026,
            date(2026, 7, 1),
            date(2026, 9, 30),
        )
        self.assertEqual(
            [item.start for item in values],
            [
                "2026-07-06T13:00:00+08:00",
                "2026-08-06T13:00:00+08:00",
                "2026-09-06T13:00:00+08:00",
            ],
        )
        self.assertTrue(all(item.end is None for item in values))

    def test_parses_recurring_nth_weekday_registration(self) -> None:
        values = _registration_windows(
            "本活動於「每月第一個週三」下午4點開放登錄",
            2026,
            date(2026, 7, 1),
            date(2026, 9, 30),
        )
        self.assertEqual(
            [item.start for item in values],
            [
                "2026-07-01T16:00:00+08:00",
                "2026-08-05T16:00:00+08:00",
                "2026-09-02T16:00:00+08:00",
            ],
        )
        self.assertTrue(all(item.end is None for item in values))


class ClassificationTests(unittest.TestCase):
    def test_chb_invalid_detail_redirect_is_skipped_and_reported(self) -> None:
        source = {
            "id": "chb",
            "bank_name": "彰化銀行",
            "entry_url": "https://www.bankchb.com/frontend/bonusDetail.jsp?id=3657",
            "official_domains": ["bankchb.com"],
            "category_ids": ["3646"],
        }
        entry_html = '<a href="bonusDetail.jsp?id=3646">信用卡優惠</a>'
        category_html = (
            '<a class="editor_link" href="bonusDetail.jsp?id=3450">'
            "【測試】不安全導向活動</a>"
        )
        rejected_url = "http://10.100.6.38/frontend/bonusDetail.jsp?id=3450"

        def fake_fetch(url: str, _domains: list[str], **_kwargs) -> FetchResult:
            if "id=3450" in url:
                raise ValueError(f"URL is outside official domains: {rejected_url}")
            html = category_html if "id=3646" in url else entry_html
            return FetchResult(url, url, 200, html, "text/html", "fixture")

        with patch(
            "card_promotions_monitor.extractors.fetch_text",
            side_effect=fake_fetch,
        ):
            activities, health, alerts = extract_chb(
                source,
                now=datetime(2026, 8, 4, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                percent_threshold=10,
                amount_threshold=500,
            )

        self.assertEqual(activities, [])
        self.assertEqual(health.status, "failed")
        self.assertIn(rejected_url, health.message)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].type, "source_emitted_invalid_url")
        self.assertEqual(alerts[0].old_url, rejected_url)

    def test_reward_threshold_inputs(self) -> None:
        percent, amount = _reward_values("最高享12%回饋，另贈NT$1,500刷卡金")
        self.assertEqual(percent, 12)
        self.assertEqual(amount, 1500)

    def test_categories_are_multidimensional(self) -> None:
        values = _categories("OPEN錢包綁卡於全國電子分期購物")
        self.assertEqual(values[0], "網購")
        self.assertIn("行動支付", values)
        self.assertIn("百貨購物", values)
        self.assertIn("分期", values)

    def test_lifecycle(self) -> None:
        today = date(2026, 7, 30)
        self.assertEqual(_lifecycle(date(2026, 8, 1), date(2026, 8, 31), today), "upcoming")
        self.assertEqual(_lifecycle(date(2026, 7, 1), date(2026, 7, 31), today), "active")
        self.assertEqual(_lifecycle(date(2026, 6, 1), date(2026, 6, 30), today), "ended")

    def test_bank_listing_period_formats(self) -> None:
        self.assertEqual(
            _parse_period("2026/07/01~2026/09/30", 2026),
            (date(2026, 7, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            _compact_period("20260630000000-20261001000000"),
            (date(2026, 6, 30), date(2026, 10, 1)),
        )
        self.assertEqual(
            _compact_period("20190531000000-29991231235959"),
            (date(2019, 5, 31), None),
        )
        self.assertEqual(
            _parse_period("活動日期：2026/04/02起至2026/12/31止", 2026),
            (date(2026, 4, 2), date(2026, 12, 31)),
        )
        self.assertEqual(
            _roc_period("115/7/1-115/12/31", date(2026, 7, 30)),
            (date(2026, 7, 1), date(2026, 12, 31)),
        )
        self.assertEqual(
            _parse_period("活動期間：115/7/1~115/9/30", 2026),
            (date(2026, 7, 1), date(2026, 9, 30)),
        )
        self.assertEqual(
            _parse_period("2026.07.08~2026.09.30", 2026),
            (date(2026, 7, 8), date(2026, 9, 30)),
        )

    def test_extracts_card_class_text_and_segments(self) -> None:
        html = (
            '<li class="card-list__item"><span class="card__date">2026/7/1~2026/12/31</span></li>'
            '<li class="card-list__item"><span class="card__date">2026/8/1~2026/8/31</span></li>'
        )
        blocks = _html_segments(html, r'<li\s+class="card-list__item"')
        self.assertEqual(len(blocks), 2)
        self.assertEqual(_class_text(blocks[0], "card__date"), "2026/7/1~2026/12/31")

    def test_ctbc_card_block_stops_before_trailing_lightbox(self) -> None:
        html = (
            '<li class="card-list__item"><span class="sr-only">LOPIA</span>'
            '<span class="card__date">2026/7/1~2026/9/30</span></li>'
            '<div class="lightbox">其他商店每月6號10:00開放登錄</div>'
        )
        blocks = _ctbc_card_blocks(html)
        self.assertEqual(len(blocks), 1)
        self.assertNotIn("其他商店", blocks[0])

    def test_extracts_yuanta_listing_card(self) -> None:
        html = (
            '<li><div class="pic"><img src="x"></div>'
            "<h6>網購最高回饋</h6><p>活動摘要</p>"
            '<a href="/bank/creditCard/promotionActivity/in.do?id=abc">詳細內容</a></li>'
        )
        cards = _yuanta_cards(html, "https://www.yuantabank.com.tw/bank/", "yuanta")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "網購最高回饋")
        self.assertTrue(cards[0]["url"].endswith("id=abc"))

    def test_extracts_esun_api_card(self) -> None:
        html = (
            '<div class="col-12 paginationList"><a href="/zh-tw/personal/credit-card/'
            'discount/shopInfo?sno=100"><p class="l-cardDiscountAllContent__discount--title h3">'
            '網購商店</p><p class="l-cardDiscountAllContent__discount--word">最高10%回饋</p></a></div>'
        )
        cards = _esun_cards(html, "https://www.esunbank.com/zh-tw/", "esun")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["summary"], "最高10%回饋")

    def test_extracts_tcbbank_listing_card(self) -> None:
        cards = _tcbbank_api_cards(
            [{
                "Title": "低碳生活新品味",
                "SubTitle": "(至116年1月5日有效)",
                "Url": "/Promotion/Info.html?ID=701",
                "StartDate": "2025-12-29T00:00:00.000",
                "EndDate": "2027-01-06T00:00:00.000",
            }],
            "https://www.tcbbank.com.tw/creditcard/J_01.html",
            "tcbbank",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "低碳生活新品味")
        self.assertEqual(cards[0]["start"], date(2025, 12, 29))
        self.assertEqual(cards[0]["end"], date(2027, 1, 5))

    def test_extracts_kgi_roc_period_and_summary(self) -> None:
        html = (
            '<div class="fs-h3 fs-lg-h3 color-primary-blue">'
            "指定旅行社最高4,500元刷卡金</div>"
            '<div class="fs-h5 kgibStatic011__item-title">'
            "活動期間：115/7/1~115/9/30 需登錄</div>"
            '<a href="/zh-tw/personal/promotion/card-campaign/travelagency-a">'
            "了解更多</a>"
        )
        cards = _kgi_cards(
            html,
            "https://www.kgibank.com.tw/zh-tw/personal/promotion/card-campaign",
            "kgi",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["start"], date(2026, 7, 1))
        self.assertEqual(cards[0]["end"], date(2026, 9, 30))

    def test_hncb_parser_is_limited_to_credit_card_tab(self) -> None:
        html = (
            '<a aria-controls="deposit-panel" role="tab" aria-label="存款/外匯">存款</a>'
            '<a aria-controls="card-panel" role="tab" aria-label="信用卡">信用卡</a>'
            '<div class="tab-pane" id="deposit-panel" role="tabpanel">'
            '<div class="feature-title"><a href="/deposit">存款優惠</a></div></div>'
            '<div class="tab-pane" id="card-panel" role="tabpanel">'
            '<div class="feature-title"><a href="+https://partner.example/card-offer">'
            "信用卡優惠</a></div></div>"
            '<div class="tab-pane" id="other-panel" role="tabpanel">'
            '<div class="feature-title"><a href="/loan">貸款優惠</a></div></div>'
        )
        cards, panel_id = _hncb_credit_card_cards(
            html,
            "https://www.hncb.com.tw/wps/portal/HNCB/card",
            "hncb",
            ["hncb.com.tw"],
            date(2026, 7, 30),
        )
        self.assertEqual(panel_id, "card-panel")
        self.assertEqual([item["title"] for item in cards], ["信用卡優惠"])
        self.assertEqual(cards[0]["url"], "https://partner.example/card-offer")

    def test_extracts_taipei_fubon_listing_card(self) -> None:
        html = (
            '<div class="discount-card">'
            '<a href="Detail?sn=D000289" class="discount-link"></a>'
            '<h3 class="discount-name">3C家電滿額回饋</h3>'
            '<div class="discount-date">2026.07.01~2026.09.30</div>'
            '<h5 class="discount-text">最高送3,500元</h5></div>'
        )
        cards = _fubon_cards(
            html,
            "https://cardpromote.taipeifubon.com.tw/promotion/Result",
            "taipei_fubon",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["start"], date(2026, 7, 1))
        self.assertEqual(cards[0]["end"], date(2026, 9, 30))

    def test_fubon_page_listener_uses_visible_page_after_index_shift(self) -> None:
        html = (
            '<a href="./Result?1-1.-fmList-divSearchResult-nav-navigation-5-pageLink">'
            "11</a>"
        )
        self.assertEqual(
            _fubon_page_listener(
                html,
                "https://cardpromote.taipeifubon.com.tw/promotion/Result",
                11,
            ),
            "https://cardpromote.taipeifubon.com.tw/promotion/"
            "Result?1-1.0-fmList-divSearchResult-nav-navigation-5-pageLink",
        )

    def test_extracts_taishin_json_card(self) -> None:
        cards = _taishin_cards(
            [{
                "promotionId": "WM_20260630112228562",
                "promotionName": "海外消費分期優惠",
                "promotionBrief": "享優惠利率",
                "promotionStartDate": "2026-06-30T16:00:00.000+00:00",
                "promotionEndDate": "2026-09-30T15:59:59.000+00:00",
                "regRequired": "Y",
            }],
            "https://mkpcard.taishinbank.com.tw/tscccms/promotion/offerList/A",
            "taishin",
            "A",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["start"], date(2026, 7, 1))
        self.assertEqual(cards[0]["end"], date(2026, 9, 30))
        self.assertTrue(cards[0]["registration_required"])

    def test_extracts_ubot_categories_and_keeps_shared_url_titles(self) -> None:
        rows = [
            {
                "title": "Uber Eats",
                "desc": "刷聯邦卡享3%",
                "url": "https://activity.ubot.com.tw/aws_act/delivery/index.htm",
                "catalog": "網購數位",
                "order": 0,
                "hotOrder": 1,
                "sdt": "2026-07-01 00:00:00",
                "edt": "2026-12-31 23:59:59",
            },
            {
                "title": "foodpanda",
                "desc": "刷聯邦卡享3%",
                "url": "https://activity.ubot.com.tw/aws_act/delivery/index.htm",
                "catalog": "網購數位",
                "order": 0,
                "hotOrder": None,
                "sdt": "2026-07-01 00:00:00",
                "edt": "2026-12-31 23:59:59",
            },
        ]
        cards = _ubot_cards(
            rows,
            "ubot",
            date(2026, 7, 30),
            ["ubot.com.tw"],
        )
        self.assertEqual(len(cards), 2)
        self.assertNotEqual(cards[0]["id"], cards[1]["id"])
        self.assertEqual(cards[0]["base_category"], "網購")
        self.assertEqual(cards[0]["featured_category"], "強打優惠")
        self.assertFalse(cards[1]["featured"])

    def test_extracts_firstbank_activity_links(self) -> None:
        html = (
            '<a href="/sites/card/touch/1234567890123">'
            "網購滿額回饋活動</a>"
        )
        cards = _firstbank_cards(
            html,
            "https://card.firstbank.com.tw/sites/card/touch/1565690686288",
            "first",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "網購滿額回饋活動")

    def test_extracts_chb_editor_activity_links_only(self) -> None:
        html = (
            '<a class="editor_link" href="https://www.bankchb.com/frontend/'
            'bonusDetail.jsp?id=3577">【線上購物】MOMO全通路活動</a>'
            '<a class="editor_link" href="/frontend/mashup.jsp?funcId=x">'
            "信用卡費率</a>"
        )
        cards = _chb_cards(
            html,
            "https://www.bankchb.com/frontend/bonusDetail.jsp?id=3655",
            "chb",
            date(2026, 7, 30),
        )
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["title"], "【線上購物】MOMO全通路活動")


if __name__ == "__main__":
    unittest.main()
