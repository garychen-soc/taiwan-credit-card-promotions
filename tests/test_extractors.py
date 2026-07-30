from __future__ import annotations

import unittest
from datetime import date

from card_promotions_monitor.extractors import (
    _class_text,
    _compact_period,
    _categories,
    _ctbc_card_blocks,
    _has_registration_requirement,
    _html_segments,
    _lifecycle,
    _parse_period,
    _roc_period,
    _registration_windows,
    _reward_values,
    _yuanta_cards,
    _esun_cards,
)


class RegistrationWindowTests(unittest.TestCase):
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

    def test_parses_start_only(self) -> None:
        values = _registration_windows(
            "刷卡金活動於2026/7/31 16:00開始登錄，額滿即止。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-07-31T16:00:00+08:00")
        self.assertEqual(values[0].end, "2026-07-31T16:30:00+08:00")

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

    def test_parses_registration_deadline(self) -> None:
        values = _registration_windows(
            "登錄期限至2026/10/31 23:59止，逾期恕不受理。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].label, "登錄截止")
        self.assertEqual(values[0].start, "2026-10-31T23:59:00+08:00")

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


class ClassificationTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
