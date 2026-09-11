"""Offline regressions for scheduled promotion processing."""
import email
from datetime import datetime, date, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
NOW = datetime(2026, 9, 11, 12, tzinfo=ZoneInfo("Asia/Taipei"))
from card_promotions_monitor import cache, cli as card_cli, extractors, fetch
from card_promotions_monitor.models import Promotion

def promotion(**kwargs):
    data = dict(id='demo', bank_id='kgi', bank_name='Demo', title='Demo', merchant='Demo',
                categories=[], start_date='2026-08-01', end_date='2026-12-31', summary='Demo',
                source_url='https://example.com/detail', source_entry_url='https://example.com',
                observed_at=NOW.isoformat(), last_detail_checked_at=NOW.isoformat(),
                source_fingerprint='fingerprint', terms_raw='活動期間：2026/8/1～2026/12/31')
    data.update(kwargs)
    return Promotion(**data)

class CardReview(unittest.TestCase):
    def reuse(self, timestamp):
        return cache.reuse_cached_promotion({'demo':promotion(last_detail_checked_at=timestamp).to_dict()},
            activity_id='demo', fingerprint='fingerprint', now=NOW, source_entry_url='https://example.com',
            percent_threshold=10, amount_threshold=500, stats={}, avoids_detail_request=True)

    def test_C01_naive_cache_timestamp_is_miss(self):
        self.assertIsNone(self.reuse('2026-09-10T12:00:00'))

    def test_C02_future_cache_timestamp_is_miss(self):
        self.assertIsNone(self.reuse('2099-09-10T12:00:00+08:00'))

    def test_C03_failed_detail_not_cached_as_verified(self):
        source = dict(id='kgi', bank_name='Demo', entry_url='https://example.com', official_domains=['example.com'])
        card = dict(id='demo', title='Demo', summary='Demo', url='https://example.com/detail',
            start=date(2026,8,1), end=date(2026,12,31), fingerprint='fingerprint', fetch_detail=True)
        with patch.object(extractors, '_fetch_many', return_value={card['url']:RuntimeError('synthetic timeout')}):
            rows, failed, _ = extractors._listing_promotions(source, [card], now=NOW,
                percent_threshold=10, amount_threshold=500, activity_cache=None, cache_stats={})
        self.assertEqual(failed, 1)
        self.assertEqual(rows[0].last_detail_checked_at, '')
        self.assertTrue(rows[0].needs_review)

    def test_C04_empty_source_health_blocks_publish(self):
        result = card_cli.assess_publish_guard({'source_health':[], 'summary':{'active_or_upcoming':0}},
                                             {'summary':{'active_or_upcoming':100}})
        self.assertTrue(result['blocked'])

    def test_C05_reset_is_retried(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'ok'
        response.geturl.return_value = 'https://example.com'
        response.headers = email.message.Message()
        response.status = 200
        opener = Mock()
        opener.open.side_effect = [ConnectionResetError('synthetic'), response]
        with patch.object(fetch.urllib.request, 'build_opener', return_value=opener), patch.object(fetch.time, 'sleep'):
            value = fetch.fetch_text('https://example.com', ['example.com'])
        self.assertEqual(value.text, 'ok')
        self.assertEqual(opener.open.call_count, 2)

    def test_C06_recurring_invalid_time_does_not_abort(self):
        result = extractors._registration_windows('每月 5 日 99:00 開放登錄', 2026, date(2026,9,1), date(2026,12,31))
        self.assertEqual(result, [])

    def test_C07_official_end_not_revived_by_date(self):
        self.assertEqual(card_cli._lifecycle_for({'official_status':'ended_by_official',
            'start_date':'2026-08-01', 'end_date':'2026-12-31'}, NOW.date()), 'ended')

    def test_C08_date_only_not_counted_as_confirmed_time(self):
        health = [{'id':'kgi'}]
        card_cli.annotate_source_registration_coverage([{'bank_id':'kgi', 'registration_required':True,
            'registration_windows':[{'start':'2026-09-11', 'precision':'date'}]}], health)
        self.assertEqual(health[0]['registration_time_confirmed_count'], 0)
