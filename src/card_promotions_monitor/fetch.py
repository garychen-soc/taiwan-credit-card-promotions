from __future__ import annotations

import hashlib
import json
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 TaiwanCardPromotionMonitor/0.1"
)


@dataclass(slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    text: str
    content_type: str
    content_hash: str


def is_allowed_url(url: str, allowed_domains: list[str]) -> bool:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_domains: list[str]) -> None:
        super().__init__()
        self.allowed_domains = allowed_domains

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if not is_allowed_url(target, self.allowed_domains):
            raise ValueError(f"Redirected outside official domains: {target}")
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_text(
    url: str,
    allowed_domains: list[str],
    *,
    timeout: float = 25.0,
    attempts: int = 2,
    max_bytes: int = 8_000_000,
) -> FetchResult:
    domains = [item.lower().rstrip(".") for item in allowed_domains]
    if not is_allowed_url(url, domains):
        raise ValueError(f"URL is outside official domains: {url}")
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _SafeRedirectHandler(domains),
    )
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ValueError(f"Response exceeded {max_bytes} bytes")
                final_url = response.geturl()
                if not is_allowed_url(final_url, domains):
                    raise ValueError(f"Final URL is outside official domains: {final_url}")
                charset = response.headers.get_content_charset() or "utf-8"
                text = body.decode(charset, errors="replace")
                return FetchResult(
                    requested_url=url,
                    final_url=final_url,
                    status_code=response.status,
                    text=text,
                    content_type=response.headers.get_content_type(),
                    content_hash=hashlib.sha256(body).hexdigest(),
                )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
    if last_error and "CERTIFICATE_VERIFY_FAILED" in str(last_error):
        return _fetch_with_system_curl(
            url,
            domains,
            timeout=timeout,
            max_bytes=max_bytes,
        )
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def fetch_json(url: str, allowed_domains: list[str], **kwargs):
    result = fetch_text(url, allowed_domains, **kwargs)
    try:
        return result, json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Official endpoint did not return valid JSON: {url}") from exc


def _fetch_with_system_curl(
    url: str,
    allowed_domains: list[str],
    *,
    timeout: float,
    max_bytes: int,
) -> FetchResult:
    """Use the platform TLS trust store without disabling certificate checks."""
    marker = b"\n__CARD_PROMOTIONS_META__"
    current_url = url
    for _ in range(11):
        if not is_allowed_url(current_url, allowed_domains):
            raise ValueError(f"Redirected outside official domains: {current_url}")
        command = [
            "/usr/bin/curl",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(timeout))),
            "--max-filesize",
            str(max_bytes),
            "--user-agent",
            USER_AGENT,
            "--header",
            "Accept-Language: zh-TW,zh;q=0.9,en;q=0.5",
            "--write-out",
            marker.decode() + "%{http_code}\t%{url_effective}\t%{content_type}\t%{redirect_url}",
            current_url,
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=timeout + 5,
        )
        if completed.returncode != 0:
            error = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"System TLS fetch failed for {current_url}: {error}")
        try:
            body, metadata = completed.stdout.rsplit(marker, 1)
            status_text, effective_url, content_type, redirect_url = metadata.decode(
                "utf-8", errors="replace"
            ).split("\t", 3)
            status_code = int(status_text)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"Could not parse system TLS response for {current_url}") from exc
        if 300 <= status_code < 400 and redirect_url:
            target = urllib.parse.urljoin(effective_url or current_url, redirect_url)
            if not is_allowed_url(target, allowed_domains):
                raise ValueError(f"Redirected outside official domains: {target}")
            current_url = target
            continue
        if len(body) > max_bytes:
            raise ValueError(f"Response exceeded {max_bytes} bytes")
        return FetchResult(
            requested_url=url,
            final_url=effective_url or current_url,
            status_code=status_code,
            text=body.decode("utf-8", errors="replace"),
            content_type=content_type.split(";", 1)[0] if content_type else "",
            content_hash=hashlib.sha256(body).hexdigest(),
        )
    raise RuntimeError(f"Too many redirects while fetching {url}")
