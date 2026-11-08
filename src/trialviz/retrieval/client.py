"""An HTTP client for the ClinicalTrials.gov API that is polite, patient, and repeatable.

- **Polite**: requests are spaced to a configured rate. The API starts answering 429 after
  roughly ten requests in quick succession (observed while building this), so the
  spacing is enforced in the client rather than left to luck.
- **Patient**: rate limiting, server errors and dropped connections are retried with
  exponential backoff. A 400 is not retried. It means the query itself is wrong, so it
  fails immediately with the API's own explanation.
- **Repeatable**: responses are cached on disk under a key that includes the registry's
  data version (`/version` reports a `dataTimestamp` that changes with each daily
  refresh). A cached answer is therefore never stale. When the registry refreshes, the
  key changes and the cache simply misses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from trialviz import progress
from trialviz.retrieval.translate import RegistryQuery

API_ROOT = "https://clinicaltrials.gov/api/v2"
MAX_PAGE_SIZE = 1000  # the API coerces anything larger down to this

Study = Mapping[str, Any]
Sleep = Callable[[float], Awaitable[None]]


class RegistryError(Exception):
    """Base class for failures talking to ClinicalTrials.gov."""


class RegistryQueryError(RegistryError):
    """The API rejected the query (HTTP 400). Retrying would not help."""


class RegistryUnavailable(RegistryError):
    """The API could not be reached, or kept failing after every retry."""


class RateLimiter:
    """Spaces requests at least `60 / requests_per_minute` seconds apart."""

    def __init__(
        self,
        requests_per_minute: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._interval = 60.0 / requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            delay = self._next_slot - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_slot = max(self._clock(), self._next_slot) + self._interval


class ResponseCache:
    """JSON responses on disk, one file per request, written atomically, in one folder per
    registry data version. When the version changes, older folders are removed, so the
    cache never serves or keeps data from a superseded version."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @staticmethod
    def key(path: str, params: Mapping[str, str]) -> str:
        material = json.dumps([path, sorted(params.items())])
        return hashlib.sha256(material.encode()).hexdigest()

    def _folder(self, data_version: str) -> Path:
        return self._directory / re.sub(r"[^0-9A-Za-z]+", "-", data_version)

    def get(self, data_version: str, key: str) -> dict[str, Any] | None:
        file = self._folder(data_version) / f"{key}.json"
        if not file.exists():
            return None
        data: dict[str, Any] = json.loads(file.read_text())
        return data

    def put(self, data_version: str, key: str, data: Mapping[str, Any]) -> None:
        folder = self._folder(data_version)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / f"{key}.{os.getpid()}.tmp"
        temporary.write_text(json.dumps(data))
        os.replace(temporary, folder / f"{key}.json")

    def prune(self, data_version: str) -> None:
        """Remove every response stored under another data version."""
        if not self._directory.exists():
            return
        keep = self._folder(data_version)
        for entry in self._directory.iterdir():
            if entry == keep:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)


@dataclass(frozen=True)
class Fetched:
    studies: tuple[Study, ...]
    total: int
    """Every study matching the query, which can exceed len(studies) when a limit applied."""
    requests: tuple[str, ...]
    """The URL of every page request made, in order, for traceability."""


class RegistryClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        limiter: RateLimiter,
        *,
        cache: ResponseCache | None = None,
        api_root: str = API_ROOT,
        max_attempts: int = 4,
        backoff_seconds: float = 2.0,
        sleep: Sleep = asyncio.sleep,
        version_ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._limiter = limiter
        self._cache = cache
        self._api_root = api_root.rstrip("/")
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._version_ttl = version_ttl_seconds
        self._clock = clock
        self._data_version: str | None = None
        self._version_checked = 0.0

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        await self._http.aclose()

    async def data_version(self) -> str:
        """The registry's current `dataTimestamp`, checked again once it is older than the
        TTL, so a long-running server picks up the registry's daily refresh. When it changes,
        cached responses from the old version are removed."""
        now = self._clock()
        if self._data_version is None or now - self._version_checked >= self._version_ttl:
            body = await self._request("/version", {})
            version = str(body["dataTimestamp"])
            if version != self._data_version and self._cache is not None:
                await asyncio.to_thread(self._cache.prune, version)
            self._data_version, self._version_checked = version, now
        return self._data_version

    async def count(self, query: RegistryQuery) -> int:
        """How many studies match, without downloading them."""
        params = {**dict(query.params), "countTotal": "true", "pageSize": "1", "fields": "NCTId"}
        body = await self._get("/studies", params)
        return int(body["totalCount"])

    async def fetch(
        self,
        query: RegistryQuery,
        *,
        fields: tuple[str, ...],
        limit: int,
        sort: str | None = None,
    ) -> Fetched:
        """Follow pages until `limit` studies are collected or the results run out.

        Every page repeats the first page's parameters, as the API requires; only the
        page size and token change.
        """
        base = {**dict(query.params), "fields": ",".join(fields)}
        if sort:
            base["sort"] = sort
        studies: list[Study] = []
        requests: list[str] = []
        total = 0
        token: str | None = None
        while len(studies) < limit:
            params = {**base, "pageSize": str(min(MAX_PAGE_SIZE, limit - len(studies)))}
            if token:
                params["pageToken"] = token
            else:
                params["countTotal"] = "true"
            body = await self._get("/studies", params)
            requests.append(f"{self._api_root}/studies?{httpx.QueryParams(params)}")
            page = body.get("studies", [])
            if token is None:
                total = int(body.get("totalCount", 0))
                progress.records(len(page), min(limit, total))
            else:
                progress.records(len(page))
            studies.extend(page)
            token = body.get("nextPageToken")
            if not token:
                break
        return Fetched(tuple(studies[:limit]), total, tuple(requests))

    async def _get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        progress.registry_request()
        if self._cache is None:
            return await self._request(path, params)
        version = await self.data_version()
        key = ResponseCache.key(path, params)
        # File reads and writes run in a thread so they never stall the event loop.
        if (cached := await asyncio.to_thread(self._cache.get, version, key)) is not None:
            return cached
        body = await self._request(path, params)
        await asyncio.to_thread(self._cache.put, version, key, body)
        return body

    async def _request(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = f"{self._api_root}{path}"
        last_problem = "no attempt made"
        for attempt in range(self._max_attempts):
            if attempt:
                await self._sleep(self._backoff * 2 ** (attempt - 1))
            await self._limiter.wait()
            try:
                response = await self._http.get(url, params=params)
            except httpx.TransportError as error:
                last_problem = f"{type(error).__name__}: {error}"
                continue
            if response.status_code == 200:
                body: dict[str, Any] = response.json()
                return body
            if response.status_code == 400:
                raise RegistryQueryError(
                    f"ClinicalTrials.gov rejected the query: {response.text[:300]}"
                )
            last_problem = f"HTTP {response.status_code}"
            if response.status_code != 429 and response.status_code < 500:
                break
        raise RegistryUnavailable(
            f"ClinicalTrials.gov did not answer {path} after {self._max_attempts} attempts "
            f"(last: {last_problem})"
        )
