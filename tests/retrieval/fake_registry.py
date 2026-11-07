"""An in-process stand-in for the ClinicalTrials.gov API, served through httpx.MockTransport.

It pages like the real API (opaque tokens, a total only when asked for), and it can be
told to fail with a scripted sequence of status codes. Every request it receives is kept,
so tests can check exactly what the client sent.
"""

from __future__ import annotations

from typing import Any

import httpx


class FakeRegistry:
    def __init__(
        self,
        study_count: int = 0,
        *,
        data_version: str = "2026-11-06T10:00:05",
        failures: list[int | type[Exception]] | None = None,
        totals: dict[str, int] | None = None,
    ) -> None:
        self.studies = [
            {"protocolSection": {"identificationModule": {"nctId": f"NCT{index:08d}"}}}
            for index in range(study_count)
        ]
        self.data_version = data_version
        self.failures = list(failures or [])
        self.totals = totals or {}
        """Per-query totals keyed by the query.intr value, to vary results by cohort."""
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport())

    def study_requests(self) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path.endswith("/studies")]

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, int):
                return httpx.Response(failure, text=f"scripted {failure}")
            raise failure("scripted transport failure")
        if request.url.path.endswith("/version"):
            return httpx.Response(
                200, json={"apiVersion": "2.0.5", "dataTimestamp": self.data_version}
            )
        return httpx.Response(200, json=self._page(dict(request.url.params)))

    def _page(self, params: dict[str, str]) -> dict[str, Any]:
        studies = self.studies[: self.totals.get(params.get("query.intr", ""), len(self.studies))]
        offset = int(params.get("pageToken", "p0")[1:])
        size = int(params.get("pageSize", "10")) or 10
        page = studies[offset : offset + min(size, 1000)]
        body: dict[str, Any] = {"studies": page}
        if params.get("countTotal") == "true" and "pageToken" not in params:
            body["totalCount"] = len(studies)
        if offset + len(page) < len(studies):
            body["nextPageToken"] = f"p{offset + len(page)}"
        return body
