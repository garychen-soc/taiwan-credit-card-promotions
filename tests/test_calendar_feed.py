from __future__ import annotations

import re
import unittest

from card_promotions_monitor.calendar_feed import build_registration_feed


class RegistrationCalendarFeedTests(unittest.TestCase):
    def test_feed_uses_stable_events_and_omits_expired_point_windows(self) -> None:
        payload = {
            "generated_at": "2026-08-16T10:00:00+08:00",
            "activities": [{
                "id": "bank-offer",
                "bank_name": "測試銀行",
                "title": "滿額登錄",
                "source_url": "https://bank.example/offer",
                "registration_url": "https://bank.example/register",
                "registration_required": True,
                "registration_timing_contracts": ["unknown"],
                "registration_windows": [
                    {"start": "2026-08-15T10:00:00+08:00", "end": "2026-08-31T23:59:00+08:00"},
                    {"start": "2026-08-17T10:00:00+08:00", "end": None},
                ],
            }],
        }

        first = build_registration_feed(payload)
        second = build_registration_feed(payload)

        self.assertEqual(first, second)
        self.assertEqual(first.count("BEGIN:VEVENT"), 1)
        self.assertIn("DTSTART:20260817T020000Z", first)
        self.assertIn("DTEND:20260817T021500Z", first)
        self.assertIn("TRIGGER:-PT10M", first)
        self.assertNotIn("DTSTART:20260815T020000Z", first)

    def test_confirmed_monthly_sequence_uses_bounded_rrule(self) -> None:
        payload = {
            "generated_at": "2026-08-16T10:00:00+08:00",
            "activities": [{
                "id": "bank-monthly",
                "bank_name": "測試銀行",
                "title": "每月重新登錄",
                "source_url": "https://bank.example/monthly",
                "registration_url": "https://bank.example/register",
                "registration_required": True,
                "registration_timing_contracts": ["per_period_reregister"],
                "registration_windows": [
                    {"start": "2026-07-22T10:00:00+08:00", "end": None},
                    {"start": "2026-08-22T10:00:00+08:00", "end": None},
                    {"start": "2026-09-22T10:00:00+08:00", "end": None},
                ],
            }],
        }

        feed = build_registration_feed(payload)

        self.assertEqual(feed.count("BEGIN:VEVENT"), 1)
        self.assertIn("RRULE:FREQ=MONTHLY;COUNT=3", feed)
        self.assertIn("包含 3 個已解析的官方時點", feed.replace("\r\n ", ""))

        extended = payload.copy()
        extended["activities"] = [dict(payload["activities"][0])]
        extended["activities"][0]["registration_windows"] = [
            *payload["activities"][0]["registration_windows"],
            {"start": "2026-10-22T10:00:00+08:00", "end": None},
        ]
        original_uid = re.search(r"^UID:(.+)$", feed, flags=re.MULTILINE).group(1)
        extended_uid = re.search(
            r"^UID:(.+)$",
            build_registration_feed(extended),
            flags=re.MULTILINE,
        ).group(1)
        self.assertEqual(original_uid, extended_uid)

    def test_irregular_periods_remain_separate_events(self) -> None:
        payload = {
            "generated_at": "2026-08-16T10:00:00+08:00",
            "activities": [{
                "id": "bank-irregular",
                "bank_name": "測試銀行",
                "title": "分檔登錄",
                "source_url": "https://bank.example/irregular",
                "registration_required": True,
                "registration_timing_contracts": ["per_period_reregister"],
                "registration_windows": [
                    {"start": "2026-09-09T16:00:00+08:00", "end": None},
                    {"start": "2026-12-22T16:00:00+08:00", "end": None},
                ],
            }],
        }

        feed = build_registration_feed(payload)

        self.assertEqual(feed.count("BEGIN:VEVENT"), 2)
        self.assertNotIn("RRULE:", feed)


if __name__ == "__main__":
    unittest.main()
