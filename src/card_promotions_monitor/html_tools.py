from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin


class PageParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self._link: dict[str, str] | None = None
        self._in_title = False
        self._in_heading = False
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = True
        if tag in {"h1", "h2", "h3"}:
            self._in_heading = True
        if tag == "a":
            href = values.get("data-link") or values.get("href") or ""
            try:
                resolved_url = urljoin(self.base_url, href)
            except ValueError:
                resolved_url = ""
            self._link = {"url": resolved_url, "text": ""}
        if tag in {"br", "p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
            return
        if self._ignored_depth:
            return
        if tag == "title":
            self._in_title = False
        if tag in {"h1", "h2", "h3"}:
            self._in_heading = False
        if tag == "a" and self._link is not None:
            self._link["text"] = clean_inline(self._link["text"])
            self.links.append(self._link)
            self._link = None
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = html.unescape(data)
        self.text_parts.append(value)
        if self._in_title:
            self.title_parts.append(value)
        if self._in_heading:
            self.heading_parts.append(value)
        if self._link is not None:
            self._link["text"] += value

    @property
    def text(self) -> str:
        lines = [clean_inline(item) for item in "".join(self.text_parts).splitlines()]
        return "\n".join(item for item in lines if item)

    @property
    def title(self) -> str:
        return clean_inline(" ".join(self.title_parts))

    @property
    def headings(self) -> str:
        return clean_inline(" ".join(self.heading_parts))


def clean_inline(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def parse_page(text: str, base_url: str) -> PageParser:
    parser = PageParser(base_url)
    parser.feed(text)
    return parser


def strip_html(value: str) -> str:
    parser = PageParser("https://invalid.local/")
    parser.feed(value)
    return clean_inline(parser.text)


def walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_strings(item)
