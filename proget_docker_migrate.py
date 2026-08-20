#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
proget_docker_migrate.py — перенос Docker-образов из фида ProGet в фид другого ProGet.

Источник:  ProGet 2022.28 build 4 (Windows)
Приёмник:  ProGet последней версии (Linux)

Работает напрямую по Docker Registry V2 API: без docker-демона, без skopeo/crane,
без выгрузки образов на диск — блобы стримятся source -> destination.
Зависимостей нет: только стандартная библиотека Python 3.8+ (для закрытого контура).

Почему скриптом, а не встроенными средствами ProGet:
  * Feed Importer (ProGet -> ProGet) требует источник >= 2023 (у нас 2022.28);
  * Bulk Upload / drop path не поддерживают Docker-фиды;
  * Feed Replication требует Enterprise на обеих сторонах;
  * pgutil не умеет копировать образы между серверами.

Особенности ProGet, учтённые здесь:
  * реестр висит на КОРНЕ сервера: <base>/v2/, а имя фида — первый сегмент
    имени репозитория: /v2/<feed>/<repo>/manifests/<ref>;
  * /v2/<feed>/_catalog не существует — работает только глобальный /v2/_catalog,
    имена в нём вида "<feed>/<repo>" (фильтруем без учёта регистра);
  * на 2022.x API-ключ в Basic на /v2/_catalog может молча деградировать до
    анонимного доступа (получите "пустой фид" вместо ошибки) — поэтому основной
    путь аутентификации это bearer-ticket через <base>/v2/_auth со scope;
  * ProGet <= 2022 автоматически добавлял префикс "library/" к репозиториям без
    namespace, а 2023+ — уже нет (см. --library-prefix);
  * двоеточие в ?digest= обязано быть percent-encoded (IIS режет ':' в query);
  * foreign / non-distributable слои (типичны для Windows-образов) не хранятся
    в реестре — их нельзя качать и нельзя заливать, дескриптор едет в манифесте.

Пример:
  python3 proget_docker_migrate.py \
      --src-url https://proget-old.corp.local --src-feed docker --src-api-key OLDKEY \
      --dst-url https://proget-new.corp.local --dst-feed docker --dst-api-key NEWKEY \
      --workers 2 --state-file migrate.state.jsonl --report report.json
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

LOG = logging.getLogger("proget-migrate")

# ---------------------------------------------------------------------------
# Транспорт: минимальный HTTP-клиент на stdlib (никаких внешних зависимостей —
# скрипт должен запускаться в закрытом контуре, где pip install недоступен).
# Поддерживает keep-alive, потоковую отправку тела с Content-Length, потоковое
# чтение ответа, редиректы со сбросом Authorization при смене хоста и TLS.
# ---------------------------------------------------------------------------

import http.client
import ssl
from email.message import Message


class TransportError(Exception):
    """Сетевая ошибка (соединение, таймаут, обрыв) — аналог requests.RequestException."""


class Headers:
    """Регистронезависимый доступ к заголовкам ответа."""

    def __init__(self, items: Iterable[Tuple[str, str]]) -> None:
        self._items = list(items)
        self._map = {k.lower(): v for k, v in self._items}

    def get(self, name: str, default: Any = None) -> Any:
        return self._map.get(name.lower(), default)

    def __contains__(self, name: str) -> bool:
        return name.lower() in self._map

    def items(self) -> List[Tuple[str, str]]:
        return list(self._items)

    def __repr__(self) -> str:
        return f"Headers({self._items!r})"


class Response:
    """Урезанный аналог requests.Response: только то, что использует скрипт."""

    def __init__(self, method: str, url: str, raw: "http.client.HTTPResponse",
                 session: "Session", key: Tuple[str, str, int], stream: bool) -> None:
        self.status_code = raw.status
        self.reason = raw.reason
        self.headers = Headers(raw.getheaders())
        self.url = url
        self.raw = raw
        self._session = session
        self._key = key
        self._stream = stream
        self._content: Optional[bytes] = None
        self._released = False
        self._method = method

    @property
    def content(self) -> bytes:
        if self._content is None:
            try:
                self._content = b"" if self._method == "HEAD" else (self.raw.read() or b"")
            except Exception as exc:
                self._content = b""
                self._session.drop(self._key)
                raise TransportError(f"обрыв при чтении ответа {self.url}: {exc}") from exc
            self._release(reusable=True)
        return self._content

    @property
    def text(self) -> str:
        ctype = self.headers.get("Content-Type", "") or ""
        charset = "utf-8"
        if "charset=" in ctype:
            charset = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        body = self.content
        if not body:
            raise ValueError("пустое тело ответа")
        return json.loads(body.decode("utf-8", errors="replace"))

    def _release(self, reusable: bool) -> None:
        if self._released:
            return
        self._released = True
        if reusable:
            self._session.release(self._key)
        else:
            self._session.drop(self._key)

    def close(self) -> None:
        """Вернуть соединение в пул (если ответ дочитан) либо закрыть его."""
        if self._released:
            return
        if self._method == "HEAD":
            self._release(reusable=True)
            return
        if self._content is not None:
            self._release(reusable=True)
            return
        # Недочитанный поток: дочитываем, если остаток невелик, иначе рвём соединение.
        try:
            length = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            length = -1
        if 0 <= length <= 1024 * 1024:
            try:
                self.raw.read()
                self._release(reusable=True)
                return
            except Exception:
                pass
        try:
            self.raw.close()
        except Exception:
            pass
        self._release(reusable=False)


class Session:
    """
    Пул HTTP(S)-соединений: по одному живому соединению на (схема, хост, порт)
    на каждый поток. Интерфейс намеренно повторяет requests.Session в объёме,
    который использует скрипт.
    """

    def __init__(self, connect_timeout: float = 30.0, read_timeout: float = 900.0,
                 verify: Any = True, blocksize: int = 1024 * 1024) -> None:
        self.headers: Dict[str, str] = {}
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.verify = verify
        self.blocksize = blocksize
        self._local = threading.local()
        self._ssl_contexts: Dict[Any, "ssl.SSLContext"] = {}
        self._ssl_guard = threading.Lock()

    # -- соединения --------------------------------------------------------

    def _pool(self) -> Dict[Tuple[str, str, int], Any]:
        pool = getattr(self._local, "pool", None)
        if pool is None:
            pool = {}
            self._local.pool = pool
        return pool

    def _ssl_context(self, verify: Any) -> "ssl.SSLContext":
        key = verify if isinstance(verify, (str, bool)) else True
        with self._ssl_guard:
            ctx = self._ssl_contexts.get(key)
            if ctx is None:
                if verify is False:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                elif isinstance(verify, str):
                    ctx = ssl.create_default_context(cafile=verify)
                else:
                    ctx = ssl.create_default_context()
                self._ssl_contexts[key] = ctx
            return ctx

    def _connection(self, key: Tuple[str, str, int], verify: Any) -> Any:
        pool = self._pool()
        conn = pool.get(key)
        if conn is not None:
            return conn
        scheme, host, port = key
        if scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=self.connect_timeout,
                                               context=self._ssl_context(verify),
                                               blocksize=self.blocksize)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout,
                                              blocksize=self.blocksize)
        pool[key] = conn
        return conn

    def release(self, key: Tuple[str, str, int]) -> None:
        """Соединение осталось валидным — просто оставляем его в пуле."""

    def drop(self, key: Tuple[str, str, int]) -> None:
        conn = self._pool().pop(key, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # -- запросы -----------------------------------------------------------

    @staticmethod
    def _split(url: str) -> Tuple[Tuple[str, str, int], str]:
        parts = urlparse(url)
        scheme = parts.scheme or "http"
        host = parts.hostname or ""
        port = parts.port or (443 if scheme == "https" else 80)
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return (scheme, host, port), path

    @staticmethod
    def _merge_params(url: str, params: Any) -> str:
        if not params:
            return url
        pairs: List[Tuple[str, str]] = []
        items = params.items() if isinstance(params, dict) else list(params)
        for key, value in items:
            if value is None:
                continue
            pairs.append((str(key), str(value)))
        if not pairs:
            return url
        parts = urlparse(url)
        existing = parse_qsl(parts.query, keep_blank_values=True)
        query = urlencode(existing + pairs, quote_via=quote)
        return urlunparse(parts._replace(query=query))

    def request(self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None,
                data: Any = None, params: Any = None, stream: bool = False,
                timeout: Any = None, verify: Any = None,
                allow_redirects: bool = True, _redirects: int = 0) -> Response:
        method = method.upper()
        url = self._merge_params(url, params)
        verify = self.verify if verify is None else verify
        connect_timeout, read_timeout = self.connect_timeout, self.read_timeout
        if isinstance(timeout, (tuple, list)) and len(timeout) == 2:
            connect_timeout, read_timeout = float(timeout[0]), float(timeout[1])
        elif isinstance(timeout, (int, float)):
            connect_timeout = read_timeout = float(timeout)

        key, path = self._split(url)
        if not key[1]:
            raise TransportError(f"некорректный URL: {url}")

        send_headers: Dict[str, str] = dict(self.headers)
        send_headers.setdefault("Accept-Encoding", "identity")
        if headers:
            for name, value in headers.items():
                if value is not None:
                    send_headers[name] = value

        body: Any = data
        if isinstance(body, str):
            body = body.encode("utf-8")
        if body is None and method in ("POST", "PUT", "PATCH"):
            body = b""
        if isinstance(body, (bytes, bytearray)):
            send_headers.setdefault("Content-Length", str(len(body)))

        attempt_reuse = True
        while True:
            conn = self._connection(key, verify)
            fresh = conn.sock is None
            try:
                if fresh:
                    conn.timeout = connect_timeout
                    conn.connect()
                if conn.sock is not None:
                    conn.sock.settimeout(read_timeout)
                conn.request(method, path, body=body, headers=send_headers)
                raw = conn.getresponse()
            except Exception as exc:
                self.drop(key)
                # Переиспользованное соединение могло протухнуть — один раз пробуем заново,
                # но только если тело можно отправить повторно.
                if attempt_reuse and not fresh and isinstance(body, (bytes, bytearray, type(None))):
                    attempt_reuse = False
                    continue
                raise TransportError(f"{method} {url}: {exc}") from exc
            break

        response = Response(method, url, raw, self, key, stream)
        if allow_redirects and response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if location and _redirects < 5:
                target = urljoin(url, location)
                response.close()
                new_headers = dict(headers or {})
                # Не отдаём учётные данные другому хосту (например, внешнему хранилищу).
                if urlparse(target).netloc != urlparse(url).netloc:
                    new_headers.pop("Authorization", None)
                next_method = "GET" if response.status_code == 303 else method
                next_body = None if next_method == "GET" else body
                if next_body is not None and not isinstance(next_body, (bytes, bytearray)):
                    raise TransportError(f"{method} {url}: редирект при потоковой отправке тела")
                return self.request(next_method, target, headers=new_headers, data=next_body,
                                    stream=stream, timeout=(connect_timeout, read_timeout),
                                    verify=verify, allow_redirects=True, _redirects=_redirects + 1)
        if not stream:
            response.content  # дочитываем и возвращаем соединение в пул
        return response

    def get(self, url: str, **kwargs: Any) -> Response:
        return self.request("GET", url, **kwargs)

    def close(self) -> None:
        for key in list(self._pool()):
            self.drop(key)

# ---------------------------------------------------------------------------
# Константы протокола
# ---------------------------------------------------------------------------

MT_DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
MT_OCI_INDEX = "application/vnd.oci.image.index.v1+json"
MT_OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_SCHEMA1 = "application/vnd.docker.distribution.manifest.v1+json"
MT_SCHEMA1_JWS = "application/vnd.docker.distribution.manifest.v1+prettyjws"

INDEX_TYPES = frozenset({MT_DOCKER_LIST, MT_OCI_INDEX})
MANIFEST_TYPES = frozenset({MT_DOCKER_MANIFEST, MT_OCI_MANIFEST})
SCHEMA1_TYPES = frozenset({MT_SCHEMA1, MT_SCHEMA1_JWS})
ALL_MANIFEST_TYPES = INDEX_TYPES | MANIFEST_TYPES | SCHEMA1_TYPES

# Accept для GET манифеста. schema1 держим в конце: он нужен, чтобы вообще
# получить древние образы, но приоритет отдаём index/list, иначе старый реестр
# может тихо отдать schema1 вместо schema2 (потеря данных).
ACCEPT_FULL = ", ".join([MT_OCI_INDEX, MT_DOCKER_LIST, MT_OCI_MANIFEST, MT_DOCKER_MANIFEST,
                         MT_SCHEMA1_JWS, MT_SCHEMA1])
# Узкий Accept — для перепроверки, действительно ли образ хранится как schema1.
ACCEPT_MODERN = ", ".join([MT_OCI_INDEX, MT_DOCKER_LIST, MT_OCI_MANIFEST, MT_DOCKER_MANIFEST])

# Слои, которые физически не хранятся в реестре (Windows base layers и т.п.).
NON_DISTRIBUTABLE = frozenset({
    "application/vnd.docker.image.rootfs.foreign.diff.tar",
    "application/vnd.docker.image.rootfs.foreign.diff.tar.gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+gzip",
    "application/vnd.oci.image.layer.nondistributable.v1.tar+zstd",
})

RETRY_STATUSES = frozenset({408, 423, 429, 500, 502, 503, 504})
USER_AGENT = "proget-docker-migrate/2.0 (docker/registry-v2)"
PROBE_REPO = "__migration_probe__"
TOKEN_TTL = 240.0  # перевыпускаем ticket заранее: заливка большого слоя может пережить его срок
EMPTY_JSON_BLOB = b"{}"
EMPTY_JSON_DIGEST = "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
MIN_CHUNK = 256 * 1024

REPO_NAME_RE = re.compile(r"^[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")


class MigrateError(Exception):
    """Ошибка переноса, показываемая пользователю как есть."""


class HttpError(MigrateError):
    def __init__(self, message: str, status: int, code: str = "", body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------


_PARAM_RE = re.compile(r'^([A-Za-z0-9_-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^\s,]*))$')
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")


def parse_challenges(value: str) -> Dict[str, Dict[str, str]]:
    """
    Разбор WWW-Authenticate, устойчивый к нескольким схемам сразу
    ('Bearer realm="...",service="...", Negotiate, NTLM' на Windows/IIS),
    к отсутствию пробела после запятой и к запятым внутри кавычек.
    """
    out: Dict[str, Dict[str, str]] = {}
    if not value:
        return out

    # режем по запятым вне кавычек: realm может содержать ','
    parts: List[str] = []
    buf: List[str] = []
    in_quotes = escaped = False
    for ch in value:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if in_quotes and ch == "\\":
            buf.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            buf.append(ch)
            continue
        if ch == "," and not in_quotes:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))

    def put(target: Optional[Dict[str, str]], item: str) -> bool:
        match = _PARAM_RE.match(item)
        if not match:
            return False
        if target is not None:
            target.setdefault(match.group(1).lower(),
                              match.group(2) if match.group(2) is not None else (match.group(3) or ""))
        return True

    current: Optional[Dict[str, str]] = None
    for raw in parts:
        item = raw.strip()
        if not item:
            continue
        if current is not None and put(current, item):
            continue
        scheme, _, rest = item.partition(" ")
        if not _SCHEME_RE.fullmatch(scheme):
            continue
        current = out.setdefault(scheme.lower(), {})
        rest = rest.strip()
        if rest:
            put(current, rest)
    return out


def proget_error(resp: "Response") -> Tuple[str, str]:
    """Разбор тела ошибки ProGet: {"errors":[{"code":..,"message":..,"detail":..}]}."""
    try:
        data = resp.json()
    except Exception:
        text = (resp.text or "")[:400]
        return "", text.strip()
    errors = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errors, list) and errors:
        first = errors[0] or {}
        code = str(first.get("code") or "")
        message = str(first.get("message") or "")
        detail = first.get("detail")
        if detail and not isinstance(detail, (dict, list)):
            message = f"{message} ({detail})"
        return code, message
    return "", json.dumps(data, ensure_ascii=False)[:400]


def raise_http(label: str, method: str, url: str, resp: "Response") -> "HttpError":
    code, message = proget_error(resp)
    text = f"[{label}] {method} {url} -> HTTP {resp.status_code}"
    if code:
        text += f" {code}"
    if message:
        text += f": {message}"
    return HttpError(text, resp.status_code, code, message)


def with_digest_param(location: str, digest: str) -> str:
    """Добавить ?digest=... к URL сессии загрузки, корректно кодируя ':'."""
    parts = urlparse(location)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "digest"]
    query.append(("digest", digest))
    return urlunparse(parts._replace(query=urlencode(query, quote_via=quote)))


def next_link(headers: Any, current_url: str) -> Optional[str]:
    link = headers.get("Link") or headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        m = re.search(r"<([^>]+)>", part)
        if m and 'rel="next"' in part.replace(" ", "").replace("'", '"'):
            return urljoin(current_url, m.group(1))
    return None


def human_size(num: Optional[int]) -> str:
    if not num:
        return "0 B"
    val = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if val < 1024.0:
            return f"{val:.1f} {unit}"
        val /= 1024.0
    return f"{val:.1f} PiB"


def matches_any(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in patterns)


def backoff_delay(attempt: int, cap: float = 60.0) -> float:
    return random.uniform(0.5, min(cap, 2.0 ** attempt))


class RawReader:
    """Обёртка над urllib3-потоком с корректным decode_content."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def read(self, amt: int = -1) -> bytes:
        if amt is None or amt < 0:
            amt = 1024 * 1024
        try:
            return self._raw.read(amt)
        except (TransportError, MigrateError):
            raise
        except Exception as exc:  # обрыв/таймаут посреди тела должен попасть в ретрай блоба
            raise TransportError(f"обрыв при чтении блоба: {exc}") from exc


class SizedStream:
    """
    Тело запроса с известной длиной: http.client отправляет объект с read()
    блоками, а Content-Length мы задаём явно (chunked transfer-encoding хуже
    переваривается IIS/nginx перед ProGet). Попутно считается sha256.
    """

    def __init__(self, source: Any, length: int) -> None:
        self._source = source
        self.len = int(length)
        self._hash = hashlib.sha256()
        self._read = 0

    def read(self, amt: int = -1) -> bytes:
        if amt is None or amt < 0:
            amt = 1024 * 1024
        chunk = self._source.read(amt)
        if chunk:
            self._hash.update(chunk)
            self._read += len(chunk)
        return chunk

    def __iter__(self):
        while True:
            chunk = self.read(1024 * 1024)
            if not chunk:
                return
            yield chunk

    def __len__(self) -> int:
        return self.len

    @property
    def digest(self) -> str:
        return "sha256:" + self._hash.hexdigest()

    @property
    def consumed(self) -> int:
        return self._read


def read_exact(source: Any, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = source.read(size - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Клиент Docker Registry V2 для ProGet
# ---------------------------------------------------------------------------


class Registry:
    def __init__(
        self,
        label: str,
        base_url: str,
        feed: str,
        username: Optional[str],
        password: Optional[str],
        *,
        verify: Any = True,
        connect_timeout: float = 30.0,
        read_timeout: float = 900.0,
        retries: int = 5,
        pool_size: int = 16,  # оставлено для совместимости вызова
        auth_mode: str = "auto",
    ) -> None:
        self.label = label
        self.base = base_url.rstrip("/")
        self.feed = feed.strip("/")
        self.username = username
        self.password = password
        self.verify = verify
        self.timeout = (connect_timeout, read_timeout)
        self.retries = max(1, retries)

        self.session = Session(connect_timeout=connect_timeout, read_timeout=read_timeout,
                               verify=verify)
        self.session.headers.update({"User-Agent": USER_AGENT})

        self.auth_mode = auth_mode  # auto -> bearer | basic | anonymous
        self.realm = f"{self.base}/v2/_auth"
        self.service = urlparse(self.base).netloc
        self.version = "unknown"
        self.identity = "?"
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self._token_locks: Dict[str, threading.Lock] = {}
        self._token_lock = threading.Lock()

    # -- имена -------------------------------------------------------------

    def repo_name(self, repo: str) -> str:
        """Полное имя репозитория в терминах registry-протокола: <feed>/<repo>."""
        repo = repo.strip("/")
        if self.feed and (repo.lower() == self.feed.lower() or repo.lower().startswith(self.feed.lower() + "/")):
            return repo
        return f"{self.feed}/{repo}" if self.feed else repo

    def strip_feed(self, name: str) -> str:
        name = name.strip("/")
        if self.feed and name.lower().startswith(self.feed.lower() + "/"):
            return name[len(self.feed) + 1:]
        return name

    def url(self, *parts: str) -> str:
        return "/".join([self.base, "v2"] + [str(p).strip("/") for p in parts if p not in (None, "")])

    # -- аутентификация ----------------------------------------------------

    def _basic_header(self) -> Optional[str]:
        if not self.username:
            return None
        raw = f"{self.username}:{self.password or ''}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _auth_headers(self, scope: Optional[str]) -> Dict[str, str]:
        if self.auth_mode == "bearer":
            token = self._token(scope or "")
            if token:
                return {"Authorization": f"Bearer {token}"}
            return {}
        if self.auth_mode == "anonymous":
            return {}
        basic = self._basic_header()
        return {"Authorization": basic} if basic else {}

    def _token(self, scope: str, refresh: bool = False) -> Optional[str]:
        """
        Кэш токенов с TTL. Сетевой запрос делается под блокировкой ЭТОГО scope,
        а не глобальной: иначе один медленный /v2/_auth останавливает все потоки.
        """
        entered = time.time()
        with self._token_lock:
            entry = self._tokens.get(scope)
            if entry and not refresh and entered - entry[1] < TOKEN_TTL:
                return entry[0]
            lock = self._token_locks.get(scope)
            if lock is None:
                lock = threading.Lock()
                self._token_locks[scope] = lock
        with lock:
            with self._token_lock:
                entry = self._tokens.get(scope)
                if entry and (entry[1] > entered if refresh else time.time() - entry[1] < TOKEN_TTL):
                    return entry[0]
            token = self._fetch_token(scope)
            if token:
                with self._token_lock:
                    self._tokens[scope] = (token, time.time())
            return token

    def _fetch_token(self, scope: str) -> Optional[str]:
        params: List[Tuple[str, str]] = []
        if self.service:
            params.append(("service", self.service))
        if scope:
            params.append(("scope", scope))
        headers = {}
        basic = self._basic_header()
        if basic:
            headers["Authorization"] = basic
        last: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = self.session.get(self.realm, params=params, headers=headers,
                                        timeout=self.timeout, verify=self.verify)
            except TransportError as exc:
                last = exc
                if attempt >= self.retries:
                    raise MigrateError(f"[{self.label}] токен-эндпоинт {self.realm}: {exc}") from exc
                time.sleep(backoff_delay(attempt))
                continue
            if resp.status_code == 403:
                code, message = proget_error(resp)
                raise MigrateError(
                    f"[{self.label}] {self.realm} -> 403 {code}: учётные данные отклонены сервером. "
                    f"Проверьте API-ключ/пароль (для API-ключа логин должен быть 'api'). {message}"
                )
            if resp.status_code in RETRY_STATUSES and attempt < self.retries:
                resp.close()
                time.sleep(backoff_delay(attempt))
                continue
            if resp.status_code != 200:
                raise raise_http(self.label, "GET", self.realm, resp)
            try:
                data = resp.json()
            except ValueError as exc:
                raise MigrateError(f"[{self.label}] {self.realm}: ответ не JSON") from exc
            token = data.get("token") or data.get("access_token")
            if not token:
                raise MigrateError(f"[{self.label}] {self.realm}: в ответе нет token/access_token")
            if token == "anonymous" and self.username:
                LOG.warning(
                    "[%s] токен-эндпоинт сопоставил учётные данные с Anonymous — вероятно, ключ "
                    "не даёт прав на фид '%s'. Данные могут оказаться неполными!", self.label, self.feed
                )
            return token
        raise MigrateError(f"[{self.label}] не удалось получить токен: {last}")

    def pull_scope(self, repo: str) -> str:
        return f"repository:{self.repo_name(repo)}:pull"

    def push_scope(self, repo: str) -> str:
        return f"repository:{self.repo_name(repo)}:pull,push"

    # -- HTTP с ретраями ---------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        scope: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        stream: bool = False,
        data: Any = None,
        params: Any = None,
        replayable: bool = True,
        expect: Optional[Iterable[int]] = None,
    ) -> "Response":
        attempts = self.retries if replayable else 1
        expect_set = set(expect) if expect is not None else None
        last: Optional[Exception] = None
        refreshed = False
        attempt = 0
        while attempt < attempts:
            attempt += 1
            hdrs = self._auth_headers(scope)
            if headers:
                hdrs.update(headers)
            try:
                resp = self.session.request(
                    method, url, headers=hdrs, data=data, params=params, stream=stream,
                    timeout=self.timeout, verify=self.verify, allow_redirects=True,
                )
            except TransportError as exc:
                last = exc
                if attempt >= attempts:
                    raise MigrateError(f"[{self.label}] {method} {url}: {exc}") from exc
                delay = backoff_delay(attempt)
                LOG.warning("[%s] %s %s: %s — повтор через %.0f c", self.label, method, url, exc, delay)
                time.sleep(delay)
                continue

            # Протух токен — перевыпустить и повторить один раз.
            if resp.status_code == 401 and self.auth_mode == "bearer" and scope is not None \
                    and not refreshed and replayable:
                refreshed = True
                attempts += 1  # перевыпуск токена не должен съедать попытку
                resp.close()
                self._token(scope or "", refresh=True)
                continue

            if resp.status_code in RETRY_STATUSES and replayable and attempt < attempts:
                retry_after = resp.headers.get("Retry-After")
                code, message = proget_error(resp)
                resp.close()
                delay = min(120.0, float(retry_after)) if (retry_after or "").isdigit() \
                    else backoff_delay(attempt)
                LOG.warning("[%s] %s %s -> HTTP %s %s %s — повтор через %.0f c",
                            self.label, method, url, resp.status_code, code, message[:120], delay)
                time.sleep(delay)
                continue

            if expect_set is not None and resp.status_code not in expect_set:
                err = raise_http(self.label, method, url, resp)
                resp.close()
                raise err
            return resp
        raise MigrateError(f"[{self.label}] {method} {url}: исчерпаны попытки ({last})")

    # -- preflight ---------------------------------------------------------

    def detect_version(self) -> str:
        for path, params in (("/health", {"format": "json"}), ("/health/json", None), ("/api/health", None)):
            try:
                resp = self.session.get(self.base + path, params=params, timeout=(self.timeout[0], 30),
                                        verify=self.verify)
            except TransportError:
                continue
            header_version = resp.headers.get("X-ProGet-Version")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = {}
                version = data.get("versionNumber") or data.get("applicationVersion") or header_version
                if version:
                    self.version = str(version)
                    return self.version
            if header_version:
                self.version = str(header_version)
                return self.version
        return self.version

    def setup_auth(self) -> None:
        """
        Определяем схему аутентификации. Основной путь — bearer-ticket через
        <base>/v2/_auth: на ProGet 2022.x прямой Basic на /v2/_catalog может молча
        деградировать до анонимного доступа и вернуть неполные данные.
        """
        url = self.base + "/v2/"
        try:
            resp = self.session.get(url, timeout=self.timeout, verify=self.verify, allow_redirects=True)
        except TransportError as exc:
            raise MigrateError(f"[{self.label}] {url} недоступен: {exc}") from exc

        api_header = resp.headers.get("Docker-Distribution-API-Version")
        challenges = parse_challenges(resp.headers.get("WWW-Authenticate", ""))
        resp.close()
        if not api_header and resp.status_code not in (200, 401):
            raise MigrateError(
                f"[{self.label}] {url} -> HTTP {resp.status_code} и без заголовка "
                f"Docker-Distribution-API-Version: похоже, --{self.label}-url указывает не на корень ProGet."
            )
        if "bearer" in challenges:
            params = challenges["bearer"]
            realm = params.get("realm") or self.realm
            # ProGet за reverse proxy умеет анонсировать realm со схемой http, хотя сам
            # доступен по https: понижать схему нельзя — уйдёт Basic открытым текстом.
            realm_parts, base_parts = urlparse(realm), urlparse(self.base)
            if base_parts.scheme == "https" and realm_parts.scheme == "http" \
                    and realm_parts.hostname == base_parts.hostname:
                realm = urlunparse(realm_parts._replace(scheme="https", netloc=base_parts.netloc))
                LOG.warning("[%s] сервер анонсировал токен-эндпоинт по http — использую https (%s)",
                            self.label, realm)
            self.realm = realm
            self.service = params.get("service") or self.service
        if {"negotiate", "ntlm"} & set(challenges):
            LOG.warning("[%s] сервер также предлагает %s (Integrated Windows Auth) — если всё 401-ит, "
                        "администратору источника нужно разрешить Basic/token-аутентификацию",
                        self.label, "/".join(sorted({"negotiate", "ntlm"} & set(challenges))))

        if not self.username:
            self.auth_mode = "anonymous"
            LOG.warning("[%s] учётные данные не заданы — работаем анонимно", self.label)
        elif self.auth_mode in ("auto", "bearer"):
            probe_scope = self.pull_scope(PROBE_REPO)
            try:
                token = self._token(probe_scope)
                self.auth_mode = "bearer" if token else "basic"
            except MigrateError as exc:
                if self.auth_mode == "bearer":
                    raise
                if isinstance(exc, HttpError) and exc.status in (404, 405):
                    LOG.info("[%s] токен-эндпоинт недоступен (%s), переключаюсь на Basic", self.label, exc.status)
                    self.auth_mode = "basic"
                else:
                    raise
        LOG.info("[%s] режим аутентификации: %s", self.label, self.auth_mode)
        self.identity = self.whoami()

    def whoami(self) -> str:
        """GET /v2/ отдаёт 'Authenticated as ProGet user: <name>' — ловим анонимную деградацию."""
        url = self.base + "/v2/"
        try:
            resp = self.request("GET", url, scope="registry:catalog:*", expect=None)
        except MigrateError:
            return "?"
        text = ""
        try:
            text = (resp.text or "").strip()
        except Exception:
            pass
        code = resp.status_code
        resp.close()
        if code != 200:
            return "?"
        m = re.search(r"Authenticated as[^:]*:\s*(.+)", text)
        name = m.group(1).strip() if m else (text[:80] or "?")
        if self.username and name.lower() in ("anonymous", "anonymous user"):
            LOG.warning("[%s] сервер видит вас как Anonymous, хотя учётные данные заданы. "
                        "Список репозиториев и тегов может быть неполным!", self.label)
        return name

    def check_feed(self) -> None:
        """
        Проверка существования фида: у ProGet несуществующий репозиторий в живом фиде
        даёт 200 c пустым списком тегов, а несуществующий ФИД — 400 UNKNOWN.
        """
        url = self.url(self.repo_name(PROBE_REPO), "tags/list")
        resp = self.request("GET", url, scope=self.pull_scope(PROBE_REPO), expect=None)
        code = resp.status_code
        err_code, message = ("", "")
        canonical = None
        if code == 200:
            try:
                canonical = (resp.json() or {}).get("name")
            except ValueError:
                canonical = None
        else:
            err_code, message = proget_error(resp)
        resp.close()
        if code == 400 or "could not resolve proget feed" in (message or "").lower():
            raise MigrateError(
                f"[{self.label}] фид '{self.feed}' не найден на {self.base}: {message or err_code}"
            )
        if code in (401, 403):
            raise MigrateError(
                f"[{self.label}] нет доступа к фиду '{self.feed}' (HTTP {code}): {message}. "
                f"Нужны права: источник — View & Download Packages, приёмник — Publish Packages "
                f"(и Overwrite/Delete, если планируется перезапись тегов)."
            )
        if canonical:
            actual_feed = str(canonical).split("/")[0]
            if actual_feed and actual_feed != self.feed:
                LOG.info("[%s] каноническое имя фида: '%s' (задано '%s')", self.label, actual_feed, self.feed)
                self.feed = actual_feed

    # -- каталог и теги ----------------------------------------------------

    def list_repositories(self) -> List[str]:
        """
        Список репозиториев фида. В ProGet работает только глобальный /v2/_catalog,
        имена вида '<feed>/<repo>'; фильтруем по префиксу без учёта регистра.
        """
        url = self.url("_catalog")
        raw: List[str] = []
        current: Optional[str] = url
        seen: Set[str] = set()
        while current and current not in seen:
            seen.add(current)
            resp = self.request("GET", current, scope="registry:catalog:*", expect=None)
            if resp.status_code >= 400:
                err = raise_http(self.label, "GET", current, resp)
                resp.close()
                raise MigrateError(
                    f"{err}. Если каталог закрыт политикой, перечислите репозитории явно "
                    f"через --repo/--repos-file."
                )
            try:
                data = resp.json() or {}
            except ValueError:
                resp.close()
                raise MigrateError(f"[{self.label}] {current}: ответ не JSON")
            raw.extend(data.get("repositories") or [])
            current = next_link(resp.headers, current)
            resp.close()

        prefix = self.feed.lower() + "/"
        names = sorted({r.strip("/")[len(prefix):] for r in raw
                        if r and r.strip("/").lower().startswith(prefix)})
        LOG.info("[%s] в каталоге всего %d репозиториев, из них в фиде '%s': %d",
                 self.label, len(raw), self.feed, len(names))
        if raw and not names:
            LOG.warning("[%s] в каталоге нет ни одного репозитория с префиксом '%s/' — проверьте имя фида",
                        self.label, self.feed)
        return names

    def list_tags(self, repo: str) -> List[str]:
        url = self.url(self.repo_name(repo), "tags/list")
        tags: List[str] = []
        current: Optional[str] = url
        seen: Set[str] = set()
        while current and current not in seen:
            seen.add(current)
            resp = self.request("GET", current, scope=self.pull_scope(repo), expect=None)
            if resp.status_code == 404:
                resp.close()
                return []
            if resp.status_code >= 400:
                err = raise_http(self.label, "GET", current, resp)
                resp.close()
                raise err
            try:
                data = resp.json() or {}
            except ValueError:
                body = (resp.text or "")[:200]
                resp.close()
                raise MigrateError(
                    f"[{self.label}] {current}: ответ не JSON (получено: {body!r}). "
                    f"Вероятно, запрос перехватил reverse proxy или страница логина."
                )
            tags.extend(data.get("tags") or [])
            current = next_link(resp.headers, current)
            resp.close()
        return sorted({t for t in tags if t})

    # -- манифесты ---------------------------------------------------------

    def get_manifest(self, repo: str, reference: str, accept: str = ACCEPT_FULL) -> Tuple[bytes, str, Optional[str]]:
        url = self.url(self.repo_name(repo), "manifests", reference)
        resp = self.request("GET", url, scope=self.pull_scope(repo),
                            headers={"Accept": accept}, expect=(200,))
        body = resp.content
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        digest = resp.headers.get("Docker-Content-Digest")
        resp.close()
        return body, ctype, digest

    def head_manifest(self, repo: str, reference: str) -> Optional[str]:
        url = self.url(self.repo_name(repo), "manifests", reference)
        resp = self.request("HEAD", url, scope=self.pull_scope(repo),
                            headers={"Accept": ACCEPT_FULL}, expect=None)
        code = resp.status_code
        digest = resp.headers.get("Docker-Content-Digest")
        resp.close()
        if code == 200:
            return digest or ""
        if code in (401, 403):
            raise HttpError(f"[{self.label}] нет прав на {url} (HTTP {code})", code)
        return None

    def put_manifest(self, repo: str, reference: str, body: bytes, content_type: str) -> Optional[str]:
        url = self.url(self.repo_name(repo), "manifests", reference)
        resp = self.request("PUT", url, scope=self.push_scope(repo),
                            headers={"Content-Type": content_type, "Content-Length": str(len(body))},
                            data=body, expect=None)
        code = resp.status_code
        if 200 <= code < 300:
            digest = resp.headers.get("Docker-Content-Digest")
            resp.close()
            return digest
        err = raise_http(self.label, "PUT", url, resp)
        resp.close()
        if code in (403, 409):
            raise HttpError(
                f"{err}. Похоже, запрещена перезапись тега (нужно право Overwrite/Delete) "
                f"или включена проверка версий тегов (Container Versioning) на фиде-приёмнике.",
                code, err.code, err.body,
            )
        raise err

    # -- блобы -------------------------------------------------------------

    def blob_exists(self, repo: str, digest: str) -> Optional[int]:
        """Размер блоба, если он есть; None — если 404. Прочие коды — ошибка."""
        url = self.url(self.repo_name(repo), "blobs", digest)
        resp = self.request("HEAD", url, scope=self.pull_scope(repo), expect=None)
        code = resp.status_code
        length = resp.headers.get("Content-Length")
        resp.close()
        if code == 200:
            try:
                return int(length) if length is not None else 0
            except ValueError:
                return 0
        if code == 404:
            return None
        raise HttpError(f"[{self.label}] HEAD {url} -> HTTP {code}", code)

    def open_blob(self, repo: str, digest: str) -> "Response":
        url = self.url(self.repo_name(repo), "blobs", digest)
        return self.request("GET", url, scope=self.pull_scope(repo), stream=True,
                            headers={"Accept-Encoding": "identity"}, expect=(200,))

    def start_upload(self, repo: str, mount: Optional[str] = None,
                     mount_from: Optional[str] = None) -> Tuple[str, Optional[str]]:
        """('mounted', None) при успешном cross-repo mount, иначе ('upload', location)."""
        url = self.url(self.repo_name(repo), "blobs", "uploads") + "/"
        params = {"mount": mount, "from": mount_from} if (mount and mount_from) else None
        resp = self.request("POST", url, scope=self.push_scope(repo), params=params,
                            headers={"Content-Length": "0"}, data=b"", expect=(201, 202))
        code = resp.status_code
        location = resp.headers.get("Location")
        resolved = urljoin(resp.url, location) if location else None
        resp.close()
        if code == 201:
            return "mounted", None
        if not resolved:
            raise MigrateError(f"[{self.label}] POST {url} вернул 202 без заголовка Location")
        return "upload", resolved

    def cancel_upload(self, location: str, repo: str) -> None:
        try:
            resp = self.request("DELETE", location, scope=self.push_scope(repo),
                                headers={"Content-Length": "0"}, expect=None, replayable=False)
            resp.close()
        except Exception:
            pass

    def upload_blob(self, repo: str, digest: str, stream: SizedStream, location: str,
                    chunk_size: int) -> None:
        """PATCH (одним потоком или чанками) -> PUT ?digest=. location уже получен через POST."""
        scope = self.push_scope(repo)
        try:
            if stream.len == 0:
                pass  # пустой блоб: сразу финализируем
            elif chunk_size and stream.len > chunk_size:
                offset = 0
                while True:
                    chunk = read_exact(stream, chunk_size)
                    if not chunk:
                        break
                    resp = self.request(
                        "PATCH", location, scope=scope, data=chunk, replayable=False, expect=None,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
                        },
                    )
                    if not (200 <= resp.status_code < 300):
                        err = raise_http(self.label, "PATCH", location, resp)
                        resp.close()
                        raise err
                    new_loc = resp.headers.get("Location")
                    if new_loc:
                        location = urljoin(resp.url, new_loc)
                    resp.close()
                    offset += len(chunk)
            else:
                resp = self.request(
                    "PATCH", location, scope=scope, data=stream, replayable=False, expect=None,
                    headers={"Content-Type": "application/octet-stream", "Content-Length": str(stream.len)},
                )
                if not (200 <= resp.status_code < 300):
                    err = raise_http(self.label, "PATCH", location, resp)
                    resp.close()
                    raise err
                new_loc = resp.headers.get("Location")
                if new_loc:
                    location = urljoin(resp.url, new_loc)
                resp.close()

            # Проверяем безусловно: пустой ответ источника не должен превратиться
            # в успешно «залитый» блоб.
            if stream.consumed != stream.len:
                raise MigrateError(
                    f"[{self.label}] источник отдал {stream.consumed} байт вместо {stream.len} "
                    f"для блоба {digest}"
                )
            if stream.digest != digest:
                raise MigrateError(
                    f"[{self.label}] контрольная сумма не совпала: ожидался {digest}, посчитан "
                    f"{stream.digest} ({stream.consumed} из {stream.len} байт)"
                )

            final_url = with_digest_param(location, digest)
            resp = self.request("PUT", final_url, scope=scope, data=b"", replayable=False, expect=None,
                                headers={"Content-Type": "application/octet-stream", "Content-Length": "0"})
            code = resp.status_code
            if not (200 <= code < 300):
                err = raise_http(self.label, "PUT", final_url, resp)
                resp.close()
                raise err
            resp.close()
        except Exception:
            self.cancel_upload(location, repo)
            raise

    def upload_bytes(self, repo: str, digest: str, payload: bytes) -> None:
        kind, location = self.start_upload(repo)
        if kind == "mounted" or not location:
            return
        import io

        self.upload_blob(repo, digest, SizedStream(io.BytesIO(payload), len(payload)), location, 0)

    def delete_manifest(self, repo: str, digest: str) -> bool:
        url = self.url(self.repo_name(repo), "manifests", digest)
        resp = self.request("DELETE", url, scope=f"repository:{self.repo_name(repo)}:delete",
                            expect=None, replayable=False)
        ok = 200 <= resp.status_code < 300
        resp.close()
        return ok


# ---------------------------------------------------------------------------
# Перенос
# ---------------------------------------------------------------------------


@dataclass
class Options:
    workers: int = 2
    chunk_size: int = 32 * 1024 * 1024
    force: bool = False
    dry_run: bool = False
    allow_schema1: bool = False
    use_mount: bool = True
    library_prefix: str = "preserve"  # preserve | strip | auto
    on_conflict: str = "skip"  # skip | overwrite | fail
    verify_after: bool = True
    blob_retries: int = 3
    download_foreign: bool = False
    tolerate_missing_blobs: bool = False
    feed_level_blob_cache: bool = False


@dataclass
class Stats:
    tags_total: int = 0
    copied: int = 0
    skipped: int = 0
    failed: int = 0
    manifests_pushed: int = 0
    blobs_uploaded: int = 0
    blobs_present: int = 0
    blobs_mounted: int = 0
    blobs_foreign: int = 0
    bytes_transferred: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, **kwargs: int) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, getattr(self, key) + value)

    def as_dict(self) -> Dict[str, int]:
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


class State:
    """JSONL-состояние: какие теги уже перенесены (для повторного запуска)."""

    def __init__(self, path: Optional[str], target: str = "") -> None:
        self.path = path
        self.target = target  # <url>/<feed> приёмника: состояние привязано к нему
        self.lock = threading.Lock()
        self.done: Dict[str, str] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        if rec.get("status") != "ok":
                            continue
                        if rec.get("target") and self.target and rec["target"] != self.target:
                            continue  # запись про другой приёмник
                        self.done[self._key(rec.get("repo", ""), rec.get("tag", ""),
                                            rec.get("dst_repo", ""))] = rec.get("digest", "")
                LOG.info("state-файл: %d уже перенесённых тегов", len(self.done))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("не удалось прочитать state-файл %s: %s", path, exc)

    @staticmethod
    def _key(repo: str, tag: str, dst_repo: str) -> str:
        return f"{repo}:{tag}->{dst_repo}"

    def is_done(self, repo: str, tag: str, dst_repo: str, digest: Optional[str]) -> bool:
        if not self.path or not digest:
            return False
        return self.done.get(self._key(repo, tag, dst_repo)) == digest

    def mark(self, repo: str, tag: str, dst_repo: str, digest: str, status: str = "ok") -> None:
        if not self.path:
            return
        with self.lock:
            if status == "ok":
                self.done[self._key(repo, tag, dst_repo)] = digest
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"repo": repo, "tag": tag, "dst_repo": dst_repo,
                                         "target": self.target, "digest": digest,
                                         "status": status, "ts": int(time.time())},
                                        ensure_ascii=False) + "\n")
            except Exception as exc:  # noqa: BLE001
                LOG.warning("не удалось записать state-файл: %s", exc)


class Migrator:
    def __init__(self, src: Registry, dst: Registry, opts: Options, state: State) -> None:
        self.src = src
        self.dst = dst
        self.opts = opts
        self.state = state
        self.stats = Stats()
        self.warnings: List[str] = []
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._blob_cache: Set[Tuple[str, str]] = set()   # (dst_repo | '*', digest)
        self._digest_home: Dict[str, str] = {}           # digest -> dst_repo, где он уже есть
        self._visited: Set[Tuple[str, str]] = set()      # (dst_repo, manifest digest)
        self._visited_guard = threading.Lock()
        self._chunk_size = opts.chunk_size
        self._chunk_guard = threading.Lock()
        self._serialize_uploads = False
        self._upload_gate = threading.Lock()
        self.stop = threading.Event()

    # -- служебное ---------------------------------------------------------

    def _lock(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def _cache_key(self, dst_repo: str, digest: str) -> Tuple[str, str]:
        return ("*" if self.opts.feed_level_blob_cache else dst_repo, digest)

    def _warn(self, message: str) -> None:
        LOG.warning("%s", message)
        self.warnings.append(message)

    def dst_repo_for(self, repo: str) -> str:
        if self.opts.library_prefix == "preserve":
            return repo
        if repo.lower().startswith("library/"):
            rest = repo[len("library/"):]
            if self.opts.library_prefix == "strip" or (self.opts.library_prefix == "auto" and "/" not in rest):
                return rest
        return repo

    def _shrink_chunk(self) -> Optional[int]:
        """Уменьшить размер чанка после 413. None — уменьшать больше некуда."""
        with self._chunk_guard:
            current = self._chunk_size or (64 * 1024 * 1024)
            if current <= MIN_CHUNK:
                return None
            self._chunk_size = max(MIN_CHUNK, current // 2)
            return self._chunk_size

    # -- блобы -------------------------------------------------------------

    def copy_blob(self, src_repo: str, dst_repo: str, descriptor: Dict[str, Any]) -> str:
        digest = descriptor.get("digest") or ""
        if not digest:
            raise MigrateError("в дескрипторе нет digest")
        media_type = descriptor.get("mediaType") or ""
        size = int(descriptor.get("size") or 0)
        urls = descriptor.get("urls") or []

        # 1. Явно нераспространяемые слои (типично для Windows-образов) не трогаем.
        if media_type in NON_DISTRIBUTABLE:
            self.stats.add(blobs_foreign=1)
            return "foreign"

        cache_key = self._cache_key(dst_repo, digest)
        if cache_key in self._blob_cache:
            self.stats.add(blobs_present=1)
            return "cached"

        with self._lock(f"blob|{cache_key[0]}|{digest}"):
            if cache_key in self._blob_cache:
                self.stats.add(blobs_present=1)
                return "cached"

            # 2. Дескриптор с urls[] может не храниться в реестре — проверяем источник.
            if urls and not self.opts.download_foreign:
                if self.src.blob_exists(src_repo, digest) is None:
                    self.stats.add(blobs_foreign=1)
                    return "foreign"

            if self.dst.blob_exists(dst_repo, digest) is not None:
                self._blob_cache.add(cache_key)
                self._digest_home.setdefault(digest, dst_repo)
                self.stats.add(blobs_present=1)
                return "exists"

            if self.opts.dry_run:
                LOG.info("      [dry-run] блоб %s (%s)", digest[:19], human_size(size))
                self._blob_cache.add(cache_key)
                return "dry-run"

            location: Optional[str] = None
            home = self._digest_home.get(digest)
            if self.opts.use_mount and home and home != dst_repo:
                try:
                    kind, location = self.dst.start_upload(dst_repo, mount=digest,
                                                           mount_from=self.dst.repo_name(home))
                    if kind == "mounted":
                        self._blob_cache.add(cache_key)
                        self.stats.add(blobs_mounted=1)
                        return "mounted"
                except MigrateError as exc:
                    LOG.debug("      mount не удался (%s)", exc)
                    location = None

            last: Optional[Exception] = None
            attempt = 0
            budget = self.opts.blob_retries
            while attempt < budget:
                attempt += 1
                try:
                    self._transfer_blob(src_repo, dst_repo, digest, size, location)
                    location = None
                    if self.dst.blob_exists(dst_repo, digest) is None:
                        raise MigrateError("после заливки блоб не виден на приёмнике")
                    self._blob_cache.add(cache_key)
                    self._digest_home.setdefault(digest, dst_repo)
                    self.stats.add(blobs_uploaded=1, bytes_transferred=size)
                    return "uploaded"
                except HttpError as exc:
                    location = None
                    last = exc
                    if exc.status == 413:
                        new_chunk = self._shrink_chunk()
                        if new_chunk:
                            self._warn(f"приёмник отверг тело запроса (HTTP 413) — уменьшаю размер чанка "
                                       f"до {human_size(new_chunk)}")
                            budget += 1  # смена размера чанка не тратит бюджет попыток
                            continue
                        self._warn("приёмник отверг тело запроса (HTTP 413) даже на минимальном чанке — "
                                   "поднимите лимит размера запроса на reverse proxy (nginx "
                                   "client_max_body_size / IIS maxAllowedContentLength)")
                    if exc.status in (500, 502, 503, 504) and not self._serialize_uploads:
                        self._serialize_uploads = True
                        self._warn("приёмник вернул 5xx при заливке блоба — дальше заливаю блобы строго "
                                   "по одному (известная гонка в ProGet при параллельном завершении блобов)")
                    if attempt >= budget:
                        break
                    time.sleep(backoff_delay(attempt))
                except (MigrateError, TransportError) as exc:
                    location = None
                    last = exc
                    if attempt >= budget:
                        break
                    LOG.warning("      блоб %s: попытка %d/%d — %s", digest[:19], attempt,
                                budget, exc)
                    time.sleep(backoff_delay(attempt))
            raise MigrateError(f"не удалось перенести блоб {digest} ({human_size(size)}): {last}")

    def _transfer_blob(self, src_repo: str, dst_repo: str, digest: str, size: int,
                       location: Optional[str]) -> None:
        gate = self._upload_gate if self._serialize_uploads else None
        if gate:
            gate.acquire()
        try:
            resp = self.src.open_blob(src_repo, digest)
            try:
                length = size
                header_len = resp.headers.get("Content-Length")
                if header_len and header_len.isdigit():
                    length = int(header_len)
                    if size and length != size:
                        self._warn(f"размер блоба {digest[:19]} по Content-Length ({length}) не совпал "
                                   f"с манифестом ({size}) — доверяю ответу, digest проверю после заливки")
                if not length and size:
                    length = size
                if not length:
                    raise MigrateError(f"источник не сообщил размер блоба {digest}")
                if location is None:
                    kind, location = self.dst.start_upload(dst_repo)
                    if kind == "mounted":
                        return
                LOG.debug("      блоб %s (%s)", digest[:19], human_size(length))
                stream = SizedStream(RawReader(resp.raw), length)
                self.dst.upload_blob(dst_repo, digest, stream, location, self._chunk_size)
            finally:
                resp.close()
        finally:
            if gate:
                gate.release()

    # -- манифесты ---------------------------------------------------------

    def _fetch_manifest(self, repo: str, reference: str) -> Tuple[bytes, str, str, bool]:
        """Возвращает (raw, media_type, digest, is_schema1)."""
        body, ctype, header_digest = self.src.get_manifest(repo, reference)
        if ctype in SCHEMA1_TYPES or ctype in ("application/json", ""):
            # Старый реестр мог отдать schema1 вместо schema2 — перепроверяем узким Accept.
            try:
                body2, ctype2, digest2 = self.src.get_manifest(repo, reference, ACCEPT_MODERN)
                if ctype2 in ALL_MANIFEST_TYPES and ctype2 not in SCHEMA1_TYPES:
                    body, ctype, header_digest = body2, ctype2, digest2
            except HttpError:
                pass

        media_type = ctype
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        body_type = parsed.get("mediaType")
        if body_type:
            media_type = str(body_type).split(";")[0].strip()
        elif media_type in MANIFEST_TYPES | INDEX_TYPES:
            self._warn(f"{repo}:{reference} — в теле манифеста нет поля mediaType; ProGet хранит манифест "
                       f"как есть, и pull такого образа может не работать. Тип берётся из заголовка ответа.")

        # schema1 узнаётся по структуре: старый ProGet мог отдать его как application/json
        is_schema1 = media_type in SCHEMA1_TYPES or "fsLayers" in parsed or parsed.get("schemaVersion") == 1

        if not is_schema1 and media_type not in ALL_MANIFEST_TYPES:
            # Content-Type бесполезен (application/json и т.п.), mediaType в теле нет —
            # определяем тип по структуре документа.
            if isinstance(parsed.get("manifests"), list):
                guessed = MT_OCI_INDEX if parsed.get("schemaVersion") == 2 and any(
                    str(c.get("mediaType", "")).startswith("application/vnd.oci.")
                    for c in parsed["manifests"]) else MT_DOCKER_LIST
            elif isinstance(parsed.get("layers"), list) and parsed.get("config"):
                guessed = MT_OCI_MANIFEST if str(parsed["config"].get("mediaType", "")).startswith(
                    "application/vnd.oci.") else MT_DOCKER_MANIFEST
            else:
                guessed = ""
            if guessed:
                self._warn(f"{repo}:{reference} — сервер вернул тип {media_type or 'пусто'!r}; "
                           f"по структуре это {guessed}, использую его")
                media_type = guessed
        computed = "sha256:" + hashlib.sha256(body).hexdigest()
        digest = header_digest or computed
        if header_digest and header_digest != computed and not is_schema1:
            raise MigrateError(
                f"{repo}:{reference} — digest манифеста не совпал с содержимым "
                f"(заголовок {header_digest}, посчитано {computed})"
            )
        return body, media_type, digest, is_schema1

    def copy_manifest(self, src_repo: str, dst_repo: str, reference: str,
                      push_ref: str, depth: int = 0) -> str:
        pad = "  " * (depth + 1)
        body, media_type, digest, is_schema1 = self._fetch_manifest(src_repo, reference)

        if is_schema1:
            if not self.opts.allow_schema1:
                raise MigrateError(
                    f"{src_repo}:{reference} — устаревший манифест schema1 ({media_type or 'без mediaType'}). "
                    f"Современный ProGet его почти наверняка не примет; конвертация меняет digest. "
                    f"Запустите с --allow-schema1, чтобы попробовать залить как есть, либо перенесите этот "
                    f"образ вручную через docker pull/push."
                )
            for layer in (json.loads(body).get("fsLayers") or []):
                if layer.get("blobSum"):
                    self.copy_blob(src_repo, dst_repo, {"digest": layer["blobSum"], "mediaType": "", "size": 0})

        elif media_type in INDEX_TYPES:
            children = (json.loads(body).get("manifests") or [])
            LOG.info("%sindex %s: %d вложенных манифестов", pad, digest[:19], len(children))
            for child in children:
                child_digest = child.get("digest")
                if not child_digest:
                    continue
                child_type = (child.get("mediaType") or "").split(";")[0].strip()
                if child_type and child_type not in ALL_MANIFEST_TYPES:
                    # редкий случай: дескриптор артефакта, а не манифеста — это просто блоб
                    self.copy_blob(src_repo, dst_repo, child)
                    continue
                platform = child.get("platform") or {}
                plat = "/".join(x for x in (platform.get("os"), platform.get("architecture"),
                                            platform.get("variant")) if x) or child_type
                LOG.info("%s└ %s (%s)", pad, child_digest[:19], plat)
                self.copy_manifest(src_repo, dst_repo, child_digest, child_digest, depth + 1)

        elif media_type in MANIFEST_TYPES:
            manifest = json.loads(body)
            config = manifest.get("config") or {}
            if config.get("digest"):
                self.copy_blob(src_repo, dst_repo, config)
            for layer in (manifest.get("layers") or []):
                if layer.get("digest"):
                    self.copy_blob(src_repo, dst_repo, layer)
            subject = manifest.get("subject") or {}
            if subject.get("digest"):
                LOG.debug("%sманифест ссылается на subject %s (referrers)", pad, subject["digest"][:19])
        else:
            raise MigrateError(f"{src_repo}:{reference} — неизвестный тип манифеста {media_type!r}")

        if self.opts.dry_run:
            LOG.info("%s[dry-run] PUT %s -> %s:%s", pad, media_type, dst_repo, push_ref)
            return digest

        key = (dst_repo, digest)
        if push_ref.startswith("sha256:"):
            with self._visited_guard:
                if key in self._visited:
                    return digest
            if self.dst.head_manifest(dst_repo, push_ref) is not None:
                with self._visited_guard:
                    self._visited.add(key)
                return digest

        pushed = self.dst.put_manifest(dst_repo, push_ref, body, media_type or MT_DOCKER_MANIFEST)
        self.stats.add(manifests_pushed=1)
        with self._visited_guard:
            self._visited.add(key)
        if pushed and not is_schema1 and pushed != digest:
            self._warn(f"{dst_repo}:{push_ref} — приёмник вернул другой digest ({pushed} вместо {digest}); "
                       f"манифест был переупакован, ссылки по digest сломаются")
        return digest

    # -- уровень тега ------------------------------------------------------

    def copy_tag(self, src_repo: str, tag: str) -> Dict[str, Any]:
        dst_repo = self.dst_repo_for(src_repo)
        row: Dict[str, Any] = {"repo": src_repo, "tag": tag, "dst_repo": dst_repo}
        try:
            if not TAG_RE.match(tag):
                row["status"] = "skipped-invalid-name"
                row["error"] = "тег не соответствует правилам docker"
                self.stats.add(skipped=1)
                return row
            if not REPO_NAME_RE.match(dst_repo):
                row["status"] = "skipped-invalid-name"
                row["error"] = f"недопустимое имя репозитория на приёмнике: {dst_repo}"
                self.stats.add(skipped=1)
                return row

            src_digest = self.src.head_manifest(src_repo, tag)
            if not src_digest:
                _, _, src_digest, _ = self._fetch_manifest(src_repo, tag)
            row["digest"] = src_digest

            if not self.opts.force and self.state.is_done(src_repo, tag, dst_repo, src_digest):
                row["status"] = "skipped-state"
                self.stats.add(skipped=1)
                LOG.info("= %s:%s (уже перенесён, по state-файлу)", src_repo, tag)
                return row

            dst_digest = self.dst.head_manifest(dst_repo, tag)
            if dst_digest == "":
                # приёмник не вернул Docker-Content-Digest — сравнить нечем, просто копируем
                LOG.debug("%s:%s — приёмник не сообщил digest на HEAD, копирую поверх", dst_repo, tag)
                dst_digest = None
            if dst_digest is not None and not self.opts.force:
                if src_digest and dst_digest == src_digest:
                    row["status"] = "skipped-exists"
                    self.stats.add(skipped=1)
                    self.state.mark(src_repo, tag, dst_repo, src_digest or "")
                    LOG.info("= %s:%s (уже на приёмнике, digest совпадает)", src_repo, tag)
                    return row
                if self.opts.on_conflict == "skip":
                    row["status"] = "skipped-conflict"
                    row["dst_digest"] = dst_digest
                    row["error"] = "на приёмнике тег занят другим образом (--on-conflict overwrite перезапишет)"
                    self.stats.add(skipped=1)
                    self._warn(f"{dst_repo}:{tag} уже занят другим образом на приёмнике — пропущен")
                    return row
                if self.opts.on_conflict == "fail":
                    raise MigrateError(f"тег {dst_repo}:{tag} занят другим образом ({dst_digest})")
                LOG.warning("~ %s:%s будет перезаписан (было %s)", dst_repo, tag, (dst_digest or "?")[:19])

            LOG.info("→ %s:%s (%s)", src_repo, tag, (src_digest or "?")[:19])
            self.copy_manifest(src_repo, dst_repo, tag, tag)

            if self.opts.verify_after and not self.opts.dry_run:
                check = self.dst.head_manifest(dst_repo, tag)
                if check is None:
                    raise MigrateError(f"после заливки тег {dst_repo}:{tag} не читается на приёмнике")
                row["dst_digest"] = check
                if src_digest and check and check != src_digest:
                    self._warn(f"{dst_repo}:{tag} — digest на приёмнике {check} != {src_digest}")

            row["status"] = "dry-run" if self.opts.dry_run else "copied"
            self.stats.add(copied=1)
            if not self.opts.dry_run:
                self.state.mark(src_repo, tag, dst_repo, src_digest or "")
            LOG.info("✓ %s:%s -> %s:%s", src_repo, tag, dst_repo, tag)
            return row
        except Exception as exc:  # noqa: BLE001 — один сбойный тег не должен валить прогон
            self.stats.add(failed=1)
            row["status"] = "failed"
            row["error"] = str(exc)
            self.state.mark(src_repo, tag, dst_repo, row.get("digest") or "", status="failed")
            LOG.error("✗ %s:%s — %s", src_repo, tag, exc)
            return row

    def compare_tag(self, src_repo: str, tag: str) -> Dict[str, Any]:
        dst_repo = self.dst_repo_for(src_repo)
        row: Dict[str, Any] = {"repo": src_repo, "tag": tag, "dst_repo": dst_repo}
        try:
            src_digest = self.src.head_manifest(src_repo, tag)
            if not src_digest:
                _, _, src_digest, _ = self._fetch_manifest(src_repo, tag)
            dst_digest = self.dst.head_manifest(dst_repo, tag)
            row["digest"] = src_digest
            row["dst_digest"] = dst_digest
            if dst_digest is None:
                row["status"] = "missing"
                self.stats.add(failed=1)
            elif src_digest and dst_digest and dst_digest != src_digest:
                row["status"] = "different"
                self.stats.add(failed=1)
            else:
                row["status"] = "ok"
                self.stats.add(copied=1)
        except Exception as exc:  # noqa: BLE001
            row["status"] = "failed"
            row["error"] = str(exc)
            self.stats.add(failed=1)
        return row

    def run(self, work: List[Tuple[str, str]], action: Callable[[str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.stats.tags_total = len(work)
        results: List[Dict[str, Any]] = []
        workers = max(1, self.opts.workers)

        def guarded(repo: str, tag: str) -> Dict[str, Any]:
            if self.stop.is_set():
                return {"repo": repo, "tag": tag, "status": "skipped-interrupted"}
            return action(repo, tag)

        if workers == 1:
            try:
                for repo, tag in work:
                    results.append(guarded(repo, tag))
            except KeyboardInterrupt:
                self.stop.set()
                LOG.warning("прервано пользователем — остановился на %d из %d тегов",
                            len(results), len(work))
            return results

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="copy")
        futures = {pool.submit(guarded, repo, tag): (repo, tag) for repo, tag in work}
        try:
            for future in as_completed(futures):
                repo, tag = futures[future]
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    self.stats.add(failed=1)
                    results.append({"repo": repo, "tag": tag, "status": "failed", "error": str(exc)})
                    LOG.error("✗ %s:%s — %s", repo, tag, exc)
        except KeyboardInterrupt:
            self.stop.set()
            LOG.warning("прервано пользователем — отменяю очередь, дожидаюсь текущих заливок…")
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        finally:
            pool.shutdown(wait=True)
        return results

    # -- проверка возможностей приёмника ----------------------------------

    def preflight_destination(self) -> None:
        """Мини-канарейка: заливаем 2-байтовый блоб и проверяем mount/дедупликацию."""
        if self.opts.dry_run:
            return
        probe_a, probe_b = PROBE_REPO, PROBE_REPO + "2"
        last: Optional[Exception] = None
        for attempt in range(1, 4):
            try:
                if self.dst.blob_exists(probe_a, EMPTY_JSON_DIGEST) is None:
                    self.dst.upload_bytes(probe_a, EMPTY_JSON_DIGEST, EMPTY_JSON_BLOB)
                LOG.info("[dst] проба заливки блоба пройдена (POST/PATCH/PUT работают)")
                last = None
                break
            except MigrateError as exc:
                last = exc
                LOG.warning("[dst] проба записи, попытка %d/3: %s", attempt, exc)
                time.sleep(backoff_delay(attempt))
        if last is not None:
            raise MigrateError(
                f"проба записи на приёмник не прошла: {last}\n"
                f"Проверьте права ключа (Publish Packages), лимиты размера тела запроса на "
                f"reverse proxy и что фид '{self.dst.feed}' — именно Docker-фид. "
                f"Пропустить пробу можно флагом --no-preflight."
            ) from last
        # Видны ли блобы одного фида из другого репозитория БЕЗ mount? Проверяем это
        # первым: если да, можно не делать лишних HEAD-ов. Проба обязана идти до
        # mount-пробы, иначе mount сам создаст блоб в probe_b и результат будет ложным.
        try:
            if self.dst.blob_exists(probe_b, EMPTY_JSON_DIGEST) is not None:
                self.opts.feed_level_blob_cache = True
                LOG.info("[dst] блобы видны в пределах всего фида — включён общий кэш digest'ов")
        except MigrateError:
            pass

        if not self.opts.use_mount:
            LOG.info("[dst] cross-repo mount отключён (--no-mount)")
            return
        try:
            kind, location = self.dst.start_upload(probe_b, mount=EMPTY_JSON_DIGEST,
                                                   mount_from=self.dst.repo_name(probe_a))
            if kind == "mounted":
                LOG.info("[dst] cross-repo mount поддерживается")
            else:
                self.opts.use_mount = False
                if location:
                    self.dst.cancel_upload(location, probe_b)
                LOG.info("[dst] cross-repo mount не поддерживается — блобы будут заливаться напрямую")
        except MigrateError:
            self.opts.use_mount = False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Перенос Docker-образов из фида ProGet 2022.x (Windows) в фид ProGet последней версии (Linux).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    src = p.add_argument_group("источник")
    src.add_argument("--src-url", default=os.environ.get("PROGET_SRC_URL"),
                     help="корневой URL старого ProGet, напр. https://proget-old:8624 (без /v2)")
    src.add_argument("--src-feed", default=os.environ.get("PROGET_SRC_FEED"), help="имя docker-фида источника")
    src.add_argument("--src-user", default=os.environ.get("PROGET_SRC_USER"))
    src.add_argument("--src-password", default=os.environ.get("PROGET_SRC_PASSWORD"))
    src.add_argument("--src-api-key", default=os.environ.get("PROGET_SRC_API_KEY"),
                     help="API-ключ ProGet (используется как пароль пользователя 'api')")
    src.add_argument("--src-ca", help="CA-бандл для проверки TLS источника")

    dst = p.add_argument_group("приёмник")
    dst.add_argument("--dst-url", default=os.environ.get("PROGET_DST_URL"))
    dst.add_argument("--dst-feed", default=os.environ.get("PROGET_DST_FEED"),
                     help="имя docker-фида приёмника (по умолчанию совпадает с --src-feed)")
    dst.add_argument("--dst-user", default=os.environ.get("PROGET_DST_USER"))
    dst.add_argument("--dst-password", default=os.environ.get("PROGET_DST_PASSWORD"))
    dst.add_argument("--dst-api-key", default=os.environ.get("PROGET_DST_API_KEY"))
    dst.add_argument("--dst-ca", help="CA-бандл для проверки TLS приёмника")

    sel = p.add_argument_group("выборка")
    sel.add_argument("--repo", action="append", default=[], metavar="GLOB",
                     help="только эти репозитории (можно повторять; поддерживает * и ?)")
    sel.add_argument("--exclude-repo", action="append", default=[], metavar="GLOB")
    sel.add_argument("--repos-file", help="файл со списком репозиториев, по одному в строке")
    sel.add_argument("--tag", action="append", default=[], metavar="GLOB", help="только эти теги")
    sel.add_argument("--exclude-tag", action="append", default=[], metavar="GLOB")
    sel.add_argument("--limit", type=int, default=0, help="максимум тегов всего (0 — без ограничения)")
    sel.add_argument("--max-tags-per-repo", type=int, default=0)

    run = p.add_argument_group("выполнение")
    run.add_argument("--workers", type=int, default=2,
                     help="сколько тегов копировать параллельно (ProGet не любит высокий параллелизм)")
    run.add_argument("--chunk-size", type=int, default=32, metavar="MB",
                     help="размер чанка при заливке блоба; 0 — одним потоком, как docker push")
    run.add_argument("--retries", type=int, default=5, help="ретраи HTTP-запросов")
    run.add_argument("--blob-retries", type=int, default=3, help="ретраи целиком одного блоба")
    run.add_argument("--connect-timeout", type=float, default=30.0)
    run.add_argument("--read-timeout", type=float, default=900.0)
    run.add_argument("--library-prefix", choices=("preserve", "strip", "auto"), default="preserve",
                     help="что делать с префиксом library/, который ProGet<=2022 добавлял автоматически: "
                          "preserve — как есть; strip — снять; auto — снять только у однокомпонентных имён")
    run.add_argument("--on-conflict", choices=("skip", "overwrite", "fail"), default="skip",
                     help="что делать, если на приёмнике тег занят другим образом")
    run.add_argument("--force", action="store_true", help="игнорировать state-файл и совпадение digest")
    run.add_argument("--allow-schema1", action="store_true", help="пытаться переносить манифесты schema1")
    run.add_argument("--download-foreign", action="store_true",
                     help="переносить foreign/nondistributable слои, если источник их всё же отдаёт")
    run.add_argument("--auth-mode", choices=("auto", "bearer", "basic"), default="auto")
    run.add_argument("--no-mount", action="store_true", help="не использовать cross-repo mount")
    run.add_argument("--no-verify", action="store_true", help="не проверять тег на приёмнике после заливки")
    run.add_argument("--no-preflight", action="store_true", help="пропустить пробу записи на приёмник")
    run.add_argument("--insecure", action="store_true", help="не проверять TLS-сертификаты")
    run.add_argument("--dry-run", action="store_true", help="ничего не менять на приёмнике")
    run.add_argument("--list-only", action="store_true", help="только показать список того, что будет скопировано")
    run.add_argument("--compare", action="store_true",
                     help="ничего не копировать: сверить теги источника и приёмника по digest")

    out = p.add_argument_group("вывод")
    out.add_argument("--state-file", help="JSONL-файл состояния для докачки при повторном запуске")
    out.add_argument("--report", help="куда сохранить JSON-отчёт")
    out.add_argument("--log-file")
    out.add_argument("-v", "--verbose", action="store_true")
    out.add_argument("-q", "--quiet", action="store_true")
    return p


def setup_logging(args: argparse.Namespace) -> None:
    level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", handlers=handlers)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def credentials(user: Optional[str], password: Optional[str],
                api_key: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    if api_key:
        return (user or "api"), api_key
    return user, password


def build_worklist(src: Registry, args: argparse.Namespace) -> List[Tuple[str, str]]:
    if args.repos_file:
        with open(args.repos_file, "r", encoding="utf-8") as fh:
            repos = [src.strip_feed(line.strip()) for line in fh
                     if line.strip() and not line.lstrip().startswith("#")]
    elif args.repo and not any(ch in pattern for pattern in args.repo for ch in "*?["):
        repos = [src.strip_feed(r) for r in args.repo]
    else:
        repos = src.list_repositories()
        if args.repo:
            repos = [r for r in repos if matches_any(r, args.repo)]
    if args.exclude_repo:
        repos = [r for r in repos if not matches_any(r, args.exclude_repo)]

    work: List[Tuple[str, str]] = []
    for repo in repos:
        tags = src.list_tags(repo)
        if not tags:
            LOG.info("- %s: тегов нет", repo)
            continue
        if args.tag:
            tags = [t for t in tags if matches_any(t, args.tag)]
        if args.exclude_tag:
            tags = [t for t in tags if not matches_any(t, args.exclude_tag)]
        if args.max_tags_per_repo:
            tags = tags[:args.max_tags_per_repo]
        if not tags:
            LOG.info("- %s: подходящих тегов нет", repo)
            continue
        LOG.info("- %s: %d тегов", repo, len(tags))
        work.extend((repo, tag) for tag in tags)
        if args.limit and len(work) >= args.limit:
            break
    return work[:args.limit] if args.limit else work


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args)

    missing = [n for n in ("src_url", "src_feed", "dst_url") if not getattr(args, n)]
    if missing:
        parser.error("не заданы обязательные параметры: " + ", ".join("--" + m.replace("_", "-") for m in missing))
    dst_feed = args.dst_feed or args.src_feed

    src_user, src_pass = credentials(args.src_user, args.src_password, args.src_api_key)
    dst_user, dst_pass = credentials(args.dst_user, args.dst_password, args.dst_api_key)
    if args.insecure:
        LOG.warning("проверка TLS-сертификатов отключена (--insecure)")

    pool = max(8, args.workers * 4)
    src = Registry("src", args.src_url, args.src_feed, src_user, src_pass,
                   verify=False if args.insecure else (args.src_ca or True),
                   connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
                   retries=args.retries, pool_size=pool, auth_mode=args.auth_mode)
    dst = Registry("dst", args.dst_url, dst_feed, dst_user, dst_pass,
                   verify=False if args.insecure else (args.dst_ca or True),
                   connect_timeout=args.connect_timeout, read_timeout=args.read_timeout,
                   retries=args.retries, pool_size=pool, auth_mode=args.auth_mode)

    started = time.time()
    try:
        for reg in (src, dst):
            reg.detect_version()
            reg.setup_auth()
            reg.check_feed()
            LOG.info("[%s] %s | ProGet %s | фид '%s' | пользователь: %s",
                     reg.label, reg.base, reg.version, reg.feed, reg.identity)
    except MigrateError as exc:
        LOG.error("%s", exc)
        return 2

    opts = Options(
        workers=max(1, args.workers),
        chunk_size=max(0, args.chunk_size) * 1024 * 1024,
        force=args.force,
        dry_run=args.dry_run,
        allow_schema1=args.allow_schema1,
        use_mount=not args.no_mount,
        library_prefix=args.library_prefix,
        on_conflict=args.on_conflict,
        verify_after=not args.no_verify,
        blob_retries=max(1, args.blob_retries),
        download_foreign=args.download_foreign,
    )
    state = State(args.state_file, target=f"{dst.base}/{dst.feed}")
    migrator = Migrator(src, dst, opts, state)

    try:
        work = build_worklist(src, args)
    except MigrateError as exc:
        LOG.error("%s", exc)
        return 2

    library_repos = sorted({r for r, _ in work if r.lower().startswith("library/")})
    if library_repos:
        mapped = {r: migrator.dst_repo_for(r) for r in library_repos}
        changed = {k: v for k, v in mapped.items() if k != v}
        LOG.warning("-" * 70)
        LOG.warning("Найдено %d репозиториев с префиксом 'library/' — ProGet <=2022 добавлял его "
                    "автоматически, ProGet 2023+ этого уже не делает.", len(library_repos))
        if changed:
            for k, v in sorted(changed.items())[:20]:
                LOG.warning("  %s -> %s", k, v)
        else:
            LOG.warning("  Сейчас имена переносятся как есть (--library-prefix preserve), значит тянуть их "
                        "нужно будет как '<host>/%s/library/<имя>'. Если хотите короткие имена — "
                        "перезапустите с --library-prefix strip.", dst_feed)
        LOG.warning("-" * 70)

    LOG.info("к обработке: %d тегов в %d репозиториях",
             len(work), len({r for r, _ in work}))
    if args.list_only:
        for repo, tag in work:
            print(f"{repo}:{tag} -> {migrator.dst_repo_for(repo)}:{tag}")
        return 0
    if not work:
        LOG.warning("нечего переносить (если это неожиданно — проверьте права ключа: неверный ключ на "
                    "ProGet выглядит как пустой фид, а не как ошибка)")
        return 0

    mode = "compare" if args.compare else "copy"
    if mode == "copy" and not args.no_preflight:
        try:
            migrator.preflight_destination()
        except MigrateError as exc:
            LOG.error("%s", exc)
            return 2

    action = migrator.compare_tag if mode == "compare" else migrator.copy_tag
    results = migrator.run(work, action)
    elapsed = time.time() - started
    stats = migrator.stats

    by_status: Dict[str, int] = {}
    for row in results:
        by_status[row.get("status", "?")] = by_status.get(row.get("status", "?"), 0) + 1

    LOG.info("-" * 70)
    if mode == "compare":
        bad = [r for r in results if r.get("status") != "ok"]
        LOG.info("сверка за %.0f c: совпало %d из %d", elapsed, len(results) - len(bad), len(results))
        for row in sorted(bad, key=lambda r: (r["repo"], r["tag"])):
            LOG.warning("  %s:%s — %s %s", row["repo"], row["tag"], row["status"], row.get("error", ""))
    else:
        LOG.info("готово за %.0f c: %s", elapsed,
                 ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
        LOG.info("манифестов залито %d; блобы: залито %d (%s), уже было %d, mount %d, foreign пропущено %d",
                 stats.manifests_pushed, stats.blobs_uploaded, human_size(stats.bytes_transferred),
                 stats.blobs_present, stats.blobs_mounted, stats.blobs_foreign)
    failures = [r for r in results if r.get("status") in ("failed", "missing", "different")]
    if failures:
        LOG.error("проблемные теги (%d):", len(failures))
        for row in failures[:50]:
            LOG.error("  %s:%s — %s", row.get("repo"), row.get("tag"),
                      row.get("error") or row.get("status"))
        if len(failures) > 50:
            LOG.error("  ... ещё %d, полный список в отчёте", len(failures) - 50)

    if migrator.warnings:
        LOG.warning("предупреждения (%d):", len(migrator.warnings))
        for text in dict.fromkeys(migrator.warnings):
            LOG.warning("  %s", text)
    LOG.info("Напоминание: untagged/dangling образы через registry API не видны и не переносятся. "
             "Сверьте число репозиториев с web UI источника.")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({
                "mode": mode,
                "source": {"url": src.base, "feed": src.feed, "version": src.version, "user": src.identity},
                "destination": {"url": dst.base, "feed": dst.feed, "version": dst.version, "user": dst.identity},
                "elapsed_seconds": round(elapsed, 1),
                "stats": stats.as_dict(),
                "by_status": by_status,
                "warnings": list(dict.fromkeys(migrator.warnings)),
                "results": sorted(results, key=lambda r: (r.get("repo", ""), r.get("tag", ""))),
            }, fh, ensure_ascii=False, indent=2)
        LOG.info("отчёт: %s", args.report)

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nпрервано пользователем\n")
        raise SystemExit(130)
