from datetime import datetime
from unittest import TestCase
from unittest.mock import patch
from zoneinfo import ZoneInfo

from card_promotions_monitor.extractors import extract_obank
from card_promotions_monitor.fetch import FetchResult


class ObankDetailLinkTests(TestCase):
    def test_related_offer_before_details_does_not_collide_or_drop_offers(self):
        root = 'https://www.o-bank.com'
        old = root + '/retail/event/event-announce/related'
        new = root + '/retail/event/event-announce/own'
        html = ('<div id="Content_divContent_1">'
                '<article class="o-article"><h3 class="heading">活動甲</h3>'
                '<div class="description">2026/07/01~2026/09/30 11%現金回饋</div>'
                f'<a href="{old}">活動詳情</a></article>'
                '<article class="o-article"><h3 class="heading">活動乙</h3>'
                '<div class="description">2026/07/01~2026/09/30 100%現金回饋'
                f'<a href="{old}">另外參加活動甲</a></div><a href="{new}">活動詳情&gt;&gt;</a></article>'
                '</div><div id="Content_divContent_2"></div>')
        url = root + '/retail/event/event-compaign'
        source = dict(id='obank', bank_name='王道銀行', entry_url=url, official_domains=['o-bank.com'])
        with patch('card_promotions_monitor.extractors.fetch_text', return_value=FetchResult(url,url,200,html,'text/html','test')):
            rows, health, _ = extract_obank(source,now=datetime(2026,9,13,tzinfo=ZoneInfo('Asia/Taipei')),
                                           percent_threshold=10,amount_threshold=500)
        self.assertEqual(health.status, 'complete')
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({row.id for row in rows}), 2)
        self.assertEqual([row.source_url for row in rows], [old,new])
