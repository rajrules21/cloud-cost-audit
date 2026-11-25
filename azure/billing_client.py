#!/usr/bin/env python3
"""
Shared read-only REST client, CSV helpers, and Markdown utilities for Cloud Cost & Billing Audits.
"""

from __future__ import annotations

import csv
import datetime as dt
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential

ARM_SCOPE = "https://management.azure.com/.default"
ARM_BASE = "https://management.azure.com"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso_dt(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def fmt_dt(timestamp: Optional[dt.datetime]) -> str:
    if not timestamp:
        return ""
    return timestamp.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class CostAuditContext:
    output_dir: Path
    csv_dir: Path
    days: int
    stale_days: int
    now: dt.datetime = field(default_factory=utc_now)
    warnings: List[str] = field(default_factory=list)

    def warn(self, message: str) -> None:
        log(f"WARNING: {message}")
        self.warnings.append(message)


class AzureCostClient:
    """Strictly read-only Azure ARM / Resource Graph / Cost Management client."""

    def __init__(self, tenant_id: Optional[str] = None, interactive: bool = False) -> None:
        if interactive:
            kwargs = {"tenant_id": tenant_id} if tenant_id else {}
            self.credential = InteractiveBrowserCredential(**kwargs)
        else:
            kwargs = {"exclude_interactive_browser_credential": True}
            if tenant_id:
                kwargs["tenant_id"] = tenant_id
            self.credential = DefaultAzureCredential(**kwargs)

        self.session = requests.Session()
        self._token: Optional[Tuple[str, float]] = None

    def _get_token(self) -> str:
        if self._token and self._token[1] > time.time() + 120:
            return self._token[0]
        tok = self.credential.get_token(ARM_SCOPE)
        self._token = (tok.token, float(tok.expires_on))
        return tok.token

    def request(
        self,
        method: str,
        url: str,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 60,
    ) -> requests.Response:
        resp = None
        for attempt in range(5):
            headers = {
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json",
            }
            resp = self.session.request(
                method, url, headers=headers, json=json_body, timeout=timeout
            )
            if resp.status_code in (429, 502, 503, 504):
                retry_after = int(resp.headers.get("Retry-After", str(2 ** (attempt + 1))))
                time.sleep(min(retry_after, 30))
                continue
            return resp
        assert resp is not None
        return resp

    def arm_get_all(self, url: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        items: List[Dict[str, Any]] = []
        next_url: Optional[str] = url
        while next_url:
            resp = self.request("GET", next_url)
            if resp.status_code >= 400:
                return items, f"HTTP {resp.status_code} on {next_url}: {resp.text[:250]}"
            data = resp.json()
            items.extend(data.get("value", []))
            next_url = data.get("nextLink")
        return items, None

    def resource_graph(
        self, query: str, subscription_ids: List[str]
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not subscription_ids:
            return [], None
        url = f"{ARM_BASE}/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"
        rows: List[Dict[str, Any]] = []
        skip_token: Optional[str] = None
        while True:
            body: Dict[str, Any] = {
                "subscriptions": subscription_ids,
                "query": query,
                "options": {"resultFormat": "objectArray", "$top": 1000},
            }
            if skip_token:
                body["options"]["$skipToken"] = skip_token
            resp = self.request("POST", url, json_body=body, timeout=90)
            if resp.status_code >= 400:
                return rows, f"HTTP {resp.status_code} Resource Graph: {resp.text[:250]}"
            data = resp.json()
            rows.extend(data.get("data", []))
            skip_token = data.get("$skipToken")
            if not skip_token:
                break
        return rows, None


def write_csv(path: Path, fieldnames: List[str], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def md_table(headers: List[str], rows: List[List[str]]) -> str:
    if not rows:
        return "_No findings in this category._\n"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for r in rows:
        safe = [str(c).replace("|", "\\|").replace("\n", " ") for c in r]
        lines.append("| " + " | ".join(safe) + " |")
    return "\n".join(lines) + "\n"
