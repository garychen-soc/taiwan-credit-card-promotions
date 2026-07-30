from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from card_promotions_monitor.cache import (
    new_cache_stats,
    reuse_cached_promotion,
    source_fingerprint,
)
from card_promotions_monitor.models import Promotion


TAIPEI = ZoneInfo("Asia/Taipei")


def cached_activity(
    *,
    fingerprint: str,
    checked_at: str = "2026-07-20T10:00:00+08:00",
    start_date: str = "2026-06-01",
    end_date: str = "2026-12-31",
) -> dict:
    return Promotion(
        id="bank-activity",
        bank_id="bank",
        bank_name="測試銀行",
        title="測試活動",
        merchant="測試商店",
        categories=["網購"],
        start_date=start_date,
        end_date=end_date,
        summary="最高 10% 回饋",
        source_url="https://bank.example/activity",
        source_entry_url="https://bank.example/",
        observed_at=checked_at,
        max_reward_percent=10,
        high_return=True,
        featured=True,
        lifecycle="active",
        source_fingerprint=fingerprint,
        last_detail_checked_at=checked_at,
    ).to_dict()


class ActivityCacheTests(unittest.TestCase):
    def test_reuses_matching_fresh_activity_and_avoids_detail_request(self) -> None:
        fingerprint = source_fingerprint("bank", {"title": "測試活動"})
        stats = new_cache_stats("2026-07-20T10:00:00+08:00")
        value = reuse_cached_promotion(
            {"bank-activity": cached_activity(fingerprint=fingerprint)},
            activity_id="bank-activity",
            fingerprint=fingerprint,
            now=datetime(2026, 7, 30, 10, 0, tzinfo=TAIPEI),
            source_entry_url="https://bank.example/",
            percent_threshold=10,
            amount_threshold=500,
            stats=stats,
            avoids_detail_request=True,
        )
        self.assertIsNotNone(value)
        self.assertEqual(stats["reused_activities"], 1)
        self.assertEqual(stats["detail_requests_avoided"], 1)
        self.assertEqual(value.observed_at, "2026-07-30T10:00:00+08:00")

    def test_refreshes_activity_near_end_boundary(self) -> None:
        fingerprint = source_fingerprint("bank", {"title": "測試活動"})
        stats = new_cache_stats()
        value = reuse_cached_promotion(
            {
                "bank-activity": cached_activity(
                    fingerprint=fingerprint,
                    end_date="2026-07-31",
                )
            },
            activity_id="bank-activity",
            fingerprint=fingerprint,
            now=datetime(2026, 7, 30, 10, 0, tzinfo=TAIPEI),
            source_entry_url="https://bank.example/",
            percent_threshold=10,
            amount_threshold=500,
            stats=stats,
            avoids_detail_request=True,
        )
        self.assertIsNone(value)
        self.assertEqual(stats["cache_misses"], 1)

    def test_refreshes_stale_cached_activity(self) -> None:
        fingerprint = source_fingerprint("bank", {"title": "測試活動"})
        stats = new_cache_stats()
        value = reuse_cached_promotion(
            {
                "bank-activity": cached_activity(
                    fingerprint=fingerprint,
                    checked_at="2026-06-01T10:00:00+08:00",
                )
            },
            activity_id="bank-activity",
            fingerprint=fingerprint,
            now=datetime(2026, 7, 30, 10, 0, tzinfo=TAIPEI),
            source_entry_url="https://bank.example/",
            percent_threshold=10,
            amount_threshold=500,
            stats=stats,
            avoids_detail_request=True,
        )
        self.assertIsNone(value)
        self.assertEqual(stats["cache_misses"], 1)


if __name__ == "__main__":
    unittest.main()
