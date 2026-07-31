from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from card_promotions_monitor.cli import (
    PUBLISH_GUARD_EXIT_CODE,
    assess_publish_guard,
    persist_payload,
    retain_failed_source_activities,
    update_lock,
)


TAIPEI = ZoneInfo("Asia/Taipei")


def candidate_payload(*, failed: int, active: int, dns_failures: int = 0) -> dict:
    health = []
    for index in range(17):
        is_failed = index < failed
        message = ""
        if index < dns_failures:
            message = "<urlopen error [Errno 8] nodename nor servname provided>"
        health.append({
            "id": f"bank-{index}",
            "bank_name": f"銀行 {index}",
            "status": "failed" if is_failed else "complete",
            "activity_count": 0 if is_failed else 1,
            "message": message,
        })
    return {
        "generated_at": "2026-08-01T01:00:00+08:00",
        "summary": {"active_or_upcoming": active, "total": active},
        "source_health": health,
        "activities": [],
    }


class PublishGuardTests(unittest.TestCase):
    def test_blocks_systemic_dns_failure(self) -> None:
        candidate = candidate_payload(failed=16, active=27, dns_failures=14)
        previous = {"summary": {"active_or_upcoming": 1110}}
        guard = assess_publish_guard(candidate, previous)
        self.assertTrue(guard["blocked"])
        self.assertIn("systemic_dns_failure", guard["reason_codes"])
        self.assertIn("catastrophic_source_failure", guard["reason_codes"])
        self.assertIn("catastrophic_activity_regression", guard["reason_codes"])

    def test_allows_one_known_source_failure(self) -> None:
        candidate = candidate_payload(failed=1, active=1077)
        previous = {"summary": {"active_or_upcoming": 1110}}
        guard = assess_publish_guard(candidate, previous)
        self.assertFalse(guard["blocked"])

    def test_blocked_candidate_does_not_replace_public_output(self) -> None:
        candidate = candidate_payload(failed=16, active=27, dns_failures=14)
        previous = {
            "generated_at": "2026-07-30T21:47:18+08:00",
            "summary": {"active_or_upcoming": 1110},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "promotions.json"
            report = root / "latest.json"
            original = json.dumps(previous, ensure_ascii=False) + "\n"
            output.write_text(original, encoding="utf-8")

            exit_code = persist_payload(candidate, previous, output, report)

            self.assertEqual(exit_code, PUBLISH_GUARD_EXIT_CODE)
            self.assertEqual(output.read_text(encoding="utf-8"), original)
            report_payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(report_payload["publish_guard"]["blocked"])
            self.assertTrue(
                report_payload["publish_guard"]["published_snapshot_preserved"]
            )

    def test_failed_source_retains_previous_current_activities(self) -> None:
        activities: list[dict] = []
        health = [{
            "id": "bank",
            "status": "failed",
            "activity_count": 0,
            "message": "DNS failed",
        }]
        previous = {
            "activities": [{
                "id": "bank-activity",
                "bank_id": "bank",
                "title": "仍有效活動",
                "start_date": "2026-07-01",
                "end_date": "2026-08-31",
                "lifecycle": "active",
            }]
        }
        stats: dict = {}

        retain_failed_source_activities(
            activities,
            health,
            previous,
            datetime(2026, 8, 1, 1, 0, tzinfo=TAIPEI),
            stats,
        )

        self.assertEqual([item["id"] for item in activities], ["bank-activity"])
        self.assertEqual(health[0]["retained_activity_count"], 1)
        self.assertEqual(stats["source_fallback_activities"], 1)

    def test_update_lock_rejects_concurrent_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "update.lock"
            with update_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "already in progress"):
                    with update_lock(lock_path):
                        self.fail("second writer unexpectedly acquired the lock")


if __name__ == "__main__":
    unittest.main()
