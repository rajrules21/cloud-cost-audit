#!/usr/bin/env python3
"""
Shared read-only boto3 billing/cost helpers, CSV & Markdown utilities
for AWS Cost & FinOps Audits.
"""

from __future__ import annotations

import csv
import datetime as dt
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def fmt_dt(timestamp: Optional[dt.datetime]) -> str:
    if not timestamp:
        return ""
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=dt.timezone.utc)
    return timestamp.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[idx], 2)


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class AwsCostContext:
    output_dir: Path
    csv_dir: Path
    days: int
    stale_days: int
    now: dt.datetime = field(default_factory=utc_now)
    warnings: List[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        log(f"WARNING: {msg}")
        self.warnings.append(msg)


def get_enabled_regions(session: boto3.Session, filter_regions: Optional[List[str]]) -> List[str]:
    if filter_regions:
        return filter_regions
    ec2 = session.client("ec2", region_name="us-east-1")
    try:
        resp = ec2.describe_regions(AllRegions=False)
        return sorted(r["RegionName"] for r in resp.get("Regions", []))
    except ClientError:
        return ["us-east-1"]


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
