from __future__ import annotations

import unittest

from card_promotions_monitor.html_tools import parse_page, strip_html


class HtmlToolsTests(unittest.TestCase):
    def test_extracts_data_link_and_text(self) -> None:
        page = parse_page(
            '<a href="#modal" data-link="https://bank.example/register">立即登錄</a>',
            "https://bank.example/campaign",
        )
        self.assertEqual(page.links[0]["url"], "https://bank.example/register")
        self.assertEqual(page.links[0]["text"], "立即登錄")

    def test_strips_markup(self) -> None:
        self.assertEqual(strip_html("<p>登錄&nbsp;時間</p>"), "登錄 時間")


if __name__ == "__main__":
    unittest.main()

