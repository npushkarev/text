#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Additive delta sync for non-Docker ProGet feeds.

The source side is compatible with ProGet 2022.28.  Package inventories are
read through the public Native API routines that existed in that release (or
through the feed protocol for Assets, PyPI, RPM and Helm).  Files are uploaded
to the current ProGet Packages API.  Maven, Bower and Asset feeds use their
native protocol endpoints because the Packages API does not support them.

The operation is deliberately additive: destination-only content is never
deleted.  Every source item is checked on the destination before it is read;
therefore an interrupted run can simply be started again.
"""

from __future__ import annotations

import argparse
import base64
import bz2
from collections import Counter
import fnmatch
import gzip
import hashlib
import html.parser
import http.client
import json
import logging
import os
import re
import ssl
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import (
    parse_qsl,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)


LOG = logging.getLogger("proget-feed-delta")
USER_AGENT = "proget-feed-delta-sync/1.0"
READ_SUCCESS_STATUS = 200
WRITE_SUCCESS_STATUSES = frozenset({200, 201, 202, 204})
RETRY_STATUSES = frozenset({408, 423, 429, 500, 502, 503, 504})


class SyncError(Exception):
    """A user-facing migration error."""


class TransportError(SyncError):
    """A network or truncated-body error."""


class HttpError(SyncError):
    def __init__(self, method: str, url: str, status: int, body: str = "") -> None:
        detail = compact_text(body)
        suffix = "" if not detail else ": " + detail
        super().__init__(f"{method} {redact_url(url)} -> HTTP {status}{suffix}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class Headers:
    def __init__(self, items: Iterable[Tuple[str, str]]) -> None:
        self._items = list(items)
        self._map = {name.lower(): value for name, value in self._items}

    def get(self, name: str, default: Any = None) -> Any:
        return self._map.get(name.lower(), default)

    def items(self) -> List[Tuple[str, str]]:
        return list(self._items)


class Response:
    def __init__(self, method: str, url: str, raw: "http.client.HTTPResponse",
                 session: "Session", key: Tuple[str, str, int]) -> None:
        self.method = method
        self.url = url
        self.status_code = raw.status
        self.reason = raw.reason
        self.headers = Headers(raw.getheaders())
        self.raw = raw
        self._session = session
        self._key = key
        self._content: Optional[bytes] = None
        self._released = False

    @property
    def content(self) -> bytes:
        if self._content is None:
            try:
                self._content = b"" if self.method == "HEAD" else (self.raw.read() or b"")
            except Exception as exc:
                self._session.drop(self._key)
                self._released = True
                raise TransportError(f"response body was interrupted for {redact_url(self.url)}: {exc}") from exc
            self._release(True)
        return self._content

    @property
    def text(self) -> str:
        content_type = self.headers.get("Content-Type", "") or ""
        match = re.search(r"charset=([^;\s]+)", content_type, flags=re.I)
        encoding = match.group(1).strip('"\'') if match else "utf-8"
        try:
            return self.content.decode(encoding, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        if not self.content:
            raise ValueError("empty JSON response")
        return json.loads(self.content.decode("utf-8-sig", errors="replace"))

    def _release(self, reusable: bool) -> None:
        if self._released:
            return
        self._released = True
        if reusable:
            self._session.release(self._key)
        else:
            self._session.drop(self._key)

    def close(self) -> None:
        if self._released:
            return
        if self.method == "HEAD" or self._content is not None:
            self._release(True)
            return
        try:
            self.raw.close()
        finally:
            self._release(False)


class Session:
    """Small stdlib HTTP client with per-thread keep-alive connections."""

    def __init__(self, connect_timeout: float = 30.0, read_timeout: float = 900.0,
                 verify: Any = True, blocksize: int = 1024 * 1024) -> None:
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.verify = verify
        self.blocksize = blocksize
        self._local = threading.local()
        self._ssl_contexts: Dict[Any, ssl.SSLContext] = {}
        self._ssl_guard = threading.Lock()

    def _pool(self) -> Dict[Tuple[str, str, int], Any]:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
        return pool

    def _ssl_context(self, verify: Any) -> ssl.SSLContext:
        key = verify if isinstance(verify, (str, bool)) else True
        with self._ssl_guard:
            context = self._ssl_contexts.get(key)
            if context is None:
                if verify is False:
                    context = ssl.create_default_context()
                    context.check_hostname = False
                    context.verify_mode = ssl.CERT_NONE
                elif isinstance(verify, str):
                    context = ssl.create_default_context(cafile=verify)
                else:
                    context = ssl.create_default_context()
                self._ssl_contexts[key] = context
            return context

    def _connection(self, key: Tuple[str, str, int], verify: Any) -> Any:
        pool = self._pool()
        connection = pool.get(key)
        if connection is not None:
            return connection
        scheme, host, port = key
        if scheme == "https":
            connection = http.client.HTTPSConnection(
                host, port, timeout=self.connect_timeout,
                context=self._ssl_context(verify), blocksize=self.blocksize,
            )
        else:
            connection = http.client.HTTPConnection(
                host, port, timeout=self.connect_timeout, blocksize=self.blocksize,
            )
        pool[key] = connection
        return connection

    def release(self, key: Tuple[str, str, int]) -> None:
        del key  # the connection intentionally stays in the pool

    def drop(self, key: Tuple[str, str, int]) -> None:
        connection = self._pool().pop(key, None)
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    @staticmethod
    def _split(url: str) -> Tuple[Tuple[str, str, int], str]:
        parts = urlparse(url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https") or not parts.hostname:
            raise TransportError(f"invalid HTTP URL: {redact_url(url)}")
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return (scheme, parts.hostname, port), path

    @staticmethod
    def _merge_params(url: str, params: Optional[Mapping[str, Any]]) -> str:
        if not params:
            return url
        pairs = parse_qsl(urlparse(url).query, keep_blank_values=True)
        for name, value in params.items():
            if value is None:
                continue
            pairs.append((str(name), str(value)))
        parts = urlparse(url)
        return urlunparse(parts._replace(query=urlencode(pairs, quote_via=quote)))

    def request(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                data: Any = None, params: Optional[Mapping[str, Any]] = None,
                stream: bool = False, allow_redirects: bool = True,
                verify: Any = None, _redirects: int = 0) -> Response:
        method = method.upper()
        url = self._merge_params(url, params)
        verify = self.verify if verify is None else verify
        key, path = self._split(url)
        send_headers = {"Accept-Encoding": "identity", "User-Agent": USER_AGENT}
        send_headers.update(headers or {})
        body = data
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is None and method in ("POST", "PUT", "PATCH"):
            body = b""
        if isinstance(body, (bytes, bytearray)):
            send_headers.setdefault("Content-Length", str(len(body)))

        reused_retry = True
        while True:
            connection = self._connection(key, verify)
            fresh = connection.sock is None
            try:
                if fresh:
                    connection.connect()
                if connection.sock is not None:
                    connection.sock.settimeout(self.read_timeout)
                connection.request(method, path, body=body, headers=send_headers)
                raw = connection.getresponse()
                break
            except Exception as exc:
                self.drop(key)
                repeatable = isinstance(body, (bytes, bytearray, type(None)))
                if reused_retry and not fresh and repeatable:
                    reused_retry = False
                    continue
                raise TransportError(f"{method} {redact_url(url)}: {exc}") from exc

        response = Response(method, url, raw, self, key)
        if (allow_redirects and response.status_code in (301, 302, 303, 307, 308)
                and response.headers.get("Location")):
            if _redirects >= 5:
                response.close()
                raise TransportError(f"too many redirects for {redact_url(url)}")
            target = urljoin(url, response.headers.get("Location"))
            status = response.status_code
            response.close()
            new_headers = dict(headers or {})
            try:
                cross_origin = redirect_requires_auth_strip(url, target)
            except (ValueError, TransportError):
                raise
            if cross_origin:
                for name in list(new_headers):
                    if name.lower() in ("authorization", "x-apikey", "x-api-key"):
                        new_headers.pop(name, None)
            next_method = "GET" if status == 303 else method
            next_body = None if next_method == "GET" else body
            if next_body is not None and not isinstance(next_body, (bytes, bytearray)):
                raise TransportError("cannot replay a streamed request body after redirect")
            return self.request(
                next_method, target, headers=new_headers, data=next_body,
                stream=stream, allow_redirects=True, verify=verify,
                _redirects=_redirects + 1,
            )
        if not stream:
            response.content
        return response

    def close(self) -> None:
        for key in list(self._pool()):
            self.drop(key)


def compact_text(value: Any, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[:limit] + "..."


def redact_url(url: str) -> str:
    parts = urlparse(url)
    netloc = parts.netloc
    if "@" in netloc:
        netloc = "***@" + netloc.rsplit("@", 1)[1]
    pairs = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        sensitive = lowered == "sig" or any(
            marker in lowered for marker in (
                "key", "token", "auth", "secret", "password",
                "credential", "signature", "ticket",
            )
        )
        pairs.append((key, "***" if sensitive else value))
    return urlunparse(parts._replace(netloc=netloc, query=urlencode(pairs)))


def normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    parts = urlparse(value)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"invalid ProGet URL: {value!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("credentials must not be embedded in the ProGet URL")
    if parts.query or parts.fragment:
        raise ValueError("the ProGet base URL must not contain a query or fragment")
    host = (parts.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"invalid port in ProGet URL: {value!r}") from exc
    default_port = 443 if parts.scheme.lower() == "https" else 80
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return urlunparse((parts.scheme.lower(), netloc, path, "", "", ""))


def http_origin(value: str) -> Tuple[str, str, int]:
    parts = urlparse(value)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"invalid HTTP URL: {redact_url(value)}")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"credentials must not be embedded in URL: {redact_url(value)}")
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError(f"invalid port in HTTP URL: {redact_url(value)}") from exc
    return scheme, parts.hostname.lower(), port


def redirect_requires_auth_strip(source: str, target: str) -> bool:
    source_origin = http_origin(source)
    target_origin = http_origin(target)
    if source_origin[0] == "https" and target_origin[0] == "http":
        raise TransportError(
            f"refusing HTTPS-to-HTTP redirect: {redact_url(source)} -> {redact_url(target)}"
        )
    return source_origin != target_origin


def path_quote(value: Any) -> str:
    return quote(str(value), safe="")


def row_get(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    for name in names:
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in normalized:
            return normalized[key]
    return default


_FEED_TYPE_ALIASES = {
    "nuget": "nuget",
    "chocolatey": "nuget",
    "powershell": "nuget",
    "deployment": "nuget",
    "npm": "npm",
    "bower": "bower",
    "maven": "maven",
    "proget": "universal",
    "universal": "universal",
    "romp": "universal",
    "rubygems": "rubygems",
    "ruby": "rubygems",
    "vsix": "vsix",
    "asset": "asset",
    "assets": "asset",
    "assetdirectory": "asset",
    "debian": "debian",
    "pypi": "pypi",
    "python": "pypi",
    "helm": "helm",
    "rpm": "rpm",
    "conda": "conda",
    "docker": "docker",
}


def normalize_feed_type(value: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", (value or "").lower())
    return _FEED_TYPE_ALIASES.get(key, key)


def decode_hash(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.hex()
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"[0-9a-fA-F]+", text) and len(text) % 2 == 0:
        return text.lower()
    try:
        return base64.b64decode(text, validate=True).hex()
    except Exception:
        return None


def best_hash(row: Mapping[str, Any], candidates: Sequence[Tuple[str, Sequence[str]]]) -> Tuple[Optional[str], Optional[str]]:
    for algorithm, names in candidates:
        value = decode_hash(row_get(row, *names))
        if value:
            return algorithm, value
    return None, None


def truthy_indicator(value: Any) -> bool:
    return str(value or "").strip().lower() in ("y", "yes", "true", "1")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Feed:
    name: str
    feed_type: str
    feed_id: Optional[int] = None
    raw_type: str = ""


@dataclass
class Artifact:
    feed_type: str
    identity: Tuple[str, ...]
    display: str
    source_url: Optional[str]
    destination_url: Optional[str]
    filename: Optional[str] = None
    checksum_algorithm: Optional[str] = None
    checksum: Optional[str] = None
    size: Optional[int] = None
    upload_kind: str = "package"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def canonical_key(self) -> str:
        encoded = [
            normalize_feed_type(self.feed_type),
            str(self.metadata.get("sourceFeed", "")),
            str(self.metadata.get("destinationFeed", "")),
        ] + [str(v or "") for v in self.identity]
        return "\x1f".join(encoded)


def classify_probe(exists: bool, source_checksum: Optional[str],
                   destination_checksum: Optional[str]) -> str:
    if not exists:
        return "missing"
    if source_checksum and destination_checksum:
        return "matched" if source_checksum.lower() == destination_checksum.lower() else "conflict"
    return "matched"


class JsonlState:
    """Append-only audit/cache. Destination probing remains the source of truth."""

    def __init__(self, path: Optional[str], source: str, destination: str) -> None:
        self.path = path
        self.source = normalize_base_url(source)
        self.destination = normalize_base_url(destination)
        self._keys: set = set()
        self._lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    if (item.get("source") == self.source
                            and item.get("destination") == self.destination
                            and item.get("status") == "uploaded"):
                        self._keys.add(item.get("key"))

    def contains(self, artifact: Artifact) -> bool:
        return artifact.canonical_key() in self._keys

    def record(self, artifact: Artifact, status: str, **details: Any) -> None:
        if not self.path:
            return
        item = {
            "time": utc_now(),
            "source": self.source,
            "destination": self.destination,
            "key": artifact.canonical_key(),
            "display": artifact.display,
            "status": status,
        }
        item.update(details)
        line = json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock:
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            if status == "uploaded":
                self._keys.add(artifact.canonical_key())


class ProGetClient:
    def __init__(self, base_url: str, api_key: str, verify: Any = True,
                 timeout: float = 900.0, retries: int = 3) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.verify = verify
        self.retries = max(0, retries)
        self.session = Session(read_timeout=timeout, verify=verify)

    @property
    def headers(self) -> Dict[str, str]:
        if not self.api_key:
            return {}
        basic = base64.b64encode(("api:" + self.api_key).encode("utf-8")).decode("ascii")
        return {"X-ApiKey": self.api_key, "Authorization": "Basic " + basic}

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return self.base_url + "/" + path.lstrip("/")

    def request(self, method: str, path: str, *, headers: Optional[Dict[str, str]] = None,
                data: Any = None, params: Optional[Mapping[str, Any]] = None,
                stream: bool = False, retry: bool = True) -> Response:
        url = self.url(path)
        # Package indexes may legitimately point at a CDN or connector origin.
        # Never attach the ProGet key to an initial cross-origin absolute URL;
        # Session.request applies the same rule to subsequent redirects.
        same_origin = http_origin(url) == http_origin(self.base_url)
        merged = self.headers if same_origin else {}
        supplied = dict(headers or {})
        if not same_origin:
            for name in list(supplied):
                if name.lower() in ("authorization", "x-apikey", "x-api-key"):
                    supplied.pop(name, None)
        merged.update(supplied)
        attempts = self.retries + 1 if retry and method.upper() in ("GET", "HEAD") else 1
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method, url, headers=merged, data=data, params=params,
                    stream=stream, verify=self.verify,
                )
            except TransportError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
            else:
                if response.status_code not in RETRY_STATUSES or attempt + 1 >= attempts:
                    return response
                response.close()
            time.sleep(min(8.0, 0.5 * (2 ** attempt)))
        assert last_error is not None
        raise last_error

    def get_json(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        response = self.request("GET", path, params=params)
        if response.status_code != READ_SUCCESS_STATUS:
            raise HttpError("GET", response.url, response.status_code, response.text)
        try:
            return response.json()
        except ValueError as exc:
            raise SyncError(f"invalid JSON returned by {redact_url(response.url)}: {exc}") from exc

    def native(self, method: str, **params: Any) -> List[Dict[str, Any]]:
        body = json.dumps(params, ensure_ascii=False).encode("utf-8")
        response = self.request(
            "POST", "/api/json/" + path_quote(method),
            headers={"Content-Type": "application/json"}, data=body, retry=False,
        )
        if response.status_code != READ_SUCCESS_STATUS:
            hint = ""
            if response.status_code in (401, 403, 404):
                hint = " (the source key needs Native API Access and feed view permission)"
            raise SyncError(
                f"Native API {method} failed with HTTP {response.status_code}{hint}: "
                f"{compact_text(response.text)}"
            )
        try:
            value = response.json()
        except ValueError as exc:
            raise SyncError(f"Native API {method} returned invalid JSON") from exc
        return unwrap_rows(value, method)

    def list_feeds_native(self) -> List[Feed]:
        rows = self.native("Feeds_GetFeeds", IncludeInactive_Indicator="N")
        result = []
        for row in rows:
            name = str(row_get(row, "Feed_Name", "name", default="") or "")
            raw_type = str(row_get(row, "FeedType_Name", "feedType", default="") or "")
            raw_id = row_get(row, "Feed_Id", "id")
            if not name:
                continue
            try:
                feed_id = int(raw_id)
            except (TypeError, ValueError):
                feed_id = None
            result.append(Feed(name, normalize_feed_type(raw_type), feed_id, raw_type))
        return result

    def list_feeds_management(self) -> List[Feed]:
        value = self.get_json("/api/management/feeds/list")
        rows = unwrap_rows(value, "feeds/list")
        result = []
        for row in rows:
            name = str(row_get(row, "name", "Feed_Name", default="") or "")
            raw_type = str(row_get(row, "feedType", "FeedType_Name", default="") or "")
            if name:
                result.append(Feed(name, normalize_feed_type(raw_type), None, raw_type))
        return result


def unwrap_rows(value: Any, operation: str = "API") -> List[Dict[str, Any]]:
    if value is None:
        raise SyncError(f"unexpected null response from {operation}")
    if isinstance(value, list):
        if all(isinstance(item, dict) for item in value):
            return list(value)
    if isinstance(value, dict):
        lists = [item for item in value.values() if isinstance(item, list)]
        if len(lists) == 1 and all(isinstance(item, dict) for item in lists[0]):
            return list(lists[0])
        if value and all(not isinstance(item, (list, dict)) for item in value.values()):
            return [dict(value)]
    raise SyncError(f"unexpected response shape from {operation}")


@dataclass
class InventoryOptions:
    include_cached: bool = False
    include_virtual: bool = False
    inventory_workers: int = 4
    debian_distribution: Optional[str] = None
    source_only: bool = False


@dataclass
class InventoryContext:
    source: ProGetClient
    destination: ProGetClient
    source_feed: Feed
    destination_feed: Feed
    options: InventoryOptions
    filtered_cached: int = 0
    filtered_virtual: int = 0
    _source_has_connectors: Optional[bool] = field(default=None, init=False, repr=False)
    _destination_has_connectors: Optional[bool] = field(default=None, init=False, repr=False)

    def local_row(self, row: Mapping[str, Any]) -> bool:
        if self.options.include_cached:
            return True
        if truthy_indicator(row_get(row, "Cached_Indicator", "cached")):
            self.filtered_cached += 1
            return False
        return True

    @staticmethod
    def _feed_has_connectors(client: ProGetClient, feed: Feed) -> bool:
        arguments: Dict[str, Any]
        if feed.feed_id is not None:
            arguments = {"Feed_Id": feed.feed_id}
        else:
            arguments = {"Feed_Name": feed.name}
        return bool(client.native("Feeds_GetFeedConnectors", **arguments))

    def source_has_connectors(self) -> bool:
        if self._source_has_connectors is None:
            self._source_has_connectors = self._feed_has_connectors(
                self.source, self.source_feed,
            )
        return self._source_has_connectors

    def destination_has_connectors(self) -> bool:
        if self._destination_has_connectors is None:
            self._destination_has_connectors = self._feed_has_connectors(
                self.destination, self.destination_feed,
            )
        return self._destination_has_connectors


def require_feed_id(feed: Feed) -> int:
    if feed.feed_id is None:
        raise SyncError(f"Native API did not return Feed_Id for source feed {feed.name!r}")
    return feed.feed_id


def optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def malformed_inventory_row(ctx: InventoryContext, kind: str,
                            missing: Sequence[str]) -> None:
    fields = ", ".join(missing)
    raise SyncError(
        f"{ctx.source_feed.name!r}: incomplete {kind} inventory row "
        f"(missing: {fields}); refusing to silently skip source content"
    )


def require_protocol_inventory_opt_in(ctx: InventoryContext, feed_type: str) -> None:
    """Avoid silently mirroring connector-visible protocol indexes."""
    if not ctx.options.include_cached and ctx.source_has_connectors():
        raise SyncError(
            f"{feed_type} feed {ctx.source_feed.name!r} has connectors, while its "
            "ProGet 2022 protocol index does not mark local versus connector content. "
            "Use --include-cached to explicitly include everything visible in the "
            "index, or temporarily remove the source connectors."
        )


def require_connector_free_destination(ctx: InventoryContext, feed_type: str) -> None:
    """Maven and Bower have no local-only Common Packages API probe."""
    if ctx.options.source_only:
        return
    try:
        has_connectors = ctx.destination_has_connectors()
    except Exception as exc:
        raise SyncError(
            f"cannot safely check {feed_type} destination {ctx.destination_feed.name!r}: "
            "Maven/Bower protocol reads may resolve connector-only content. Grant the "
            "destination key Native API Access so connectors can be checked"
        ) from exc
    if has_connectors:
        raise SyncError(
            f"{feed_type} destination {ctx.destination_feed.name!r} has connectors; "
            "temporarily remove them before delta sync so remote content is not "
            "mistaken for a local package"
        )


def package_url(client: ProGetClient, route: str, feed: str, *segments: Any,
                query: Optional[Mapping[str, Any]] = None) -> str:
    path = "/" + route.strip("/") + "/" + path_quote(feed)
    if segments:
        path += "/" + "/".join(path_quote(value) for value in segments)
    url = client.url(path)
    return Session._merge_params(url, query)


def package_qualifier(**values: Any) -> Optional[str]:
    parts = []
    for key in sorted(values):
        value = values[key]
        if value is not None and str(value) != "":
            parts.append(f"{key}={quote(str(value), safe='')}")
    return "&".join(parts) or None


def common_download_url(client: ProGetClient, feed: str, name: str, version: str,
                        *, group: Optional[str] = None,
                        qualifier: Optional[str] = None) -> str:
    return Session._merge_params(
        client.url(f"/api/packages/{path_quote(feed)}/download"),
        {
            "group": group or None,
            "name": name,
            "version": version,
            "qualifier": qualifier or None,
        },
    )


def make_artifact(feed_type: str, identity: Sequence[Any], display: str,
                  source_url: Optional[str], destination_url: Optional[str],
                  *, filename: Optional[str] = None,
                  checksum: Tuple[Optional[str], Optional[str]] = (None, None),
                  size: Any = None, upload_kind: str = "package",
                  metadata: Optional[Dict[str, Any]] = None) -> Artifact:
    return Artifact(
        feed_type=feed_type,
        identity=tuple(str(value or "") for value in identity),
        display=display,
        source_url=source_url,
        destination_url=destination_url,
        filename=filename,
        checksum_algorithm=checksum[0],
        checksum=checksum[1],
        size=optional_int(size),
        upload_kind=upload_kind,
        metadata=dict(metadata or {}),
    )


def inventory_nuget(ctx: InventoryContext) -> List[Artifact]:
    rows = ctx.source.native(
        "NuGetPackagesV2_GetPackages",
        Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        if not ctx.local_row(row):
            continue
        name = str(row_get(row, "Package_Id", "name", default="") or "")
        version = str(row_get(row, "Version_Text", "version", default="") or "")
        if not name or not version:
            malformed_inventory_row(
                ctx, "NuGet package",
                [field for field, value in (("name", name), ("version", version)) if not value],
            )
        checksum = best_hash(row, (
            ("sha512", ("PackageHash_SHA512_Bytes", "sha512")),
            ("sha1", ("PackageHash_SHA1_Bytes", "sha1")),
        ))
        filename = f"{name}.{version}.nupkg"
        artifacts.append(make_artifact(
            "nuget", (name.lower(), version.lower()), f"{name} {version}",
            package_url(ctx.source, "nuget", ctx.source_feed.name, "package", name, version),
            common_download_url(
                ctx.destination, ctx.destination_feed.name, name, version,
            ),
            filename=filename, checksum=checksum, size=row_get(row, "Package_Size", "size"),
            metadata={
                "listed": row_get(row, "Listed_Indicator"),
                "containsSymbols": truthy_indicator(row_get(row, "Symbols_Indicator")),
            },
        ))
    return artifacts


def npm_full_name(scope: str, name: str) -> str:
    if not scope:
        return name
    scope = scope if scope.startswith("@") else "@" + scope
    return scope + "/" + name


def npm_tarball_name(name: str, version: str) -> str:
    return f"{name}-{version}.tgz"


def npm_download_url(client: ProGetClient, feed: str, scope: str,
                     name: str, version: str) -> str:
    full = npm_full_name(scope, name)
    # Keep @ and the slash between scope/name as path syntax; encode each component.
    package_path = "/".join(path_quote(part) for part in full.split("/"))
    return client.url(
        f"/npm/{path_quote(feed)}/{package_path}/-/{path_quote(npm_tarball_name(name, version))}"
    )


def inventory_npm(ctx: InventoryContext) -> List[Artifact]:
    rows = ctx.source.native(
        "NpmFeeds_GetAllPackageVersions",
        Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        if not ctx.local_row(row):
            continue
        name = str(row_get(row, "Package_Name", "name", default="") or "")
        scope = str(row_get(row, "Scope_Name", "scope", default="") or "")
        version = str(row_get(row, "Version_Text", "version", default="") or "")
        if not name or not version:
            malformed_inventory_row(
                ctx, "npm package",
                [field for field, value in (("name", name), ("version", version)) if not value],
            )
        checksum = best_hash(row, (
            ("sha512", ("SHA512Hash_Bytes", "sha512")),
            ("sha1", ("PackageHash_Bytes", "sha1")),
        ))
        full = npm_full_name(scope, name)
        common_group = full.rsplit("/", 1)[0] if "/" in full else None
        artifacts.append(make_artifact(
            "npm", (full.lower(), version), f"{full} {version}",
            npm_download_url(ctx.source, ctx.source_feed.name, scope, name, version),
            common_download_url(
                ctx.destination, ctx.destination_feed.name, name, version,
                group=common_group,
            ),
            filename=npm_tarball_name(name, version), checksum=checksum,
            size=row_get(row, "Package_Size", "size"),
            metadata={"group": common_group, "name": name, "version": version},
        ))
    return artifacts


def universal_download_url(client: ProGetClient, feed: str, group: str,
                           name: str, version: str) -> str:
    segments = ["download"]
    if group:
        segments.extend(part for part in group.strip("/").split("/") if part)
    segments.extend((name, version))
    return package_url(client, "upack", feed, *segments)


def inventory_universal(ctx: InventoryContext) -> List[Artifact]:
    feed_id = require_feed_id(ctx.source_feed)
    packages = ctx.source.native(
        "ProGetPackages_GetPackages", Feed_Id=feed_id,
        IncludeUnlisted_Indicator="Y",
    )
    artifacts = []
    for package in packages:
        group_value = row_get(package, "Group_Name", "group", default="")
        group = str(group_value or "")
        name = str(row_get(package, "Package_Name", "name", default="") or "")
        if not name:
            malformed_inventory_row(ctx, "Universal package", ["name"])
        versions = ctx.source.native(
            "ProGetPackages_GetPackageVersions", Feed_Id=feed_id,
            Group_Name=group_value, Package_Name=name,
            IncludeUnlisted_Indicator="Y",
        )
        for row in versions:
            if not ctx.local_row(row):
                continue
            if truthy_indicator(row_get(row, "Virtual_Indicator", "virtual")):
                if not ctx.options.include_virtual:
                    ctx.filtered_virtual += 1
                    continue
            version = str(row_get(row, "Version_Text", "version", default="") or "")
            if not version:
                malformed_inventory_row(ctx, "Universal package version", ["version"])
            checksum = best_hash(row, (("sha1", ("PackageHash_Bytes", "sha1")),))
            display_name = "/".join(part for part in (group, name) if part)
            artifacts.append(make_artifact(
                "universal", (group.lower(), name.lower(), version),
                f"{display_name} {version}",
                universal_download_url(ctx.source, ctx.source_feed.name, group, name, version),
                common_download_url(
                    ctx.destination, ctx.destination_feed.name, name, version,
                    group=group or None,
                ),
                filename=f"{name}-{version}.upack", checksum=checksum,
                size=row_get(row, "Package_Size", "size"),
                metadata={"group": group, "name": name, "version": version},
            ))
    return artifacts


def maven_file_url(client: ProGetClient, feed: str, group: str, artifact: str,
                   version: str, filename: str) -> str:
    group_path = "/".join(path_quote(part) for part in group.split("."))
    return client.url(
        f"/maven2/{path_quote(feed)}/{group_path}/{path_quote(artifact)}/"
        f"{path_quote(version)}/{path_quote(filename)}"
    )


def inventory_maven(ctx: InventoryContext) -> List[Artifact]:
    require_connector_free_destination(ctx, "Maven")
    feed_id = require_feed_id(ctx.source_feed)
    packages = ctx.source.native("MavenArtifacts_GetArtifacts", Feed_Id=feed_id)
    artifacts = []
    for package in packages:
        group = str(row_get(package, "GroupId_Text", "groupId", default="") or "")
        name = str(row_get(package, "ArtifactId_Text", "artifactId", default="") or "")
        if not group or not name:
            malformed_inventory_row(
                ctx, "Maven artifact",
                [field for field, value in (("group", group), ("name", name)) if not value],
            )
        versions = ctx.source.native(
            "MavenArtifacts_GetArtifactVersions", Feed_Id=feed_id,
            GroupId_Text=group, ArtifactId_Text=name,
        )
        for version_row in versions:
            version = str(row_get(version_row, "Version_Text", "version", default="") or "")
            if not version:
                malformed_inventory_row(ctx, "Maven artifact version", ["version"])
            files = ctx.source.native(
                "MavenArtifacts_GetArtifactFiles", Feed_Id=feed_id,
                GroupId_Text=group, ArtifactId_Text=name, Version_Text=version,
            )
            for row in files:
                if not ctx.local_row(row):
                    continue
                stem = str(row_get(row, "FileName_Text", "fileName", default="") or "")
                extension = str(row_get(row, "File_Type", "type", default="") or "")
                filename = stem + extension
                if not filename:
                    malformed_inventory_row(ctx, "Maven artifact file", ["filename"])
                checksum = best_hash(row, (
                    ("sha512", ("File_SHA512_Bytes", "sha512")),
                    ("sha256", ("File_SHA256_Bytes", "sha256")),
                    ("sha1", ("File_SHA1_Bytes", "sha1")),
                    ("md5", ("File_MD5_Bytes", "md5")),
                ))
                artifacts.append(make_artifact(
                    "maven", (group, name, version, filename),
                    f"{group}:{name}:{version} / {filename}",
                    maven_file_url(ctx.source, ctx.source_feed.name, group, name, version, filename),
                    maven_file_url(ctx.destination, ctx.destination_feed.name, group, name, version, filename),
                    filename=filename, checksum=checksum, size=row_get(row, "File_Size", "size"),
                    upload_kind="maven",
                    metadata={"group": group, "name": name, "version": version},
                ))
    return artifacts


def inventory_bower(ctx: InventoryContext) -> List[Artifact]:
    require_connector_free_destination(ctx, "Bower")
    rows = ctx.source.native(
        "BowerPackages_GetPackages", Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        name = str(row_get(row, "Package_Name", "name", default="") or "")
        repository = str(row_get(row, "Repository_Url", "url", default="") or "")
        if not name or not repository:
            malformed_inventory_row(
                ctx, "Bower package",
                [
                    field for field, value in
                    (("name", name), ("repository URL", repository)) if not value
                ],
            )
        repository_checksum = hashlib.sha256(repository.encode("utf-8")).hexdigest()
        destination_url = package_url(
            ctx.destination, "bower", ctx.destination_feed.name, "packages", name,
        )
        artifacts.append(make_artifact(
            "bower", (name.lower(),), name, None, destination_url,
            checksum=("sha256", repository_checksum), upload_kind="bower",
            metadata={"name": name, "repository": repository},
        ))
    return artifacts


def ruby_filename(name: str, version: str, platform: str) -> str:
    suffix = "" if not platform or platform.lower() == "ruby" else "-" + platform
    return f"{name}-{version}{suffix}.gem"


def inventory_rubygems(ctx: InventoryContext) -> List[Artifact]:
    feed_id = require_feed_id(ctx.source_feed)
    gems = ctx.source.native(
        "RubyGems_SearchGems", Feed_Id=feed_id,
        SearchTerm_Text="", Max_Count=2147483647,
    )
    artifacts = []
    seen_names = set()
    for gem in gems:
        name = str(row_get(gem, "Gem_Name", "name", default="") or "")
        if not name:
            malformed_inventory_row(ctx, "RubyGems package", ["name"])
        if name.lower() in seen_names:
            continue
        seen_names.add(name.lower())
        versions = ctx.source.native(
            "RubyGems_GetGemVersions", Feed_Id=feed_id,
            Gem_Name=name,
        )
        for row in versions:
            if not ctx.local_row(row):
                continue
            version = str(row_get(row, "Version_Text", "version", default="") or "")
            platform = str(row_get(row, "Platform_Text", "platform", default="ruby") or "ruby")
            if not version:
                malformed_inventory_row(ctx, "RubyGems package version", ["version"])
            filename = ruby_filename(name, version, platform)
            checksum = best_hash(row, (("sha1", ("GemHash_Bytes", "sha1")),))
            artifacts.append(make_artifact(
                "rubygems", (name.lower(), version, platform.lower()),
                f"{name} {version} ({platform})",
                package_url(ctx.source, "rubygems", ctx.source_feed.name, "gems", filename),
                common_download_url(
                    ctx.destination, ctx.destination_feed.name, name, version,
                    qualifier=package_qualifier(platform=platform),
                ),
                filename=filename, checksum=checksum, size=row_get(row, "Gem_Size", "size"),
                metadata={"name": name, "version": version, "qualifier": platform},
            ))
    return artifacts


def vsix_version(row: Mapping[str, Any]) -> str:
    values = [
        optional_int(row_get(row, "Major_Number", "major")),
        optional_int(row_get(row, "Minor_Number", "minor")),
        optional_int(row_get(row, "Build_Number", "build")),
        optional_int(row_get(row, "Revision_Number", "revision")),
    ]
    result = [str(values[0] if values[0] is not None else 0),
              str(values[1] if values[1] is not None else 0)]
    if values[2] is not None and values[2] >= 0:
        result.append(str(values[2]))
    if values[3] is not None and values[3] >= 0:
        result.append(str(values[3]))
    return ".".join(result)


def inventory_vsix(ctx: InventoryContext) -> List[Artifact]:
    rows = ctx.source.native(
        "VsixPackages_GetPackages", Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        if not ctx.local_row(row):
            continue
        name = str(row_get(row, "Package_Id", "name", default="") or "")
        if not name:
            malformed_inventory_row(ctx, "VSIX package", ["name"])
        if (optional_int(row_get(row, "Major_Number", "major")) is None
                or optional_int(row_get(row, "Minor_Number", "minor")) is None):
            malformed_inventory_row(ctx, "VSIX package", ["major/minor version"])
        version = vsix_version(row)
        checksum = best_hash(row, (("sha1", ("PackageHash_Bytes", "sha1")),))
        filename = f"{name}-{version}.vsix"
        artifacts.append(make_artifact(
            "vsix", (name.lower(), version), f"{name} {version}",
            package_url(ctx.source, "vsix", ctx.source_feed.name, "downloads", name, version),
            common_download_url(
                ctx.destination, ctx.destination_feed.name, name, version,
            ),
            filename=filename, checksum=checksum, size=row_get(row, "Package_Size", "size"),
            metadata={"name": name, "version": version},
        ))
    return artifacts


def debian_source_url(client: ProGetClient, feed: str, component: str, name: str,
                      version: str, architecture: str) -> str:
    filename = f"{name}_{version}_{architecture}.deb"
    return client.url(
        "/debian-feeds/" + "/".join(path_quote(x) for x in (
            feed, component, name, version, filename,
        ))
    )


def debian_destination_url(ctx: InventoryContext, component: str, name: str,
                           version: str, architecture: str) -> Optional[str]:
    distribution = ctx.options.debian_distribution
    if not distribution:
        if ctx.options.source_only:
            return None
        raise SyncError(
            f"Debian feed {ctx.source_feed.name!r} needs --debian-distribution "
            "for the destination ProGet"
        )
    return common_download_url(
        ctx.destination, ctx.destination_feed.name, name, version,
        # Current pgutil treats all three values as qualifiers. In particular,
        # component is not the Common API group coordinate.
        qualifier=package_qualifier(
            arch=architecture, component=component, distro=distribution,
        ),
    )


def inventory_debian(ctx: InventoryContext) -> List[Artifact]:
    rows = ctx.source.native(
        "DebianPackages_GetPackageVersions", Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        if not ctx.local_row(row):
            continue
        component = str(row_get(row, "Component_Name", "component", default="") or "")
        name = str(row_get(row, "Package_Name", "name", default="") or "")
        version = str(row_get(row, "Version_Text", "version", default="") or "")
        architecture = str(row_get(row, "Architecture_Name", "architecture", default="") or "")
        if not all((component, name, version, architecture)):
            malformed_inventory_row(
                ctx, "Debian package",
                [
                    field for field, value in (
                        ("component", component), ("name", name),
                        ("version", version), ("architecture", architecture),
                    ) if not value
                ],
            )
        filename = f"{name}_{version}_{architecture}.deb"
        checksum = best_hash(row, (
            ("sha512", ("PackageSha512_Bytes", "sha512")),
            ("sha256", ("PackageSha256_Bytes", "sha256")),
        ))
        artifacts.append(make_artifact(
            "debian", (
                ctx.options.debian_distribution or "", component,
                name, version, architecture,
            ),
            f"{component}/{name} {version} ({architecture})",
            debian_source_url(ctx.source, ctx.source_feed.name, component, name, version, architecture),
            debian_destination_url(ctx, component, name, version, architecture),
            filename=filename, checksum=checksum, size=row_get(row, "Package_Size", "size"),
            metadata={
                "component": component, "name": name, "version": version,
                "architecture": architecture,
                "distribution": ctx.options.debian_distribution,
            },
        ))
    return artifacts


def conda_archive_type(code: str) -> Tuple[str, str]:
    """Translate ProGet 2022's one-letter code to current API type + suffix."""
    normalized = code.strip().lower()
    if normalized in ("c", "conda", ".conda"):
        return "conda", ".conda"
    if normalized in ("b", "tar.bz2", ".tar.bz2"):
        return "tar.bz2", ".tar.bz2"
    raise SyncError(f"unknown Conda archive type {code!r}")


def conda_extension(code: str) -> str:
    return conda_archive_type(code)[1]


def inventory_conda(ctx: InventoryContext) -> List[Artifact]:
    rows = ctx.source.native(
        "CondaPackages_GetPackages", Feed_Id=require_feed_id(ctx.source_feed),
    )
    artifacts = []
    for row in rows:
        if not ctx.local_row(row):
            continue
        subdir = str(row_get(row, "Subdir_Name", "subdir", default="") or "")
        name = str(row_get(row, "Package_Name", "name", default="") or "")
        version = str(row_get(row, "Version_Text", "version", default="") or "")
        build = str(row_get(row, "Build_Text", "build", default="") or "")
        archive_type = str(row_get(row, "ArchiveType_Code", "archiveType", default="") or "")
        if not all((subdir, name, version, build)):
            malformed_inventory_row(
                ctx, "Conda package",
                [
                    field for field, value in (
                        ("subdir", subdir), ("name", name),
                        ("version", version), ("build", build),
                    ) if not value
                ],
            )
        if not archive_type:
            malformed_inventory_row(ctx, "Conda package", ["archive type"])
        common_type, extension = conda_archive_type(archive_type)
        filename = f"{name}-{version}-{build}{extension}"
        checksum = best_hash(row, (
            ("sha256", ("SHA256Hash_Bytes", "PackageSha256_Bytes", "sha256")),
            ("sha1", ("SHA1Hash_Bytes", "sha1")),
            ("md5", ("MD5Hash_Bytes", "md5")),
        ))
        artifacts.append(make_artifact(
            "conda", (subdir, name, version, build, conda_extension(archive_type)),
            f"{subdir}/{filename}",
            package_url(ctx.source, "conda", ctx.source_feed.name, subdir, filename),
            common_download_url(
                ctx.destination, ctx.destination_feed.name, name, version,
                qualifier=package_qualifier(
                    build=build, subdir=subdir, type=common_type,
                ),
            ),
            filename=filename, checksum=checksum, size=row_get(row, "Package_Size", "size"),
            metadata={"subdir": subdir, "name": name, "version": version, "build": build},
        ))
    return artifacts


def fetch_bytes(client: ProGetClient, url: str, accept: Optional[str] = None) -> Tuple[bytes, Headers]:
    headers = {"Accept": accept} if accept else None
    response = client.request("GET", url, headers=headers)
    if response.status_code != READ_SUCCESS_STATUS:
        raise HttpError("GET", response.url, response.status_code, response.text)
    return response.content, response.headers


class LinkCollector(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        values = {str(name).lower(): value for name, value in attrs}
        href = values.get("href")
        if href:
            self.links.append(href)


def parse_links(body: bytes) -> List[str]:
    parser = LinkCollector()
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.links


def remap_feed_url(url: str, source: ProGetClient, destination: ProGetClient,
                   route: str, source_feed: str, destination_feed: str) -> Optional[str]:
    parsed = urlparse(url)
    if http_origin(url) != http_origin(source.base_url):
        return None
    base_path = urlparse(source.base_url).path.rstrip("/")
    prefix = f"{base_path}/{route.strip('/')}/{path_quote(source_feed)}/"
    if not parsed.path.startswith(prefix):
        # ProGet may render the feed name without percent encoding in an absolute URL.
        prefix = f"{base_path}/{route.strip('/')}/{source_feed}/"
        if not parsed.path.startswith(prefix):
            return None
    suffix = parsed.path[len(prefix):]
    return destination.url(
        f"/{route.strip('/')}/{path_quote(destination_feed)}/{suffix}"
    )


def pypi_project_name(project_url: str) -> str:
    segments = [unquote(part) for part in urlparse(project_url).path.split("/") if part]
    if not segments or segments[-1].lower() == "simple":
        raise SyncError(f"cannot determine PyPI project name from {redact_url(project_url)}")
    return segments[-1]


def normalize_pypi_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def pypi_download_coordinates(client: ProGetClient, feed: str,
                               expected_project: str,
                               source_url: str) -> Tuple[str, str, str]:
    """Read project/version/file from ProGet 22's canonical download href."""
    parsed = urlparse(source_url)
    prefix = urlparse(package_url(client, "pypi", feed, "download")).path.rstrip("/") + "/"
    if http_origin(source_url) != http_origin(client.base_url) or not parsed.path.startswith(prefix):
        raise SyncError(
            "PyPI index returned a non-canonical download URL: "
            + redact_url(source_url)
        )
    relative = parsed.path[len(prefix):]
    parts = [unquote(value) for value in relative.split("/")]
    if len(parts) != 3 or not all(parts):
        raise SyncError(
            "cannot read project/version/file from PyPI download URL: "
            + redact_url(source_url)
        )
    project, version, filename = parts
    if normalize_pypi_name(project) != normalize_pypi_name(expected_project):
        raise SyncError(
            f"PyPI project mismatch: index page is {expected_project!r}, "
            f"download link is {project!r}"
        )
    return project, version, filename


def pypi_project_artifacts(ctx: InventoryContext, project_url: str) -> List[Artifact]:
    body, _ = fetch_bytes(ctx.source, project_url, "text/html")
    index_project = pypi_project_name(project_url)
    artifacts = []
    for href in parse_links(body):
        source_url = urljoin(project_url, href)
        parsed = urlparse(source_url)
        project, version, filename = pypi_download_coordinates(
            ctx.source, ctx.source_feed.name, index_project, source_url,
        )
        destination_url = common_download_url(
            ctx.destination, ctx.destination_feed.name, project, version,
            qualifier=package_qualifier(file=filename),
        )
        checksum_algorithm = None
        checksum_value = None
        fragment = parsed.fragment
        if "=" in fragment:
            checksum_algorithm, checksum_value = fragment.split("=", 1)
            checksum_algorithm = checksum_algorithm.lower()
            checksum_value = checksum_value.lower()
            if checksum_algorithm not in hashlib.algorithms_available:
                checksum_algorithm = checksum_value = None
        source_url = urlunparse(parsed._replace(fragment=""))
        artifacts.append(make_artifact(
            "pypi", (normalize_pypi_name(project), version, filename),
            f"{project} {version} / {filename}", source_url, destination_url,
            filename=filename, checksum=(checksum_algorithm, checksum_value),
            metadata={
                "uploadFilename": True, "name": project, "version": version,
                "qualifier": package_qualifier(file=filename),
            },
        ))
    return artifacts


def inventory_pypi(ctx: InventoryContext) -> List[Artifact]:
    require_protocol_inventory_opt_in(ctx, "PyPI")
    root_url = package_url(ctx.source, "pypi", ctx.source_feed.name, "simple") + "/"
    body, _ = fetch_bytes(ctx.source, root_url, "text/html")
    project_urls = []
    seen = set()
    for href in parse_links(body):
        url = urljoin(root_url, href)
        key = urlunparse(urlparse(url)._replace(query="", fragment=""))
        if key not in seen:
            seen.add(key)
            project_urls.append(key)

    artifacts: List[Artifact] = []
    workers = max(1, min(ctx.options.inventory_workers, 16))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(pypi_project_artifacts, ctx, url): url for url in project_urls}
        for future in as_completed(futures):
            try:
                artifacts.extend(future.result())
            except Exception as exc:
                raise SyncError(
                    f"failed to enumerate PyPI project {redact_url(futures[future])}: {exc}"
                ) from exc
    artifacts.sort(key=lambda item: item.identity)
    return artifacts


def decompress_repository_file(data: bytes, name: str) -> bytes:
    lower = name.lower()
    if lower.endswith(".gz"):
        return gzip.decompress(data)
    if lower.endswith(".bz2"):
        return bz2.decompress(data)
    if lower.endswith(".xz"):
        import lzma
        return lzma.decompress(data)
    return data


def find_xml_text(element: ET.Element, path: str, default: str = "") -> str:
    child = element.find(path)
    return default if child is None or child.text is None else child.text.strip()


def inventory_rpm(ctx: InventoryContext) -> List[Artifact]:
    require_protocol_inventory_opt_in(ctx, "RPM")
    repomd_url = package_url(ctx.source, "rpm", ctx.source_feed.name, "repodata", "repomd.xml")
    repomd_bytes, _ = fetch_bytes(ctx.source, repomd_url, "application/xml")
    try:
        repomd = ET.fromstring(repomd_bytes)
    except ET.ParseError as exc:
        raise SyncError(f"invalid RPM repomd.xml in {ctx.source_feed.name!r}: {exc}") from exc
    primary_href = None
    for data in repomd.findall(".//{*}data"):
        if data.attrib.get("type") != "primary":
            continue
        location = data.find("{*}location")
        if location is not None:
            primary_href = location.attrib.get("href")
            break
    if not primary_href:
        raise SyncError(f"RPM feed {ctx.source_feed.name!r} has no primary metadata")
    primary_url = urljoin(package_url(ctx.source, "rpm", ctx.source_feed.name) + "/", primary_href)
    primary_bytes, _ = fetch_bytes(ctx.source, primary_url)
    try:
        primary = ET.fromstring(decompress_repository_file(primary_bytes, primary_href))
    except (ET.ParseError, OSError, EOFError) as exc:
        raise SyncError(f"invalid RPM primary metadata in {ctx.source_feed.name!r}: {exc}") from exc

    artifacts = []
    for package in primary.findall(".//{*}package"):
        if package.attrib.get("type", "rpm") != "rpm":
            continue
        name = find_xml_text(package, "{*}name")
        architecture = find_xml_text(package, "{*}arch")
        version_node = package.find("{*}version")
        location_node = package.find("{*}location")
        checksum_node = package.find("{*}checksum")
        if version_node is None or location_node is None:
            malformed_inventory_row(
                ctx, "RPM package",
                [
                    field for field, value in (
                        ("version metadata", version_node),
                        ("location metadata", location_node),
                    ) if value is None
                ],
            )
        version = version_node.attrib.get("ver", "")
        release = version_node.attrib.get("rel", "")
        epoch = version_node.attrib.get("epoch", "")
        full_version = version + ("-" + release if release else "")
        if epoch and epoch not in ("0", "(none)"):
            full_version = epoch + ":" + full_version
        href = location_node.attrib.get("href", "")
        filename = unquote(os.path.basename(urlparse(href).path))
        if not all((name, architecture, version, href, filename)):
            malformed_inventory_row(
                ctx, "RPM package",
                [
                    field for field, value in (
                        ("name", name), ("architecture", architecture),
                        ("version", version), ("location", href),
                        ("filename", filename),
                    ) if not value
                ],
            )
        algorithm = checksum_node.attrib.get("type", "").lower() if checksum_node is not None else None
        checksum_value = (checksum_node.text or "").strip().lower() if checksum_node is not None else None
        source_url = urljoin(package_url(ctx.source, "rpm", ctx.source_feed.name) + "/", href)
        destination_url = common_download_url(
            ctx.destination, ctx.destination_feed.name, name, full_version,
            qualifier=package_qualifier(arch=architecture),
        )
        artifacts.append(make_artifact(
            "rpm", (name, full_version, architecture, filename),
            f"{name} {full_version} ({architecture})",
            source_url, destination_url, filename=filename,
            checksum=(algorithm, checksum_value),
            upload_kind="package",
            metadata={
                "uploadFilename": True, "name": name, "version": full_version,
                "architecture": architecture,
                "qualifier": package_qualifier(arch=architecture),
            },
        ))
    return artifacts


def yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    return value


def parse_helm_index(data: bytes) -> List[Dict[str, str]]:
    """Parse the small, regular subset of YAML emitted by ProGet's Helm index."""
    entries_started = False
    current_chart = ""
    current: Optional[Dict[str, str]] = None
    in_urls = False
    records: List[Dict[str, str]] = []

    def flush() -> None:
        nonlocal current
        if current is not None:
            current.setdefault("name", current_chart)
            missing = [field for field in ("name", "version", "url") if not current.get(field)]
            if missing:
                raise SyncError(
                    "incomplete Helm index record (missing: " + ", ".join(missing) + ")"
                )
            records.append(current)
        current = None

    for raw_line in data.decode("utf-8-sig", errors="replace").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        if not entries_started:
            if stripped == "entries:":
                entries_started = True
            continue
        if indent == 0:
            flush()
            break
        if indent == 2 and not stripped.startswith("-") and stripped.endswith(":"):
            flush()
            current_chart = yaml_scalar(stripped[:-1])
            in_urls = False
            continue
        if current is not None and in_urls and stripped.startswith("-") and indent >= 4:
            current.setdefault("url", yaml_scalar(stripped[1:]))
            continue
        if indent in (2, 4) and stripped.startswith("-"):
            flush()
            current = {"name": current_chart}
            in_urls = False
            remainder = stripped[1:].strip()
            if ":" in remainder:
                key, value = remainder.split(":", 1)
                current[key.strip()] = yaml_scalar(value)
            continue
        if current is None:
            continue
        if stripped == "urls:":
            in_urls = True
            continue
        if in_urls and stripped.startswith("-"):
            current.setdefault("url", yaml_scalar(stripped[1:]))
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = yaml_scalar(value)
            in_urls = False
    flush()
    return records


def inventory_helm(ctx: InventoryContext) -> List[Artifact]:
    require_protocol_inventory_opt_in(ctx, "Helm")
    index_url = package_url(ctx.source, "helm", ctx.source_feed.name, "index.yaml")
    body, _ = fetch_bytes(ctx.source, index_url, "text/yaml")
    records = parse_helm_index(body)
    if b"entries:" in body and not records and re.search(br"(?m)^\s{2}[^:#]+:\s*$", body):
        raise SyncError(
            f"Helm index format in {ctx.source_feed.name!r} could not be parsed safely"
        )
    artifacts = []
    for row in records:
        name = row.get("name", "")
        version = row.get("version", "")
        source_url = urljoin(index_url, row.get("url", ""))
        filename = unquote(os.path.basename(urlparse(source_url).path)) or f"{name}-{version}.tgz"
        digest = row.get("digest", "")
        artifacts.append(make_artifact(
            "helm", (name, version), f"{name} {version}", source_url,
            common_download_url(
                ctx.destination, ctx.destination_feed.name, name, version,
            ),
            filename=filename, checksum=("sha256" if digest else None, digest or None),
            metadata={"name": name, "version": version},
        ))
    return artifacts


def inventory_asset(ctx: InventoryContext) -> List[Artifact]:
    list_url = package_url(ctx.source, "endpoints", ctx.source_feed.name, "dir") + "/"
    value = ctx.source.get_json(list_url, params={"recursive": "true"})
    rows = unwrap_rows(value, "asset directory listing")
    artifacts = []
    for row in rows:
        item_type = str(row_get(row, "type", "ContentType", default="") or "")
        if item_type.lower() == "dir":
            continue
        name = str(row_get(row, "name", default="") or "")
        parent = str(row_get(row, "parent", default="") or "").strip("/")
        if not name:
            malformed_inventory_row(ctx, "asset directory item", ["name"])
        item_path = "/".join(part for part in (parent, name) if part)
        source_content = row_get(row, "content", "url")
        source_url = (
            ctx.source.url(str(source_content)) if source_content
            else package_url(ctx.source, "endpoints", ctx.source_feed.name, "content", *item_path.split("/"))
        )
        destination_url = package_url(
            ctx.destination, "endpoints", ctx.destination_feed.name,
            "content", *item_path.split("/"),
        )
        checksum = best_hash(row, (
            ("sha512", ("sha512",)),
            ("sha256", ("sha256",)),
            ("sha1", ("sha1",)),
            ("md5", ("md5",)),
        ))
        artifacts.append(make_artifact(
            "asset", (item_path,), item_path, source_url, destination_url,
            filename=name, checksum=checksum, size=row_get(row, "size"),
            upload_kind="asset",
            metadata={"contentType": item_type or "application/octet-stream"},
        ))
    return artifacts


INVENTORY_READERS = {
    "nuget": inventory_nuget,
    "npm": inventory_npm,
    "bower": inventory_bower,
    "maven": inventory_maven,
    "universal": inventory_universal,
    "rubygems": inventory_rubygems,
    "vsix": inventory_vsix,
    "asset": inventory_asset,
    "debian": inventory_debian,
    "pypi": inventory_pypi,
    "helm": inventory_helm,
    "rpm": inventory_rpm,
    "conda": inventory_conda,
}


@dataclass
class ProbeResult:
    exists: bool
    checksum: Optional[str] = None
    status: Optional[int] = None


def response_content_length(response: Response) -> Optional[int]:
    value = response.headers.get("Content-Length")
    if value is None or str(value).strip() == "":
        return None
    try:
        length = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TransportError(
            f"invalid Content-Length for {redact_url(response.url)}: {value!r}"
        ) from exc
    if length < 0:
        raise TransportError(
            f"negative Content-Length for {redact_url(response.url)}: {length}"
        )
    return length


def ensure_complete_response(response: Response, total: int,
                             expected: Optional[int]) -> None:
    remaining = getattr(response.raw, "length", None)
    if expected is not None and total != expected:
        raise TransportError(
            f"truncated response for {redact_url(response.url)}: "
            f"received {total} of {expected} bytes"
        )
    if isinstance(remaining, int) and remaining > 0:
        raise TransportError(
            f"truncated response for {redact_url(response.url)}: "
            f"{remaining} declared bytes were not received"
        )


def response_hash(response: Response, algorithm: str) -> Tuple[str, int]:
    try:
        digest = hashlib.new(algorithm)
    except (ValueError, TypeError) as exc:
        response.close()
        raise SyncError(f"unsupported checksum algorithm {algorithm!r}") from exc
    total = 0
    try:
        expected = response_content_length(response)
        while True:
            chunk = response.raw.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
        ensure_complete_response(response, total, expected)
    except TransportError:
        raise
    except Exception as exc:
        raise TransportError(f"response body was interrupted for {redact_url(response.url)}: {exc}") from exc
    finally:
        response.close()
    return digest.hexdigest(), total


def probe_artifact(client: ProGetClient, artifact: Artifact,
                   verify_checksum: bool = False) -> ProbeResult:
    if not artifact.destination_url:
        return ProbeResult(False)

    if artifact.upload_kind == "bower":
        expected = str(artifact.metadata.get("repository", ""))
        if not expected:
            raise SyncError(f"{artifact.display}: source Bower repository URL is empty")
        response = client.request("GET", artifact.destination_url, stream=False)
        if response.status_code == 404:
            return ProbeResult(False, status=404)
        if response.status_code != READ_SUCCESS_STATUS:
            raise HttpError("GET", response.url, response.status_code, response.text)
        # Bower packages are repository registrations, not byte archives.
        try:
            value = response.json()
        except ValueError:
            value = {}
        repository = ""
        if isinstance(value, dict):
            repository = str(row_get(value, "url", "Repository_Url", "repository", default="") or "")
        if not repository:
            raise SyncError(
                f"{artifact.display}: destination Bower response has no repository URL"
            )
        checksum = hashlib.sha256(repository.encode("utf-8")).hexdigest()
        return ProbeResult(True, checksum=checksum, status=response.status_code)

    if verify_checksum:
        algorithm = artifact.checksum_algorithm or "sha256"
        response = client.request("GET", artifact.destination_url, stream=True)
        if response.status_code == 404:
            response.close()
            return ProbeResult(False, status=404)
        if response.status_code != READ_SUCCESS_STATUS:
            body = response.raw.read(4096).decode("utf-8", errors="replace")
            response.close()
            raise HttpError("GET", response.url, response.status_code, body)
        checksum, _ = response_hash(response, algorithm)
        return ProbeResult(True, checksum=checksum, status=response.status_code)

    response = client.request("HEAD", artifact.destination_url, stream=True)
    if response.status_code in (405, 501):
        response.close()
        response = client.request("GET", artifact.destination_url, stream=True)
    status = response.status_code
    if status == 404:
        response.close()
        return ProbeResult(False, status=status)
    if status != READ_SUCCESS_STATUS:
        body = response.raw.read(4096).decode("utf-8", errors="replace") if response.method != "HEAD" else ""
        response.close()
        raise HttpError(response.method, response.url, status, body)
    response.close()
    return ProbeResult(True, status=status)


def source_artifact_hash(client: ProGetClient, artifact: Artifact,
                         algorithm: str = "sha256") -> Tuple[str, int]:
    if not artifact.source_url:
        raise SyncError(f"{artifact.display}: source URL is unavailable")
    response = client.request("GET", artifact.source_url, stream=True)
    if response.status_code != READ_SUCCESS_STATUS:
        body = response.raw.read(4096).decode("utf-8", errors="replace")
        response.close()
        raise HttpError("GET", response.url, response.status_code, body)
    checksum, size = response_hash(response, algorithm)
    if artifact.size is not None and size != artifact.size:
        raise SyncError(
            f"{artifact.display}: source returned {size} bytes, inventory says {artifact.size}"
        )
    return checksum, size


def destination_artifact_sha256(client: ProGetClient, artifact: Artifact) -> str:
    if not artifact.destination_url:
        raise SyncError(f"{artifact.display}: destination URL is unavailable")
    response = client.request("GET", artifact.destination_url, stream=True)
    if response.status_code != READ_SUCCESS_STATUS:
        body = response.raw.read(4096).decode("utf-8", errors="replace")
        response.close()
        raise HttpError("GET", response.url, response.status_code, body)
    checksum, _ = response_hash(response, "sha256")
    return checksum


@dataclass
class DownloadedFile:
    path: str
    size: int
    sha256: str
    content_type: str


def download_artifact(client: ProGetClient, artifact: Artifact,
                      spool_dir: Optional[str]) -> DownloadedFile:
    if not artifact.source_url:
        raise SyncError(f"{artifact.display}: source URL is unavailable")
    response = client.request("GET", artifact.source_url, stream=True)
    if response.status_code != READ_SUCCESS_STATUS:
        body = response.raw.read(4096).decode("utf-8", errors="replace")
        response.close()
        raise HttpError("GET", response.url, response.status_code, body)

    try:
        expected_length = response_content_length(response)
    except Exception:
        response.close()
        raise

    hashers = {"sha256": hashlib.sha256()}
    if artifact.checksum_algorithm and artifact.checksum_algorithm not in hashers:
        try:
            hashers[artifact.checksum_algorithm] = hashlib.new(artifact.checksum_algorithm)
        except ValueError as exc:
            response.close()
            raise SyncError(
                f"{artifact.display}: unsupported source checksum {artifact.checksum_algorithm}"
            ) from exc
    suffix = "-" + re.sub(r"[^A-Za-z0-9._-]", "_", artifact.filename or "artifact")
    handle = tempfile.NamedTemporaryFile(
        mode="wb", prefix="proget-delta-", suffix=suffix,
        dir=spool_dir, delete=False,
    )
    path = handle.name
    total = 0
    try:
        with handle:
            while True:
                chunk = response.raw.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
                for hasher in hashers.values():
                    hasher.update(chunk)
        ensure_complete_response(response, total, expected_length)
        response.close()
    except Exception as exc:
        response.close()
        try:
            os.unlink(path)
        except OSError:
            pass
        raise TransportError(f"download interrupted for {artifact.display}: {exc}") from exc

    if artifact.size is not None and total != artifact.size:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise SyncError(
            f"{artifact.display}: downloaded {total} bytes, source inventory says {artifact.size}"
        )
    if artifact.checksum and artifact.checksum_algorithm:
        actual = hashers[artifact.checksum_algorithm].hexdigest()
        if actual.lower() != artifact.checksum.lower():
            try:
                os.unlink(path)
            except OSError:
                pass
            raise SyncError(
                f"{artifact.display}: source checksum mismatch "
                f"({artifact.checksum_algorithm} {actual}, expected {artifact.checksum})"
            )
    return DownloadedFile(
        path=path,
        size=total,
        sha256=hashers["sha256"].hexdigest(),
        content_type=response.headers.get("Content-Type", "application/octet-stream")
        or "application/octet-stream",
    )


def common_upload_url(client: ProGetClient, feed: Feed, artifact: Artifact) -> str:
    url = client.url(f"/api/packages/{path_quote(feed.name)}/upload")
    if artifact.metadata.get("uploadFilename"):
        if not artifact.filename:
            raise SyncError(f"{artifact.display}: upload filename is required")
        url += "/" + path_quote(artifact.filename)
    if artifact.feed_type == "debian":
        url = Session._merge_params(url, {
            "distribution": artifact.metadata.get("distribution"),
            "component": artifact.metadata.get("component"),
        })
    return url


def upload_bower(client: ProGetClient, feed: Feed, artifact: Artifact) -> None:
    body = urlencode({
        "name": artifact.metadata.get("name", ""),
        "url": artifact.metadata.get("repository", ""),
    }).encode("utf-8")
    url = package_url(client, "bower", feed.name, "packages")
    response = client.request(
        "POST", url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body, retry=False,
    )
    if response.status_code not in WRITE_SUCCESS_STATUSES:
        raise HttpError("POST", response.url, response.status_code, response.text)


def upload_file(client: ProGetClient, feed: Feed, artifact: Artifact,
                downloaded: DownloadedFile, overwrite: bool = False) -> None:
    if artifact.upload_kind == "maven":
        method = "PUT"
        url = artifact.destination_url
    elif artifact.upload_kind == "asset":
        method = "POST" if overwrite else "PUT"
        url = artifact.destination_url
    else:
        method = "PUT"
        url = common_upload_url(client, feed, artifact)
    if not url:
        raise SyncError(f"{artifact.display}: destination upload URL is unavailable")
    content_type = (
        artifact.metadata.get("contentType")
        if artifact.upload_kind == "asset"
        else "application/octet-stream"
    ) or downloaded.content_type
    headers = {
        "Content-Type": str(content_type),
        "Content-Length": str(downloaded.size),
    }
    with open(downloaded.path, "rb") as handle:
        response = client.request(
            method, url, headers=headers, data=handle,
            stream=False, retry=False,
        )
    if response.status_code not in WRITE_SUCCESS_STATUSES:
        raise HttpError(method, response.url, response.status_code, response.text)


@dataclass
class EngineOptions:
    workers: int = 2
    dry_run: bool = False
    compare_only: bool = False
    verify_existing: bool = False
    on_conflict: str = "fail"
    spool_dir: Optional[str] = None


class DeltaSyncEngine:
    def __init__(self, source: ProGetClient, destination: ProGetClient,
                 source_feed: Feed, destination_feed: Feed,
                 options: EngineOptions, state: JsonlState) -> None:
        self.source = source
        self.destination = destination
        self.source_feed = source_feed
        self.destination_feed = destination_feed
        self.options = options
        self.state = state

    def _result(self, artifact: Artifact, status: str, **extra: Any) -> Dict[str, Any]:
        item = {
            "key": artifact.canonical_key(),
            "display": artifact.display,
            "status": status,
            "sourceChecksum": artifact.checksum,
            "checksumAlgorithm": artifact.checksum_algorithm,
            "stateSeen": self.state.contains(artifact),
        }
        item.update(extra)
        return item

    def _sync_one(self, artifact: Artifact) -> Dict[str, Any]:
        try:
            probe = probe_artifact(
                self.destination, artifact,
                verify_checksum=self.options.verify_existing,
            )
            if (self.options.verify_existing and probe.exists
                    and not (artifact.checksum and artifact.checksum_algorithm)):
                checksum, _ = source_artifact_hash(self.source, artifact, "sha256")
                artifact.checksum_algorithm = "sha256"
                artifact.checksum = checksum
            classification = classify_probe(probe.exists, artifact.checksum, probe.checksum)
            if classification == "matched":
                return self._result(
                    artifact, "matched", destinationChecksum=probe.checksum,
                )
            overwrite = classification == "conflict" and self.options.on_conflict == "overwrite"
            if classification == "conflict" and not overwrite:
                return self._result(
                    artifact,
                    "conflict-skipped" if self.options.on_conflict == "skip" else "conflict",
                    destinationChecksum=probe.checksum,
                    error="destination contains different bytes",
                )
            if self.options.compare_only:
                return self._result(
                    artifact, "conflict" if classification == "conflict" else "missing",
                    destinationChecksum=probe.checksum,
                )
            if self.options.dry_run:
                return self._result(
                    artifact, "would-overwrite" if overwrite else "would-upload",
                    destinationChecksum=probe.checksum,
                )

            if artifact.upload_kind == "bower":
                upload_bower(self.destination, self.destination_feed, artifact)
                post = probe_artifact(self.destination, artifact, verify_checksum=True)
                if not post.exists:
                    raise SyncError("Bower registration was not visible after upload")
                if not post.checksum or post.checksum != artifact.checksum:
                    raise SyncError("Bower repository URL differs after upload")
                self.state.record(artifact, "uploaded")
                return self._result(artifact, "uploaded")

            downloaded = download_artifact(self.source, artifact, self.options.spool_dir)
            if self.options.verify_existing and not artifact.checksum:
                artifact.checksum_algorithm = "sha256"
                artifact.checksum = downloaded.sha256
            post: Optional[ProbeResult] = None
            try:
                upload_file(
                    self.destination, self.destination_feed, artifact,
                    downloaded, overwrite=overwrite,
                )
            except TransportError as exc:
                # A server can commit an upload and lose the response.  Probe before
                # reporting failure. For overwrite, existence proves nothing because
                # the old object was already present, so keep the transport failure.
                if overwrite:
                    raise
                try:
                    destination_sha256 = destination_artifact_sha256(
                        self.destination, artifact,
                    )
                except Exception:
                    destination_sha256 = None
                if destination_sha256 != downloaded.sha256:
                    raise exc
            finally:
                try:
                    os.unlink(downloaded.path)
                except OSError:
                    pass

            if post is None:
                post = probe_artifact(
                    self.destination, artifact,
                    verify_checksum=self.options.verify_existing,
                )
            if not post.exists:
                raise SyncError("artifact was not visible on destination after upload")
            if (self.options.verify_existing and artifact.checksum and post.checksum
                    and artifact.checksum.lower() != post.checksum.lower()):
                raise SyncError("destination checksum differs after upload")
            self.state.record(
                artifact, "uploaded", bytes=downloaded.size, sha256=downloaded.sha256,
            )
            return self._result(
                artifact, "overwritten" if overwrite else "uploaded",
                bytes=downloaded.size, sha256=downloaded.sha256,
                destinationChecksum=post.checksum,
            )
        except Exception as exc:
            return self._result(artifact, "failed", error=str(exc))

    def run(self, artifacts: Sequence[Artifact]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        workers = max(1, min(self.options.workers, 16))
        if workers == 1:
            for artifact in artifacts:
                result = self._sync_one(artifact)
                results.append(result)
                log_item_result(result)
            return results
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(self._sync_one, item): item for item in artifacts}
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                log_item_result(result)
        results.sort(key=lambda item: item["key"])
        return results


def log_item_result(result: Mapping[str, Any]) -> None:
    status = result.get("status")
    display = result.get("display")
    if status in ("failed", "conflict", "conflict-skipped", "missing"):
        LOG.error("[%s] %s%s", status, display,
                  ": " + str(result.get("error")) if result.get("error") else "")
    elif status in ("uploaded", "overwritten", "would-upload", "would-overwrite"):
        LOG.info("[%s] %s", status, display)
    else:
        LOG.debug("[%s] %s", status, display)


def parse_feed_mapping(value: str) -> Tuple[str, str]:
    value = value.strip()
    if not value:
        raise ValueError("empty feed mapping")
    if "=" in value:
        source, destination = value.split("=", 1)
    elif ":" in value:
        source, destination = value.split(":", 1)
    else:
        source = destination = value
    source, destination = source.strip(), destination.strip()
    if not source or not destination:
        raise ValueError(f"invalid feed mapping {value!r}")
    return source, destination


def feed_lookup(feeds: Sequence[Feed], name: str) -> Optional[Feed]:
    wanted = name.casefold()
    return next((feed for feed in feeds if feed.name.casefold() == wanted), None)


def discover_destination_feeds(client: ProGetClient) -> List[Feed]:
    management_error: Optional[Exception] = None
    try:
        feeds = client.list_feeds_management()
        if feeds:
            return feeds
    except Exception as exc:
        management_error = exc
    try:
        feeds = client.list_feeds_native()
        if feeds:
            return feeds
    except Exception as native_error:
        if management_error:
            raise SyncError(
                "cannot list destination feeds through Management or Native API: "
                f"{management_error}; {native_error}"
            ) from native_error
        raise
    if management_error:
        raise SyncError(f"cannot list destination feeds: {management_error}")
    raise SyncError(
        "destination returned an empty feed list; verify the API key and its feed permissions"
    )


def tls_verify(insecure: bool, ca_file: Optional[str]) -> Any:
    if insecure:
        return False
    return ca_file or True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Догружает отсутствующие пакеты и файлы между ProGet. "
            "Docker намеренно исключён и переносится proget_docker_migrate.py."
        ),
    )
    parser.add_argument("--src-url", default=os.getenv("PROGET_SRC_URL"))
    parser.add_argument("--src-api-key", default=os.getenv("PROGET_SRC_API_KEY"))
    parser.add_argument("--dst-url", default=os.getenv("PROGET_DST_URL"))
    parser.add_argument("--dst-api-key", default=os.getenv("PROGET_DST_API_KEY"))
    parser.add_argument(
        "--feed", action="append", default=[], metavar="SOURCE[=DESTINATION]",
        help="фид или соответствие имён; можно повторять",
    )
    parser.add_argument("--src-feed", help="имя одного source-фида (совместимо со старым CLI)")
    parser.add_argument("--dst-feed", help="имя destination-фида; по умолчанию как source")
    parser.add_argument("--all-feeds", action="store_true", help="все фиды с теми же именами")
    parser.add_argument("--exclude-feed", action="append", default=[], metavar="GLOB")

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list-only", action="store_true", help="только вывести source-инвентарь")
    mode.add_argument("--dry-run", action="store_true", help="показать план без загрузки файлов")
    mode.add_argument("--compare", action="store_true", help="только сверить наличие/хеши")

    parser.add_argument("--verify-existing", action="store_true",
                        help="скачивать существующие destination-файлы и сравнивать хеш")
    parser.add_argument("--on-conflict", choices=("fail", "skip", "overwrite"), default="fail")
    parser.add_argument("--include-cached", action="store_true",
                        help=(
                            "включить cached; для PyPI/RPM/Helm с connectors явно "
                            "разрешить всё видимое содержимое"
                        ))
    parser.add_argument("--include-virtual", action="store_true",
                        help="материализовать и перенести virtual Universal packages")
    parser.add_argument("--debian-distribution",
                        help="distribution на новом Debian-фиде, например stable")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--inventory-workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900.0)
    parser.add_argument("--spool-dir", help="каталог для одного временного package-файла на worker")
    parser.add_argument("--state-file", help="append-only JSONL для аудита успешных загрузок")
    parser.add_argument("--report", help="итоговый JSON-отчёт")
    parser.add_argument("--log-file")
    parser.add_argument("--src-ca")
    parser.add_argument("--dst-ca")
    parser.add_argument("--insecure", action="store_true", help="отключить проверку TLS на обоих ProGet")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser


def configure_logging(verbose: int, log_file: Optional[str]) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers: List[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def write_report(path: str, value: Mapping[str, Any]) -> None:
    absolute = os.path.abspath(path)
    parent = os.path.dirname(absolute)
    os.makedirs(parent, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".proget-report-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, absolute)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def selected_mappings(args: argparse.Namespace, source_feeds: Sequence[Feed]) -> List[Tuple[str, str]]:
    if args.all_feeds and (args.feed or args.src_feed or args.dst_feed):
        raise SyncError("--all-feeds cannot be combined with --feed/--src-feed/--dst-feed")
    if args.feed and (args.src_feed or args.dst_feed):
        raise SyncError("use either --feed or --src-feed/--dst-feed")
    if args.all_feeds:
        mappings = [(feed.name, feed.name) for feed in source_feeds]
    elif args.feed:
        try:
            mappings = [parse_feed_mapping(value) for value in args.feed]
        except ValueError as exc:
            raise SyncError(str(exc)) from exc
    elif args.src_feed:
        mappings = [(args.src_feed, args.dst_feed or args.src_feed)]
    else:
        raise SyncError("specify --all-feeds, --feed, or --src-feed")
    result = []
    seen = set()
    for source, destination in mappings:
        if any(fnmatch.fnmatchcase(source, pattern) for pattern in args.exclude_feed):
            continue
        key = (source.casefold(), destination.casefold())
        if key not in seen:
            seen.add(key)
            result.append((source, destination))
    return result


def count_statuses(feed_reports: Sequence[Mapping[str, Any]]) -> Counter:
    counts: Counter = Counter()
    for report in feed_reports:
        if report.get("status") != "processed":
            counts[str(report.get("status", "unknown"))] += 1
        for item in report.get("items", []):
            counts[str(item.get("status", "unknown"))] += 1
    return counts


def run(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    required = ["src_url", "src_api_key"]
    if not args.list_only:
        required.extend(("dst_url", "dst_api_key"))
    for name in required:
        if not getattr(args, name, None):
            raise SyncError("missing --" + name.replace("_", "-"))
    if args.workers < 1 or args.inventory_workers < 1:
        raise SyncError("worker counts must be positive")
    if args.spool_dir and not os.path.isdir(args.spool_dir):
        raise SyncError(f"spool directory does not exist: {args.spool_dir}")

    source = ProGetClient(
        args.src_url, args.src_api_key,
        verify=tls_verify(args.insecure, args.src_ca),
        timeout=args.timeout, retries=args.retries,
    )
    destination = source if args.list_only else ProGetClient(
        args.dst_url, args.dst_api_key,
        verify=tls_verify(args.insecure, args.dst_ca),
        timeout=args.timeout, retries=args.retries,
    )
    started = utc_now()
    report: Dict[str, Any] = {
        "started": started,
        "source": source.base_url,
        "destination": None if args.list_only else destination.base_url,
        "mode": (
            "list" if args.list_only else "dry-run" if args.dry_run
            else "compare" if args.compare else "sync"
        ),
        "feeds": [],
    }
    try:
        source_feeds = source.list_feeds_native()
        if not source_feeds:
            raise SyncError(
                "source returned an empty feed list; the key needs Native API Access"
            )
        mappings = selected_mappings(args, source_feeds)
        if not mappings:
            raise SyncError("no feeds matched the selection")
        destination_feeds = [] if args.list_only else discover_destination_feeds(destination)
        state_path = None if (args.list_only or args.dry_run or args.compare) else args.state_file
        state = JsonlState(
            state_path, source.base_url,
            # JsonlState is disabled in list mode, but it still canonicalizes
            # its scope. Reuse the source URL instead of an invalid sentinel.
            destination.base_url,
        )

        for requested_source, requested_destination in mappings:
            source_feed = feed_lookup(source_feeds, requested_source)
            if source_feed is None:
                report["feeds"].append({
                    "sourceFeed": requested_source,
                    "destinationFeed": requested_destination,
                    "status": "failed",
                    "error": "source feed not found or not visible to the API key",
                })
                continue
            if source_feed.feed_type == "docker":
                message = "Docker is excluded; use proget_docker_migrate.py"
                LOG.info("[%s] %s", source_feed.name, message)
                report["feeds"].append({
                    "sourceFeed": source_feed.name,
                    "destinationFeed": requested_destination,
                    "feedType": source_feed.raw_type or source_feed.feed_type,
                    "status": "docker-skipped",
                    "error": message,
                })
                continue
            destination_feed = (
                Feed(
                    requested_destination, source_feed.feed_type, None,
                    source_feed.raw_type,
                )
                if args.list_only
                else feed_lookup(destination_feeds, requested_destination)
            )
            if destination_feed is None:
                report["feeds"].append({
                    "sourceFeed": source_feed.name,
                    "destinationFeed": requested_destination,
                    "feedType": source_feed.raw_type or source_feed.feed_type,
                    "status": "failed",
                    "error": "destination feed not found or not visible to the API key",
                })
                continue
            if source_feed.feed_type != destination_feed.feed_type:
                report["feeds"].append({
                    "sourceFeed": source_feed.name,
                    "destinationFeed": destination_feed.name,
                    "feedType": source_feed.raw_type or source_feed.feed_type,
                    "status": "failed",
                    "error": (
                        "feed type mismatch: source=" + (source_feed.raw_type or source_feed.feed_type)
                        + ", destination=" + (destination_feed.raw_type or destination_feed.feed_type)
                    ),
                })
                continue
            reader = INVENTORY_READERS.get(source_feed.feed_type)
            if reader is None:
                report["feeds"].append({
                    "sourceFeed": source_feed.name,
                    "destinationFeed": destination_feed.name,
                    "feedType": source_feed.raw_type or source_feed.feed_type,
                    "status": "unsupported",
                    "error": "no safe adapter for this feed type",
                })
                continue
            if (not args.list_only and source.base_url == destination.base_url
                    and source_feed.name.casefold() == destination_feed.name.casefold()):
                report["feeds"].append({
                    "sourceFeed": source_feed.name,
                    "destinationFeed": destination_feed.name,
                    "feedType": source_feed.raw_type or source_feed.feed_type,
                    "status": "failed",
                    "error": "source and destination are the same feed",
                })
                continue

            LOG.info(
                "Инвентаризация %s (%s) -> %s",
                source_feed.name, source_feed.raw_type or source_feed.feed_type,
                destination_feed.name,
            )
            inventory_options = InventoryOptions(
                include_cached=args.include_cached,
                include_virtual=args.include_virtual,
                inventory_workers=args.inventory_workers,
                debian_distribution=args.debian_distribution,
                source_only=args.list_only,
            )
            context = InventoryContext(
                source, destination, source_feed, destination_feed, inventory_options,
            )
            feed_report: Dict[str, Any] = {
                "sourceFeed": source_feed.name,
                "destinationFeed": destination_feed.name,
                "feedType": source_feed.raw_type or source_feed.feed_type,
            }
            try:
                artifacts = reader(context)
                if source_feed.feed_type == "maven":
                    artifacts.sort(key=lambda item: (
                        item.identity[:3],
                        0 if (item.filename or "").lower().endswith(".pom") else 1,
                        item.filename or "",
                    ))
                for artifact in artifacts:
                    artifact.metadata.setdefault("sourceFeed", source_feed.name)
                    artifact.metadata.setdefault("destinationFeed", destination_feed.name)
                feed_report["sourceItems"] = len(artifacts)
                feed_report["filteredCached"] = context.filtered_cached
                feed_report["filteredVirtual"] = context.filtered_virtual
                LOG.info(
                    "%s: найдено %d; отфильтровано cached=%d, virtual=%d",
                    source_feed.name, len(artifacts),
                    context.filtered_cached, context.filtered_virtual,
                )
                if args.list_only:
                    for artifact in artifacts:
                        print(f"{source_feed.name}\t{source_feed.feed_type}\t{artifact.display}")
                    feed_report["status"] = "processed"
                    feed_report["items"] = [
                        {"key": item.canonical_key(), "display": item.display, "status": "listed"}
                        for item in artifacts
                    ]
                else:
                    engine = DeltaSyncEngine(
                        source, destination, source_feed, destination_feed,
                        EngineOptions(
                            # Maven metadata is most reliable when the POM reaches
                            # ProGet before its sibling JAR/classifier files.
                            workers=1 if source_feed.feed_type == "maven" else args.workers,
                            dry_run=args.dry_run,
                            compare_only=args.compare,
                            verify_existing=args.verify_existing,
                            on_conflict=args.on_conflict,
                            spool_dir=args.spool_dir,
                        ),
                        state,
                    )
                    feed_report["items"] = engine.run(artifacts)
                    feed_report["status"] = "processed"
            except Exception as exc:
                LOG.error("[%s] %s", source_feed.name, exc)
                feed_report["status"] = "failed"
                feed_report["error"] = str(exc)
            report["feeds"].append(feed_report)

        counts = count_statuses(report["feeds"])
        report["counts"] = dict(sorted(counts.items()))
        report["finished"] = utc_now()
        bad = (
            counts["failed"] + counts["unsupported"]
            + counts["conflict"] + counts["conflict-skipped"]
        )
        if args.compare:
            bad += counts["missing"]
        LOG.info("Итог: %s", ", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "пусто")
        return (1 if bad else 0), report
    finally:
        source.session.close()
        if destination is not source:
            destination.session.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    try:
        code, report = run(args)
    except (SyncError, ValueError) as exc:
        LOG.error("Не удалось начать: %s", exc)
        return 2
    if args.report:
        try:
            write_report(args.report, report)
        except Exception as exc:
            LOG.error("Не удалось записать отчёт: %s", exc)
            return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
