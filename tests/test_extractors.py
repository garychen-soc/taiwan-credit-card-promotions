from __future__ import annotations

import unittest
from datetime import date

from card_promotions_monitor.extractors import (
    _categories,
    _has_registration_requirement,
    _lifecycle,
    _registration_windows,
    _reward_values,
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

    def test_parses_same_day_range(self) -> None:
        values = _registration_windows(
            "於2026/9/9 16:00至23:59開放登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-09-09T16:00:00+08:00")
        self.assertEqual(values[0].end, "2026-09-09T23:59:00+08:00")

    def test_ignores_malformed_range_endpoint(self) -> None:
        values = _registration_windows(
            "須於2026/08/03當日16:00至07/20 23:59完成活動登錄始符合資格。",
            2026,
        )
        self.assertEqual(values, [])

    def test_generic_legal_wording_is_not_requirement(self) -> None:
        self.assertFalse(_has_registration_requirement("如持卡人登錄之資料有遲延，本行不負責。"))
        self.assertTrue(_has_registration_requirement("須於2026/8/28完成活動登錄始符合資格。"))


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


if __name__ == "__main__":
    unittest.main()
