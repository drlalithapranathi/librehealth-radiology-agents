"""Thin Orthanc REST client. Used for imaging metadata (lean-reference). Stubs for M0."""
from __future__ import annotations
from typing import Any, Optional
import os
import httpx


class OrthancClient:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 15.0):
        self.base_url = (base_url or os.environ.get("ORTHANC_BASE_URL", "http://orthanc:8042")).rstrip("/")
        self._timeout = timeout

    async def _get(self, path: str) -> Any:
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(f"{self.base_url}/{path.lstrip('/')}")
            r.raise_for_status()
            return r.json()

    async def get_study(self, orthanc_study_id: str) -> dict:
        raise NotImplementedError("TODO(M1): GET /studies/{id}")

    async def list_completed_studies(self) -> list[dict]:
        """Used by the Worklist API to build the reading worklist."""
        raise NotImplementedError("TODO(M1): GET /studies (+ filter)")
