"""Minimal async Kubernetes client for the catcher (service account `catcher`), D74/D75.

RBAC (homelab-gitops apps/catcher/rbac.yaml, apps/sre): get Argo CD Applications; in namespace `sre` only:
create/get/list/delete Jobs, create/delete Secrets (the per-run GitHub token), get/list Pods and pods/log.
"""

from __future__ import annotations

import os
import ssl
from typing import Any

import aiohttp

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://kubernetes.default.svc"


class K8sError(RuntimeError):
    def __init__(self, status: int, text: str) -> None:
        super().__init__(f"Kubernetes API HTTP {status}: {text[:300]}")
        self.status = status


class K8s:
    def __init__(self, session: aiohttp.ClientSession, sa_dir: str = SA_DIR, api: str = API) -> None:
        self.session = session
        self.sa_dir = sa_dir
        self.api = api
        self._ssl = ssl.create_default_context(cafile=os.path.join(sa_dir, "ca.crt"))

    def _token(self) -> str:
        with open(os.path.join(self.sa_dir, "token"), encoding="utf-8") as fh:  # rotated by the kubelet: read every time
            return fh.read().strip()

    async def _req(self, method: str, path: str, body: Any = None, text: bool = False) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        async with self.session.request(method, self.api + path, json=body, headers=headers, ssl=self._ssl,
                                        timeout=aiohttp.ClientTimeout(total=30)) as resp:  # fmt: skip
            raw = await resp.text()
            if resp.status >= 300:
                raise K8sError(resp.status, raw)
            if text:
                return raw
            return await resp.json(content_type=None) if raw else None

    async def create(self, path: str, body: dict) -> dict:
        return await self._req("POST", path, body)

    async def get(self, path: str) -> dict:
        return await self._req("GET", path)

    async def delete(self, path: str) -> None:
        await self._req("DELETE", path, {"propagationPolicy": "Background"})

    async def log(self, path: str) -> str:
        return await self._req("GET", path, text=True)
