from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from card_promotions_monitor.extractors import (
    _class_text,
    _activity_periods,
    _chb_cards,
    _compact_period,
    _categories,
    _ctbc_card_blocks,
    _firstbank_category_requests,
    _firstbank_rest_cards,
    _firstbank_rest_page,
    _fubon_cards,
    _fubon_page_listener,
    _has_registration_requirement,
    _html_segments,
    _hncb_credit_card_cards,
    _kgi_cards,
    _listing_promotions,
    _lifecycle,
    _megabank_cards,
    _parse_period,
    _promotion_invariants,
    _roc_period,
    _registration_windows,
    _reward_values,
    _reward_tiers,
    _subactivity_blocks,
    _terms_content,
    _taishin_cards,
    _tcbbank_api_cards,
    _ubot_cards,
    _yuanta_cards,
    _esun_cards,
    extract_chb,
    extract_first,
    extract_megabank,
    normalize_registration_url,
    reuse_cached_promotion,
)
from card_promotions_monitor.cache import new_cache_stats, source_fingerprint
from card_promotions_monitor.fetch import FetchResult
from card_promotions_monitor.models import Promotion, RegistrationWindow


class RegistrationWindowTests(unittest.TestCase):
    def test_cache_hit_reparses_official_terms_with_current_parser(self) -> None:
        fingerprint = source_fingerprint("bank", {"listing": "same"})
        promotion = Promotion(
            id="bank-offer", bank_id="bank", bank_name="測試銀行", title="測試活動",
            merchant="測試商店", categories=["網購"], start_date="2026-08-01",
            end_date="2026-08-31", summary="最高 3% 回饋",
            source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-01T10:00:00+08:00",
            registration_required=True,
            registration_windows=[RegistrationWindow(
                start="2026-08-07T17:00:00+08:00",
                end="2026-08-07T17:15:00+08:00",
                label="舊解析結果", source_text="舊解析結果",
            )],
            terms_raw="登錄時間：2026/8/7 17:00:00~2026/8/31 23:59:00\n最高 12% 回饋",
            max_reward_percent=3,
            source_fingerprint=fingerprint,
            last_detail_checked_at="2026-08-01T10:00:00+08:00",
        )
        stats = new_cache_stats()

        value = reuse_cached_promotion(
            {promotion.id: promotion.to_dict()},
            activity_id=promotion.id,
            fingerprint=fingerprint,
            now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            source_entry_url="https://bank.example",
            percent_threshold=10,
            amount_threshold=500,
            stats=stats,
            avoids_detail_request=True,
        )

        self.assertIsNotNone(value)
        self.assertEqual(value.registration_windows[0].end, "2026-08-31T23:59:00+08:00")
        self.assertEqual(value.max_reward_percent, 3)
        self.assertFalse(value.high_return)
        self.assertEqual(stats["reparsed_activities"], 1)
        self.assertEqual(stats["detail_requests_avoided"], 1)

    def test_cache_reparse_uses_matching_current_activity_period(self) -> None:
        fingerprint = source_fingerprint("bank", {"listing": "same"})
        promotion = Promotion(
            id="bank-recurring", bank_id="bank", bank_name="測試銀行", title="每月活動",
            merchant="測試商店", categories=["生活消費"], start_date="2024-01-01",
            end_date="2026-09-30", summary="每月登錄",
            source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-01T10:00:00+08:00",
            registration_required=True,
            terms_raw="活動暨登錄期間：115/7/1-9/30，每月5日下午2點開放登錄",
            activity_periods=[
                {"start": "2024-01-01", "end": "2024-06-30", "label": "舊活動"},
                {"start": "2026-07-01", "end": "2026-09-30", "label": "目前活動"},
            ],
            source_fingerprint=fingerprint,
            last_detail_checked_at="2026-08-01T10:00:00+08:00",
        )

        value = reuse_cached_promotion(
            {promotion.id: promotion.to_dict()},
            activity_id=promotion.id,
            fingerprint=fingerprint,
            now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            source_entry_url="https://bank.example",
            percent_threshold=10,
            amount_threshold=500,
            stats=new_cache_stats(),
            avoids_detail_request=True,
        )

        self.assertEqual(
            [window.start for window in value.registration_windows],
            [
                "2026-07-05T14:00:00+08:00",
                "2026-08-05T14:00:00+08:00",
                "2026-09-05T14:00:00+08:00",
            ],
        )

    def test_cache_reparse_does_not_merge_other_subactivity_windows(self) -> None:
        fingerprint = source_fingerprint("bank", {"listing": "same"})
        promotion = Promotion(
            id="bank-multi", bank_id="bank", bank_name="測試銀行", title="主活動",
            merchant="測試商店", categories=["網購"], start_date="2026-08-01",
            end_date="2026-08-31", summary="主活動",
            source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-01T10:00:00+08:00",
            registration_required=True,
            registration_windows=[RegistrationWindow(
                start="2026-08-25T16:00:00+08:00", end="2026-08-25T16:15:00+08:00",
                label="舊解析結果", source_text="舊解析結果",
            )],
            terms_raw=(
                "活動一 登錄時間：2026/8/25 16:00~2026/8/31 23:59\n"
                "活動二 登錄時間：2026/8/9 12:00~2026/8/10 12:00"
            ),
            source_fingerprint=fingerprint,
            last_detail_checked_at="2026-08-01T10:00:00+08:00",
        )

        value = reuse_cached_promotion(
            {promotion.id: promotion.to_dict()},
            activity_id=promotion.id,
            fingerprint=fingerprint,
            now=datetime(2026, 8, 10, 10, 0, tzinfo=ZoneInfo("Asia/Taipei")),
            source_entry_url="https://bank.example",
            percent_threshold=10,
            amount_threshold=500,
            stats=new_cache_stats(),
            avoids_detail_request=True,
        )

        self.assertEqual(len(value.registration_windows), 1)
        self.assertEqual(value.registration_windows[0].end, "2026-08-31T23:59:00+08:00")
        self.assertTrue(value.needs_review)
        self.assertIn("本頁含多個活動", value.review_message)

    def test_splits_repeated_activity_headings_before_notes(self) -> None:
        blocks = _subactivity_blocks(
            "頁首\n【活動一】第一波\n活動日期：2026/8/2\n登錄辦法：8/7 17:00\n"
            "【活動二】第二波\n活動期間：2026/8/3~2026/8/15\n登錄辦法：8/20 17:00\n"
            "注意事項\n【活動一】重複說明"
        )
        self.assertEqual(
            [heading for heading, _ in blocks],
            ["活動一 第一波", "活動二 第二波"],
        )

    def test_extracts_four_reward_tiers(self) -> None:
        text = (
            "35,000元\n700元\n800元\n100名\n"
            "50,000元\n1,000元\n1,100元\n100名\n"
            "65,000元\n1,300元\n2,100元\n100名\n"
            "75,000元\n1,500元\n2,500元\n50名"
        )
        tiers = _reward_tiers(text)
        self.assertEqual(len(tiers), 4)
        self.assertEqual(tiers[2]["spend_amount_twd"], 65000)
        self.assertEqual(tiers[2]["installment_reward_amount_twd"], 2100)
        self.assertEqual(tiers[3]["quota"], 50)

    def test_extracts_multiple_roc_activity_waves(self) -> None:
        periods = _activity_periods(
            "活動期間：\n第一波 115/7/30-8/31\n第二波 115/9/1-10/31",
            2026,
        )
        self.assertEqual(len(periods), 2)
        self.assertEqual(periods[0]["start"].isoformat(), "2026-07-30")
        self.assertEqual(periods[1]["end"].isoformat(), "2026-10-31")

    def test_marks_out_of_period_registration_for_manual_review(self) -> None:
        promotion = Promotion(
            id="offer", bank_id="bank", bank_name="銀行", title="活動",
            merchant="商店", categories=["網購"], start_date="2026-08-01",
            end_date="2026-08-15", summary="摘要", source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-04T00:00:00+08:00",
            registration_required=True,
            registration_windows=[RegistrationWindow(
                start="2026-08-20T17:00:00+08:00", end=None,
                label="登錄開放", source_text="8/20 17:00 開放登錄",
            )],
        )
        _promotion_invariants(promotion)
        self.assertTrue(promotion.needs_review)
        self.assertIn("晚於活動期間", promotion.review_message)

    def test_marks_missing_registration_time_and_period_for_review(self) -> None:
        promotion = Promotion(
            id="offer", bank_id="bank", bank_name="銀行", title="活動",
            merchant="商店", categories=["網購"], start_date="2026-08-01",
            end_date=None, summary="摘要", source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-04T00:00:00+08:00",
            registration_required=True,
        )

        _promotion_invariants(promotion)

        self.assertTrue(promotion.needs_review)
        self.assertTrue(promotion.review_required)
        self.assertIn("活動截止日尚未確認", promotion.review_message)
        self.assertIn("尚未取得可確認的登錄時點", promotion.review_message)
        self.assertEqual(promotion.registration_timing_contracts, ["unknown"])

    def test_extracts_explicit_registration_timing_contracts(self) -> None:
        promotion = Promotion(
            id="offer", bank_id="bank", bank_name="銀行", title="活動",
            merchant="商店", categories=["網購"], start_date="2026-08-01",
            end_date="2026-09-30", summary="摘要", source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-04T00:00:00+08:00",
            registration_required=True,
            terms_raw=(
                "不提供登錄前之消費回饋。需先消費後登錄。"
                "本活動需每月登錄。"
            ),
            registration_windows=[RegistrationWindow(
                start="2026-08-01T10:00:00+08:00",
                end="2026-08-31T23:59:00+08:00",
                label="登錄期間", source_text="8月登錄期間",
            )],
        )

        _promotion_invariants(promotion)

        self.assertEqual(
            promotion.registration_timing_contracts,
            [
                "register_before_spend",
                "retroactive_ok",
                "per_period_reregister",
                "registration_closes_early",
            ],
        )

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
        self.assertEqual(
            normalize_registration_url("ubot", "https://newnewbank.com.tw/card/register"),
            "https://newnewbank.com.tw/card/register",
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

    def test_same_start_prefers_confirmed_range_over_start_only_duplicate(self) -> None:
        values = _registration_windows(
            "登錄辦法：2026/8/20 17:00~2026/8/31 23:59 開放登錄。"
            "8/20 17:00 開放登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-20T17:00:00+08:00")
        self.assertEqual(values[0].end, "2026-08-31T23:59:00+08:00")

    def test_parses_second_precision_range(self) -> None:
        values = _registration_windows(
            "登錄時間：2026/8/7 17:00:00~2026/8/31 23:59:00",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-07T17:00:00+08:00")
        self.assertEqual(values[0].end, "2026-08-31T23:59:00+08:00")

    def test_parses_full_year_dot_date_range_without_inventing_time(self) -> None:
        values = _registration_windows(
            "登錄期間：2026.1.1~2026.12.31(每月登錄，額滿即關閉登錄功能)",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-01-01")
        self.assertEqual(values[0].end, "2026-12-31")
        self.assertEqual(values[0].precision, "date")

    def test_does_not_treat_short_decimal_values_as_dates(self) -> None:
        values = _registration_windows(
            "活動回饋享2.5%，上限1.5萬元，需完成登錄。",
            2026,
        )
        self.assertEqual(values, [])

    def test_parses_roc_registration_date_without_inventing_time(self) -> None:
        values = _registration_windows(
            "本活動採登錄制，115年8月20日開放登錄。",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-20")
        self.assertIsNone(values[0].end)
        self.assertEqual(values[0].precision, "date")

    def test_parses_firstbank_parenthesized_monthly_opening(self) -> None:
        values = _registration_windows(
            "登錄期間：每月22日上午10點起(逐月登錄，額滿即關閉)",
            2026,
            date(2026, 8, 1),
            date(2026, 10, 31),
        )
        self.assertEqual(
            [value.start for value in values],
            [
                "2026-08-22T10:00:00+08:00",
                "2026-09-22T10:00:00+08:00",
                "2026-10-22T10:00:00+08:00",
            ],
        )

    def test_invariants_handle_mixed_date_and_datetime_windows(self) -> None:
        promotion = Promotion(
            id="bank-mixed", bank_id="bank", bank_name="測試銀行", title="混合精度",
            merchant="測試", categories=["網購"], start_date="2026-08-01",
            end_date="2026-08-31", summary="測試", source_url="https://bank.example/offer",
            source_entry_url="https://bank.example", observed_at="2026-08-01T10:00:00+08:00",
            registration_required=True,
            registration_windows=[
                RegistrationWindow(
                    "2026-08-01", "2026-08-15", "登錄期間", "日期", precision="date",
                ),
                RegistrationWindow(
                    "2026-08-20T10:00:00+08:00", None, "登錄開放", "時間",
                ),
            ],
            needs_review=True,
            review_required=True,
            review_message=(
                "官方註明需登錄，但尚未取得可確認的登錄時點。 "
                "同一子活動的登錄視窗互相重疊。"
            ),
        )
        _promotion_invariants(promotion)
        self.assertNotIn("互相重疊", promotion.review_message)
        self.assertNotIn("尚未取得", promotion.review_message)
        self.assertFalse(promotion.needs_review)

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

    def test_parses_megabank_limited_registration_opening(self) -> None:
        values = _registration_windows(
            "momo購物網活動於115/8/27 14:00開放限量登錄",
            2026,
        )
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0].start, "2026-08-27T14:00:00+08:00")
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

    def test_hncb_shared_detail_does_not_assign_one_date_to_multiple_activities(self) -> None:
        detail_url = "https://www.hncb.com.tw/wps/portal/HNCB/card/shared"
        source = {
            "id": "hncb",
            "bank_name": "華南銀行",
            "entry_url": "https://www.hncb.com.tw/wps/portal/HNCB/card",
            "official_domains": ["hncb.com.tw"],
        }
        cards = [
            {
                "id": f"hncb-{index}",
                "title": title,
                "summary": title,
                "url": detail_url,
                "start": date(2026, 8, 1),
                "end": date(2026, 8, 31),
                "fingerprint": f"fingerprint-{index}",
                "fetch_detail": True,
                "ambiguous_shared_detail": True,
            }
            for index, title in enumerate(("活動甲", "活動乙"), start=1)
        ]
        detail = "<h1>多活動專頁</h1><p>活動採登錄制，115年8月20日開放登錄。</p>"
        with patch(
            "card_promotions_monitor.extractors._fetch_many",
            return_value={
                detail_url: FetchResult(
                    detail_url, detail_url, 200, detail, "text/html", "fixture"
                )
            },
        ):
            activities, failed_details, invalid_urls = _listing_promotions(
                source,
                cards,
                now=datetime(2026, 8, 16, tzinfo=ZoneInfo("Asia/Taipei")),
                percent_threshold=10,
                amount_threshold=500,
                activity_cache=None,
                cache_stats=None,
            )
        self.assertEqual(failed_details, 0)
        self.assertEqual(invalid_urls, [])
        self.assertEqual(len(activities), 2)
        self.assertTrue(all(activity.registration_required for activity in activities))
        self.assertTrue(all(not activity.registration_windows for activity in activities))
        self.assertTrue(all("無法可靠對應" in activity.review_message for activity in activities))

    def test_listing_prefers_precise_firstbank_windows_over_broad_date_range(self) -> None:
        source = {
            "id": "first",
            "bank_name": "第一銀行",
            "entry_url": "https://card.firstbank.com.tw/sites/card/touch/1565690686288",
            "official_domains": ["firstbank.com.tw"],
        }
        url = "https://card.firstbank.com.tw/sites/card/zh_TW/offer"
        cards = [{
            "id": "first-offer",
            "title": "每月登錄活動",
            "summary": "需登錄",
            "url": url,
            "start": date(2026, 8, 1),
            "end": date(2026, 10, 31),
            "fingerprint": "fingerprint",
            "fetch_detail": False,
            "prefer_precise_registration_windows": True,
            "detail_html": (
                "<p>登錄期間：2026.8.1~2026.10.31</p>"
                "<p>每月22日上午10點起(逐月登錄，額滿即關閉)</p>"
            ),
        }]
        activities, failed_details, _ = _listing_promotions(
            source,
            cards,
            now=datetime(2026, 8, 16, tzinfo=ZoneInfo("Asia/Taipei")),
            percent_threshold=10,
            amount_threshold=500,
            activity_cache=None,
            cache_stats=None,
        )
        self.assertEqual(failed_details, 0)
        self.assertEqual(
            [window.start for window in activities[0].registration_windows],
            [
                "2026-08-22T10:00:00+08:00",
                "2026-09-22T10:00:00+08:00",
                "2026-10-22T10:00:00+08:00",
            ],
        )
        self.assertTrue(all(
            window.precision == "datetime"
            for window in activities[0].registration_windows
        ))

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

    def test_extracts_firstbank_rest_categories_and_activity_fields(self) -> None:
        listing = """
        <div class="row showAjaxActivityData" data-category_id_list = '0'
          data-all_category_asset_id = '1565690672041'
          data-category_en_name = 'all_category'>
        <div class="row showAjaxActivityData"
          data-category_id_list = '1565690671943'
          data-all_category_asset_id = '1565690672041'
          data-category_en_name = 'online_store'>
        """
        requests = _firstbank_category_requests(listing)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["categoryEnName"], "online_store")

        xml = """
        <HashMap>
          <activityListData>
            <activityTitle>momo購物網</activityTitle>
            <subheadlineEditor>&lt;div&gt;單筆滿萬最高回饋5%&lt;/div&gt;</subheadlineEditor>
            <activityContent>&lt;p&gt;活動須登錄&lt;/p&gt;</activityContent>
            <activityDate>2026.7.1~2026.12.31</activityDate>
            <loginDate>2026/8/14 16:00~2026/8/31 23:59</loginDate>
            <activityUrl>/sites/card/zh_TW/1565702005868</activityUrl>
          </activityListData>
          <pageSpacingHtml>&lt;a title='第2頁'&gt;2&lt;/a&gt;</pageSpacingHtml>
        </HashMap>
        """
        rows, max_page = _firstbank_rest_page(xml)
        cards = _firstbank_rest_cards(
            rows,
            "https://card.firstbank.com.tw/sites/card/touch/1565690686288",
            "first",
            date(2026, 8, 16),
            "online_store",
        )
        self.assertEqual(max_page, 2)
        self.assertEqual(cards[0]["title"], "momo購物網")
        self.assertEqual(cards[0]["start"], date(2026, 7, 1))
        self.assertEqual(cards[0]["end"], date(2026, 12, 31))
        self.assertEqual(cards[0]["base_category"], "網購")
        self.assertFalse(cards[0]["fetch_detail"])

    def test_firstbank_extractor_uses_official_rest_pages(self) -> None:
        listing = """
        <div class="row showAjaxActivityData"
          data-category_id_list = '1565690671943'
          data-all_category_asset_id = '1565690672041'
          data-category_en_name = 'online_store'>
        """

        def xml_page(activity_id: str, title: str, *, page_two: bool) -> str:
            pagination = "" if page_two else "&lt;a title='第2頁'&gt;2&lt;/a&gt;"
            return f"""
            <HashMap>
              <activityListData>
                <activityTitle>{title}</activityTitle>
                <subheadlineEditor>&lt;div&gt;滿額最高回饋5%&lt;/div&gt;</subheadlineEditor>
                <activityContent>&lt;p&gt;活動須登錄&lt;/p&gt;</activityContent>
                <activityDate>2026.7.1~2026.12.31</activityDate>
                <loginDate>2026.1.1~2026.12.31(每月登錄，額滿即關閉登錄功能)</loginDate>
                <activityUrl>/sites/card/zh_TW/{activity_id}</activityUrl>
              </activityListData>
              <pageSpacingHtml>{pagination}</pageSpacingHtml>
            </HashMap>
            """

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def fetch_text(self, url, *, data=None, **kwargs):
                if data is None:
                    text = listing
                else:
                    page_two = data["pageNumberSel"] == "2"
                    text = xml_page(
                        "1565702005869" if page_two else "1565702005868",
                        "第二頁活動" if page_two else "第一頁活動",
                        page_two=page_two,
                    )
                return FetchResult(url, url, 200, text, "text/xml", "hash")

        source = {
            "id": "first",
            "bank_name": "第一銀行",
            "entry_url": "https://card.firstbank.com.tw/sites/card/touch/1565690686288",
            "official_domains": ["firstbank.com.tw"],
        }
        with patch(
            "card_promotions_monitor.extractors.SystemCurlSession",
            FakeSession,
        ):
            activities, health, alerts = extract_first(
                source,
                now=datetime(2026, 8, 16, tzinfo=ZoneInfo("Asia/Taipei")),
                percent_threshold=10,
                amount_threshold=500,
            )

        self.assertEqual(len(activities), 2)
        self.assertEqual(health.status, "complete")
        self.assertEqual(health.activity_count, 2)
        self.assertEqual(alerts, [])
        self.assertEqual(activities[0].start_date, "2026-07-01")
        self.assertEqual(activities[0].end_date, "2026-12-31")
        self.assertEqual(len(activities[0].registration_windows), 1)
        self.assertEqual(activities[0].registration_windows[0].precision, "date")
        self.assertEqual(
            activities[0].registration_url,
            "https://ccard.firstbank.com.tw/cmsweb/act/ca_index",
        )

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

    def test_megabank_cards_keep_only_current_registration_items(self) -> None:
        rows = [
            {
                "Title": "八月網購回饋",
                "Description": "活動期間：115/8/1~8/31",
                "Tags": ["強檔", "需登錄"],
                "Removal": "",
                "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/august">了解更多</a>',
                "KeyValues": {"discount": ["Entry", "Shopping"]},
            },
            {
                "Title": "一般免登錄優惠",
                "Description": "活動期間：115/8/1~8/31",
                "Tags": ["強檔"],
                "Removal": "",
                "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/general">了解更多</a>',
            },
            {
                "Title": "七月已結束活動",
                "Description": "活動期間：115/7/1~7/31",
                "Tags": ["需登錄"],
                "Removal": "",
                "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/july">了解更多</a>',
            },
            {
                "Title": "官網已下架活動",
                "Description": "活動期間：115/8/1~8/31",
                "Tags": ["需登錄"],
                "Removal": "c-card--disabled",
                "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/removed">了解更多</a>',
            },
            {
                "Title": "信用卡服務登錄",
                "Description": "",
                "Tags": ["需登錄"],
                "Removal": "",
                "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/service">了解更多</a>',
            },
        ]

        cards = _megabank_cards(
            rows,
            "https://www.megabank.com.tw/personal/credit-card/event/overview",
            "megabank",
            ["megabank.com.tw"],
            date(2026, 8, 15),
        )

        self.assertEqual([card["title"] for card in cards], ["八月網購回饋", "信用卡服務登錄"])
        self.assertEqual(cards[0]["end"], date(2026, 8, 31))
        self.assertTrue(cards[0]["registration_required"])
        self.assertTrue(cards[0]["featured"])
        self.assertEqual(cards[0]["registration_url"], cards[0]["url"])

    def test_megabank_extractor_preserves_activity_specific_registration_page(self) -> None:
        source = {
            "id": "megabank",
            "bank_name": "兆豐銀行",
            "entry_url": "https://www.megabank.com.tw/personal/credit-card/event/overview?Tag=Tag",
            "api_url": "https://www.megabank.com.tw/api/client/DiscountOverview/GetDiscount",
            "api_item_id": "{item}",
            "api_setting_id": "{setting}",
            "official_domains": ["megabank.com.tw"],
        }
        row = {
            "Title": "網購爸氣刷",
            "Description": "活動期間：115/8/1~8/31",
            "Tags": ["需登錄"],
            "Removal": "",
            "DetailPageLinkHtml": '<a href="/personal/credit-card/event/overview/ec202608">了解更多</a>',
            "KeyValues": {"discount": ["Entry", "Shopping"]},
        }
        detail = (
            "<h1>網購爸氣刷</h1><p>活動期間：115/8/1~8/31</p><p>更多優惠</p>"
            "<p>回饋上限NT$2,200，本活動於115/8/27 14:00開放限量登錄</p>"
            "<p>您可能有興趣</p><p>其他活動最高回饋NT$5,000</p>"
            "<p>謹慎理財 信用至上，循環利率上限15%</p>"
        )

        def fake_fetch(url: str, _domains: list[str], **_kwargs) -> FetchResult:
            html = detail if url.endswith("ec202608") else "<h1>優惠總覽</h1>"
            return FetchResult(url, url, 200, html, "text/html", "fixture")

        with (
            patch("card_promotions_monitor.extractors.fetch_text", side_effect=fake_fetch),
            patch(
                "card_promotions_monitor.extractors.fetch_json",
                return_value=(FetchResult(source["api_url"], source["api_url"], 200, "[]", "application/json", "fixture"), [row]),
            ),
        ):
            activities, health, alerts = extract_megabank(
                source,
                now=datetime(2026, 8, 15, 12, 0, tzinfo=ZoneInfo("Asia/Taipei")),
                percent_threshold=10,
                amount_threshold=500,
            )

        self.assertEqual(len(activities), 1)
        self.assertEqual(health.status, "complete")
        self.assertEqual(alerts, [])
        self.assertEqual(activities[0].registration_url, activities[0].source_url)
        self.assertEqual(activities[0].max_reward_amount_twd, 2200)
        self.assertIsNone(activities[0].max_reward_percent)
        self.assertEqual(
            [window.start for window in activities[0].registration_windows],
            ["2026-08-27T14:00:00+08:00"],
        )

    def test_megabank_empty_success_response_is_structure_failure(self) -> None:
        source = {
            "id": "megabank",
            "bank_name": "兆豐銀行",
            "entry_url": "https://www.megabank.com.tw/personal/credit-card/event/overview?Tag=Tag",
            "api_url": "https://www.megabank.com.tw/api/client/DiscountOverview/GetDiscount",
            "api_item_id": "{item}",
            "api_setting_id": "{setting}",
            "official_domains": ["megabank.com.tw"],
        }
        listing = FetchResult(
            source["entry_url"], source["entry_url"], 200,
            "<h1>優惠總覽</h1>", "text/html", "fixture",
        )
        api = FetchResult(
            source["api_url"], source["api_url"], 200,
            "[]", "application/json", "fixture",
        )
        with (
            patch("card_promotions_monitor.extractors.fetch_text", return_value=listing),
            patch("card_promotions_monitor.extractors.fetch_json", return_value=(api, [])),
        ):
            activities, health, alerts = extract_megabank(
                source,
                now=datetime(2026, 8, 16, tzinfo=ZoneInfo("Asia/Taipei")),
                percent_threshold=10,
                amount_threshold=500,
            )
        self.assertEqual(activities, [])
        self.assertEqual(health.status, "failed")
        self.assertIn("Sitecore", health.message)
        self.assertEqual(alerts[0].type, "source_structure_changed")


if __name__ == "__main__":
    unittest.main()
