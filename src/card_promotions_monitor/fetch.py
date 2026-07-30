from __future__ import annotations

import hashlib
import http.cookiejar
import http.client
import json
import os
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from dataclasses import dataclass


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
USER_AGENT = f"{BROWSER_USER_AGENT} TaiwanCardPromotionMonitor/0.1"


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


class FetchSession:
    """Cookie-preserving fetcher for official sites with stateful pagination."""

    def __init__(self, allowed_domains: list[str]) -> None:
        self.domains = [item.lower().rstrip(".") for item in allowed_domains]
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            _SafeRedirectHandler(self.domains),
            urllib.request.HTTPCookieProcessor(self.cookies),
        )

    def fetch_text(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 25.0,
        attempts: int = 2,
        max_bytes: int = 8_000_000,
    ) -> FetchResult:
        if not is_allowed_url(url, self.domains):
            raise ValueError(f"URL is outside official domains: {url}")
        encoded_data = (
            urllib.parse.urlencode(data).encode("utf-8")
            if data is not None
            else None
        )
        request_headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            "Cache-Control": "no-cache",
        }
        if encoded_data is not None:
            request_headers["Content-Type"] = (
                "application/x-www-form-urlencoded; charset=UTF-8"
            )
        request_headers.update(headers or {})
        request = urllib.request.Request(
            url,
            data=encoded_data,
            headers=request_headers,
        )
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError(f"Response exceeded {max_bytes} bytes")
                    final_url = response.geturl()
                    if not is_allowed_url(final_url, self.domains):
                        raise ValueError(
                            f"Final URL is outside official domains: {final_url}"
                        )
                    charset = response.headers.get_content_charset() or "utf-8"
                    return FetchResult(
                        requested_url=url,
                        final_url=final_url,
                        status_code=response.status,
                        text=body.decode(charset, errors="replace"),
                        content_type=response.headers.get_content_type(),
                        content_hash=hashlib.sha256(body).hexdigest(),
                    )
            except (
                urllib.error.URLError,
                urllib.error.HTTPError,
                TimeoutError,
                ValueError,
            ) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error

    def fetch_json(self, url: str, **kwargs):
        result = self.fetch_text(url, **kwargs)
        try:
            return result, json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Official endpoint did not return valid JSON: {url}"
            ) from exc


class PersistentHTTPSession:
    """Cookie-preserving HTTP/1.1 session that reuses each official host connection."""

    def __init__(
        self,
        allowed_domains: list[str],
        *,
        user_agent: str = BROWSER_USER_AGENT,
    ) -> None:
        self.domains = [item.lower().rstrip(".") for item in allowed_domains]
        self.user_agent = user_agent
        self.cookies: dict[str, str] = {}
        self.connections: dict[str, http.client.HTTPSConnection] = {}

    def close(self) -> None:
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def _connection(
        self,
        hostname: str,
        port: int,
        timeout: float,
        *,
        refresh: bool = False,
    ) -> http.client.HTTPSConnection:
        key = f"{hostname}:{port}"
        if refresh and key in self.connections:
            self.connections.pop(key).close()
        if key not in self.connections:
            self.connections[key] = http.client.HTTPSConnection(
                hostname,
                port,
                context=ssl.create_default_context(),
                timeout=timeout,
            )
        return self.connections[key]

    def fetch_text(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 25.0,
        attempts: int = 2,
        max_bytes: int = 8_000_000,
    ) -> FetchResult:
        current_url = url
        method = "POST" if data is not None else "GET"
        body = (
            urllib.parse.urlencode(data).encode("utf-8")
            if data is not None
            else None
        )
        for _ in range(11):
            if not is_allowed_url(current_url, self.domains):
                raise ValueError(f"URL is outside official domains: {current_url}")
            parsed = urllib.parse.urlsplit(current_url)
            port = parsed.port or 443
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            request_headers = {
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
            }
            if self.cookies:
                request_headers["Cookie"] = "; ".join(
                    f"{key}={value}" for key, value in self.cookies.items()
                )
            if body is not None:
                request_headers["Content-Type"] = (
                    "application/x-www-form-urlencoded; charset=UTF-8"
                )
            request_headers.update(headers or {})
            response = None
            last_error: Exception | None = None
            for attempt in range(attempts):
                try:
                    connection = self._connection(
                        parsed.hostname or "",
                        port,
                        timeout,
                        refresh=attempt > 0,
                    )
                    connection.request(
                        method,
                        path,
                        body=body,
                        headers=request_headers,
                    )
                    response = connection.getresponse()
                    break
                except (
                    http.client.HTTPException,
                    OSError,
                    TimeoutError,
                ) as exc:
                    last_error = exc
            if response is None:
                raise RuntimeError(
                    f"Persistent HTTPS fetch failed for {current_url}: {last_error}"
                ) from last_error
            response_body = response.read(max_bytes + 1)
            if len(response_body) > max_bytes:
                raise ValueError(f"Response exceeded {max_bytes} bytes")
            for raw_cookie in response.headers.get_all("Set-Cookie", []):
                parsed_cookie = SimpleCookie()
                parsed_cookie.load(raw_cookie)
                for key, morsel in parsed_cookie.items():
                    self.cookies[key] = morsel.value
            if 300 <= response.status < 400 and response.headers.get("Location"):
                target = urllib.parse.urljoin(
                    current_url,
                    response.headers["Location"],
                )
                if not is_allowed_url(target, self.domains):
                    raise ValueError(f"Redirected outside official domains: {target}")
                current_url = target
                if response.status in {301, 302, 303}:
                    method = "GET"
                    body = None
                continue
            if response.status >= 400:
                raise RuntimeError(
                    f"Persistent HTTPS fetch failed for {current_url}: "
                    f"HTTP {response.status}"
                )
            charset = response.headers.get_content_charset() or "utf-8"
            return FetchResult(
                requested_url=url,
                final_url=current_url,
                status_code=response.status,
                text=response_body.decode(charset, errors="replace"),
                content_type=response.headers.get_content_type(),
                content_hash=hashlib.sha256(response_body).hexdigest(),
            )
        raise RuntimeError(f"Too many redirects while fetching {url}")


class SystemCurlSession:
    """Cookie-preserving curl session for sites that reject urllib TLS."""

    def __init__(
        self,
        allowed_domains: list[str],
        *,
        user_agent: str = BROWSER_USER_AGENT,
    ) -> None:
        self.domains = [item.lower().rstrip(".") for item in allowed_domains]
        self.user_agent = user_agent
        descriptor, self.cookie_path = tempfile.mkstemp(
            prefix="card-promotions-",
            suffix=".cookies",
        )
        os.close(descriptor)

    def close(self) -> None:
        try:
            os.unlink(self.cookie_path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def fetch_text(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 25.0,
        max_bytes: int = 8_000_000,
    ) -> FetchResult:
        marker = b"\n__CARD_PROMOTIONS_SESSION_META__"
        current_url = url
        for _ in range(11):
            if not is_allowed_url(current_url, self.domains):
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
                "--cookie",
                self.cookie_path,
                "--cookie-jar",
                self.cookie_path,
                "--user-agent",
                self.user_agent,
                "--header",
                "Accept-Language: zh-TW,zh;q=0.9,en;q=0.5",
            ]
            for key, value in (headers or {}).items():
                command.extend(["--header", f"{key}: {value}"])
            if data is not None:
                command.extend([
                    "--header",
                    "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                    "--data",
                    urllib.parse.urlencode(data),
                ])
            command.extend([
                "--write-out",
                marker.decode()
                + "%{http_code}\t%{url_effective}\t%{content_type}\t%{redirect_url}",
                current_url,
            ])
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout + 5,
            )
            if completed.returncode != 0:
                error = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"System TLS session fetch failed for {current_url}: {error}"
                )
            try:
                body, metadata = completed.stdout.rsplit(marker, 1)
                status_text, effective_url, content_type, redirect_url = metadata.decode(
                    "utf-8", errors="replace"
                ).split("\t", 3)
                status_code = int(status_text)
            except (ValueError, TypeError) as exc:
                raise RuntimeError(
                    f"Could not parse system TLS session response for {current_url}"
                ) from exc
            if 300 <= status_code < 400 and redirect_url:
                target = urllib.parse.urljoin(
                    effective_url or current_url,
                    redirect_url,
                )
                if not is_allowed_url(target, self.domains):
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

    def fetch_json(self, url: str, **kwargs):
        result = self.fetch_text(url, **kwargs)
        try:
            return result, json.loads(result.text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Official endpoint did not return valid JSON: {url}"
            ) from exc


def fetch_text(
    url: str,
    allowed_domains: list[str],
    *,
    data: dict[str, str] | None = None,
    timeout: float = 25.0,
    attempts: int = 2,
    max_bytes: int = 8_000_000,
) -> FetchResult:
    domains = [item.lower().rstrip(".") for item in allowed_domains]
    if not is_allowed_url(url, domains):
        raise ValueError(f"URL is outside official domains: {url}")
    encoded_data = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.7",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
        "Cache-Control": "no-cache",
    }
    if encoded_data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    request = urllib.request.Request(
        url,
        data=encoded_data,
        headers=headers,
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
            data=data,
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
    data: dict[str, str] | None,
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
        ]
        if data is not None:
            command.extend([
                "--header",
                "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
                "--data",
                urllib.parse.urlencode(data),
            ])
        command.append(current_url)
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
