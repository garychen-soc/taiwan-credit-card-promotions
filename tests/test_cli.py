from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from card_promotions_monitor.cli import (
    PUBLISH_GUARD_EXIT_CODE,
    _write_public_artifacts,
    annotate_source_registration_coverage,
    assess_publish_guard,
    classify_registration_urls,
    load_previous_public_payload,
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
    def test_source_registration_coverage_is_explicit(self) -> None:
        activities = [
            {"bank_id": "bank", "registration_required": True, "registration_windows": [{"start": "2026-08-01T10:00:00+08:00"}]},
            {"bank_id": "bank", "registration_required": True, "registration_windows": []},
            {"bank_id": "bank", "registration_required": False, "registration_windows": []},
        ]
        health = [{"id": "bank", "activity_count": 3}]

        annotate_source_registration_coverage(activities, health)

        self.assertEqual(health[0]["registration_required_count"], 2)
        self.assertEqual(health[0]["registration_time_confirmed_count"], 1)
        self.assertEqual(health[0]["registration_time_coverage_percent"], 50.0)

    def test_classifies_shared_portal_and_activity_specific_urls(self) -> None:
        activities = [
            {"bank_id": "bank", "registration_required": True, "registration_url": "https://bank.example/portal", "source_url": "https://bank.example/a"},
            {"bank_id": "bank", "registration_required": True, "registration_url": "https://bank.example/portal", "source_url": "https://bank.example/b"},
            {"bank_id": "bank", "registration_required": True, "registration_url": "https://bank.example/c/register", "source_url": "https://bank.example/c"},
            {"bank_id": "bank", "registration_required": True, "registration_url": "https://bank.example/d", "source_url": "https://bank.example/d"},
        ]
        classify_registration_urls(activities)
        self.assertEqual(activities[0]["registration_url_kind"], "bank_portal")
        self.assertEqual(activities[1]["registration_url_kind"], "bank_portal")
        self.assertEqual(activities[2]["registration_url_kind"], "activity_specific")
        self.assertEqual(activities[3]["registration_url_kind"], "unknown")

    def test_public_artifact_strips_bookkeeping_and_writes_ledger(self) -> None:
        candidate = candidate_payload(failed=0, active=1)
        candidate["activities"] = [{
            "id": "offer", "title": "活動", "source_entry_url": "https://bank.example",
            "bank_id": "bank-0", "bank_name": "銀行 0", "registration_required": True,
            "source_fingerprint": "abc", "observed_at": "2026-08-04T10:00:00+08:00",
            "last_detail_checked_at": "2026-08-04T09:00:00+08:00", "official_status": "published",
            "lifecycle": "active", "high_return": True,
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_payload(candidate, None, root / "public.json", root / "report.json", root / "cache.json")
            public = json.loads((root / "public.json").read_text())
            report = json.loads((root / "report.json").read_text())
            cache = json.loads((root / "cache.json").read_text())
        self.assertNotIn("source_fingerprint", public["activities"][0])
        self.assertNotIn("lifecycle", public["activities"][0])
        self.assertNotIn("high_return", public["activities"][0])
        self.assertEqual(public["catalog"]["registration_index_count"], 1)
        self.assertEqual(report["activities"][0]["source_fingerprint"], "abc")
        self.assertEqual(cache["activities"]["offer"]["fingerprint"], "abc")

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

    def test_blocks_single_large_source_regression_below_global_threshold(self) -> None:
        candidate = candidate_payload(failed=1, active=859)
        previous = candidate_payload(failed=0, active=1071)
        previous["source_health"][0]["activity_count"] = 212

        guard = assess_publish_guard(candidate, previous)

        self.assertTrue(guard["blocked"])
        self.assertIn("source_activity_regression", guard["reason_codes"])
        self.assertNotIn("catastrophic_activity_regression", guard["reason_codes"])

    def test_activity_drop_excludes_retained_fallback_activities(self) -> None:
        candidate = candidate_payload(failed=16, active=1110)
        candidate["cache"] = {"source_fallback_activities": 1069}
        previous = candidate_payload(failed=0, active=1110)

        guard = assess_publish_guard(candidate, previous)

        self.assertEqual(guard["candidate_active_or_upcoming"], 41)
        self.assertEqual(guard["activity_drop_percent"], 96.3)
        self.assertIn("catastrophic_activity_regression", guard["reason_codes"])

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

    def test_one_invalid_detail_source_still_publishes_other_sources(self) -> None:
        rejected_url = "http://10.100.6.38/frontend/bonusDetail.jsp?id=3450"
        candidate = {
            "generated_at": "2026-08-04T12:00:00+08:00",
            "summary": {"active_or_upcoming": 1, "total": 1},
            "source_health": [
                {
                    "id": "healthy-bank",
                    "bank_name": "正常銀行",
                    "status": "complete",
                    "activity_count": 1,
                    "message": "",
                },
                {
                    "id": "chb",
                    "bank_name": "彰化銀行",
                    "status": "failed",
                    "activity_count": 0,
                    "message": (
                        "1 個官方活動明細暫時無法讀取；"
                        f"官方頁輸出不允許的明細 URL，已拒絕並跳過：{rejected_url}"
                    ),
                },
            ],
            "alerts": [{
                "type": "source_emitted_invalid_url",
                "bank_name": "彰化銀行",
                "message": "該筆已跳過",
                "old_url": rejected_url,
                "new_url": "",
            }],
            "activities": [{
                "id": "healthy-offer",
                "bank_id": "healthy-bank",
                "registration_required": True,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_code = persist_payload(
                candidate,
                None,
                root / "promotions.json",
                root / "latest.json",
            )

            published = json.loads((root / "promotions.json").read_text())

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            [item["id"] for item in published["activities"]],
            ["healthy-offer"],
        )
        self.assertEqual(published["publish_guard"]["status"], "passed")
        self.assertEqual(published["alerts"][0]["old_url"], rejected_url)

    def test_raw_terms_move_to_lazy_detail_and_rehydrate_for_cache(self) -> None:
        candidate = candidate_payload(failed=0, active=1)
        candidate["activities"] = [{
            "id": "offer",
            "bank_id": "bank-0",
            "bank_name": "銀行 0",
            "registration_required": True,
            "terms_raw": "參加資格：限正卡持卡人",
            "terms_sections": {"eligibility": "參加資格：限正卡持卡人"},
        }]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exit_code = persist_payload(
                candidate,
                None,
                root / "promotions.json",
                root / "latest.json",
            )
            report = json.loads((root / "latest.json").read_text())
            public = json.loads((root / "promotions.json").read_text())
            detail = json.loads((root / "activities" / "offer.json").read_text())
            rehydrated = load_previous_public_payload(root / "promotions.json")

        self.assertEqual(exit_code, 0)
        self.assertIn("terms_raw", report["activities"][0])
        self.assertNotIn("terms_raw", public["activities"][0])
        self.assertEqual(public["activities"][0]["detail_ref"], "activities/offer.json")
        self.assertEqual(
            detail["terms_raw"],
            "參加資格：限正卡持卡人",
        )
        self.assertEqual(
            rehydrated["activities"][0]["terms_sections"]["eligibility"],
            "參加資格：限正卡持卡人",
        )

    def test_unchanged_detail_keeps_its_original_generated_time(self) -> None:
        candidate = candidate_payload(failed=0, active=1)
        candidate["generated_at"] = "2026-08-15T10:00:00+08:00"
        candidate["activities"] = [{
            "id": "offer",
            "bank_id": "bank-0",
            "bank_name": "銀行 0",
            "registration_required": True,
            "terms_raw": "活動條款",
        }]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "promotions.json"
            _write_public_artifacts(candidate, output)
            candidate["generated_at"] = "2026-08-16T10:00:00+08:00"
            _write_public_artifacts(candidate, output)
            detail = json.loads(
                (output.parent / "activities" / "offer.json").read_text()
            )

        self.assertEqual(detail["generated_at"], "2026-08-15T10:00:00+08:00")

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
