#!/usr/bin/env python3
"""
GCP Cost & Billing Audit (Strictly Read-Only).

Zero destructive actions: collects Cloud Billing Accounts & Project linkage,
orphaned Persistent Disks / Static IPs / stale Snapshots / terminated VMs,
14-day Cloud Monitoring VM CPU (Avg & P95), Committed Use Discounts (CUDs),
GCS Lifecycle / Billing Budgets, and unlabeled resources.
Exports CSVs and COST_AUDIT_REPORT.md (no JSON).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from gcp_billing_client import (
    GcpCostClient,
    GcpCostContext,
    fmt_dt,
    log,
    md_table,
    parse_iso_dt,
    percentile,
    write_csv,
)

REQUIRED_COST_LABELS = ("owner", "environment", "costcenter", "project")


def main() -> int:
    parser = argparse.ArgumentParser(description="GCP Cost & Billing Audit (Strictly Read-Only)")
    parser.add_argument("--output-dir", default="./gcp_cost_output", help="Output folder")
    parser.add_argument("--project", nargs="*", help="Optional GCP project IDs (default: all accessible)")
    parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30)")
    parser.add_argument("--stale-days", type=int, default=60, help="Snapshot age threshold in days (default: 60)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    ctx = GcpCostContext(output_dir=out_dir, csv_dir=csv_dir, days=args.days, stale_days=args.stale_days)

    client = GcpCostClient()

    projects, p_err = client.get_paginated(
        "https://cloudresourcemanager.googleapis.com/v1/projects",
        "projects",
        params={"filter": "lifecycleState:ACTIVE"},
    )
    if p_err:
        ctx.warn(f"Failed to list projects: {p_err}")
    if args.project:
        wanted = {p.lower() for p in args.project}
        projects = [p for p in projects if p.get("projectId", "").lower() in wanted]

    billing_rows: List[Dict[str, Any]] = []
    orphan_rows: List[Dict[str, Any]] = []
    vm_rows: List[Dict[str, Any]] = []
    cud_rows: List[Dict[str, Any]] = []
    ops_rows: List[Dict[str, Any]] = []
    unlabeled_rows: List[Dict[str, Any]] = []

    # Billing Accounts & Budgets
    bas, ba_err = client.get_paginated("https://cloudbilling.googleapis.com/v1/billingAccounts", "billingAccounts")
    if ba_err:
        ctx.warn(f"Could not list Billing Accounts: {ba_err}")
    for ba in bas:
        ba_name = ba.get("name", "")
        b_resp = client.request("GET", f"https://billingbudgets.googleapis.com/v1/{ba_name}/budgets")
        if b_resp.status_code < 400:
            budgets = b_resp.json().get("budgets", [])
            if not budgets:
                ops_rows.append({"Area": "Missing Billing Budget", "ProjectId": "(Billing Account)", "Name": ba.get("displayName", ba_name), "Details": "0 Budgets configured", "Recommendation": "Create monthly budget alerts"})
        else:
            ctx.warn(f"Could not check budgets for {ba_name}: HTTP {b_resp.status_code}")

    end_str = ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_str = (ctx.now - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for proj in projects:
        pid = proj["projectId"]
        log(f"Auditing GCP cost posture for project: {pid}...")

        # Project Billing Info
        bi = client.request("GET", f"https://cloudbilling.googleapis.com/v1/projects/{pid}/billingInfo")
        if bi.status_code < 400:
            bdata = bi.json()
            billing_rows.append({
                "ProjectId": pid,
                "BillingEnabled": bdata.get("billingEnabled", False),
                "BillingAccount": bdata.get("billingAccountName", ""),
            })

        # Unattached Persistent Disks
        agg_disks = client.request("GET", f"https://compute.googleapis.com/compute/v1/projects/{pid}/aggregated/disks")
        if agg_disks.status_code < 400:
            for _, scope in (agg_disks.json().get("items") or {}).items():
                for d in scope.get("disks", []):
                    if not d.get("users"):
                        orphan_rows.append({
                            "Category": "Unattached Persistent Disk",
                            "ProjectId": pid,
                            "Name": d.get("name", ""),
                            "Details": f"SizeGB={d.get('sizeGb')}, Type={str(d.get('type', '')).split('/')[-1]}",
                            "RecommendedAction": "Snapshot if needed and delete",
                        })
                    labels = {k.lower() for k in (d.get("labels") or {}).keys()}
                    missing = [l for l in REQUIRED_COST_LABELS if l not in labels]
                    if missing:
                        unlabeled_rows.append({"ProjectId": pid, "ResourceType": "Persistent Disk", "Name": d.get("name", ""), "MissingCostLabels": ", ".join(missing)})

        # Unused Static External IPs
        agg_addrs = client.request("GET", f"https://compute.googleapis.com/compute/v1/projects/{pid}/aggregated/addresses")
        if agg_addrs.status_code < 400:
            for _, scope in (agg_addrs.json().get("items") or {}).items():
                for a in scope.get("addresses", []):
                    if a.get("status") == "RESERVED" and a.get("addressType") == "EXTERNAL":
                        orphan_rows.append({
                            "Category": "Unused Static External IP",
                            "ProjectId": pid,
                            "Name": a.get("name", ""),
                            "Details": f"IP={a.get('address')}",
                            "RecommendedAction": "Release unused static IP",
                        })

        # Stale Snapshots
        snaps, _ = client.get_paginated(f"https://compute.googleapis.com/compute/v1/projects/{pid}/global/snapshots", "items")
        for s in snaps:
            created = parse_iso_dt(s.get("creationTimestamp"))
            age = (ctx.now - created).days if created else None
            if age is not None and age >= ctx.stale_days:
                orphan_rows.append({
                    "Category": f"Stale Snapshot (>={ctx.stale_days}d)",
                    "ProjectId": pid,
                    "Name": s.get("name", ""),
                    "Details": f"Age={age}d, DiskSizeGB={s.get('diskSizeGb')}",
                    "RecommendedAction": "Review retention; delete obsolete snapshots",
                })

        # Compute VMs: stopped, unlabeled, and 14d CPU utilization
        agg_vms = client.request("GET", f"https://compute.googleapis.com/compute/v1/projects/{pid}/aggregated/instances")
        if agg_vms.status_code < 400:
            for _, scope in (agg_vms.json().get("items") or {}).items():
                for vm in scope.get("instances", []):
                    vname = vm.get("name", "")
                    status = vm.get("status", "")
                    mtype = str(vm.get("machineType", "")).split("/")[-1]
                    labels = {k.lower() for k in (vm.get("labels") or {}).keys()}
                    missing = [l for l in REQUIRED_COST_LABELS if l not in labels]
                    if missing and status != "TERMINATED":
                        unlabeled_rows.append({"ProjectId": pid, "ResourceType": "Compute VM", "Name": vname, "MissingCostLabels": ", ".join(missing)})
                    if status == "TERMINATED":
                        orphan_rows.append({"Category": "Stopped Compute VM", "ProjectId": pid, "Name": vname, "Details": f"MachineType={mtype} (Still billing attached disks)", "RecommendedAction": "Delete if obsolete"})
                    elif status == "RUNNING":
                        ts_resp = client.request(
                            "GET",
                            f"https://monitoring.googleapis.com/v3/projects/{pid}/timeSeries",
                            params={
                                "filter": f'metric.type="compute.googleapis.com/instance/cpu/utilization" AND resource.labels.instance_id="{vm.get("id")}"',
                                "interval.startTime": start_str,
                                "interval.endTime": end_str,
                                "aggregation.alignmentPeriod": "3600s",
                                "aggregation.perSeriesAligner": "ALIGN_MEAN",
                            },
                        )
                        if ts_resp.status_code < 400:
                            vals = [
                                float(pt["value"]["doubleValue"]) * 100.0
                                for series in ts_resp.json().get("timeSeries", [])
                                for pt in series.get("points", [])
                                if "doubleValue" in pt.get("value", {})
                            ]
                            if vals:
                                avg_cpu = round(sum(vals) / len(vals), 2)
                                p95_cpu = percentile(vals, 95)
                                if p95_cpu is not None and p95_cpu < 15.0:
                                    vm_rows.append({"Source": "14d Cloud Monitoring CPU", "ProjectId": pid, "Resource": vname, "Details": f"MachineType={mtype} | AvgCPU={avg_cpu}% | P95CPU={p95_cpu}%", "Recommendation": "Underutilized CPU (verify memory/disk before downsizing)"})

        # Committed Use Discounts (CUDs)
        agg_cuds = client.request("GET", f"https://compute.googleapis.com/compute/v1/projects/{pid}/aggregated/commitments")
        if agg_cuds.status_code < 400:
            for _, scope in (agg_cuds.json().get("items") or {}).items():
                for c in scope.get("commitments", []):
                    cud_rows.append({"ProjectId": pid, "Name": c.get("name", ""), "Plan": c.get("plan", ""), "Status": c.get("status", ""), "EndTimestamp": c.get("endTimestamp", "")})

        # GCS Lifecycle
        buckets, b_err = client.get_paginated("https://storage.googleapis.com/storage/v1/b", "items", params={"project": pid})
        if not b_err:
            for b in buckets:
                bname = b.get("name", "")
                if not (b.get("lifecycle") or {}).get("rule"):
                    ops_rows.append({"Area": "GCS Missing Lifecycle", "ProjectId": pid, "Name": bname, "Details": f"StorageClass={b.get('storageClass')}", "Recommendation": "Add lifecycle rules (Nearline/Coldline/Archive)"})

    write_csv(csv_dir / "01_billing_accounts_and_projects.csv", ["ProjectId", "BillingEnabled", "BillingAccount"], billing_rows)
    write_csv(csv_dir / "02_orphaned_and_idle_resources.csv", ["Category", "ProjectId", "Name", "Details", "RecommendedAction"], orphan_rows)
    write_csv(csv_dir / "03_vm_rightsizing_and_recommender.csv", ["Source", "ProjectId", "Resource", "Details", "Recommendation"], vm_rows)
    write_csv(csv_dir / "04_committed_use_discounts.csv", ["ProjectId", "Name", "Plan", "Status", "EndTimestamp"], cud_rows)
    write_csv(csv_dir / "05_storage_logs_and_budgets.csv", ["Area", "ProjectId", "Name", "Details", "Recommendation"], ops_rows)
    write_csv(csv_dir / "06_unlabeled_resources.csv", ["ProjectId", "ResourceType", "Name", "MissingCostLabels"], unlabeled_rows)

    lines = [
        "# GCP Cost & Billing Audit Report",
        "",
        f"- **Generated (UTC):** {fmt_dt(ctx.now)}",
        f"- **Projects Audited:** {len(projects)}",
        "",
        "## Executive Summary",
        "",
        md_table(
            ["Audit Area", "Findings", "Detailed CSV"],
            [
                ["Billing-Linked Projects", str(len(billing_rows)), "[01_billing_accounts_and_projects.csv](./csv/01_billing_accounts_and_projects.csv)"],
                ["Orphaned & Idle Resources", str(len(orphan_rows)), "[02_orphaned_and_idle_resources.csv](./csv/02_orphaned_and_idle_resources.csv)"],
                ["Underutilized Compute VMs", str(len(vm_rows)), "[03_vm_rightsizing_and_recommender.csv](./csv/03_vm_rightsizing_and_recommender.csv)"],
                ["Committed Use Discounts (CUDs)", str(len(cud_rows)), "[04_committed_use_discounts.csv](./csv/04_committed_use_discounts.csv)"],
                ["GCS Lifecycle & Missing Budgets", str(len(ops_rows)), "[05_storage_logs_and_budgets.csv](./csv/05_storage_logs_and_budgets.csv)"],
                ["Unlabeled Resources", str(len(unlabeled_rows)), "[06_unlabeled_resources.csv](./csv/06_unlabeled_resources.csv)"],
            ],
        ),
    ]
    if ctx.warnings:
        lines.extend(["## Audit Warnings", ""] + [f"- {w}" for w in ctx.warnings] + [""])

    report_path = out_dir / "COST_AUDIT_REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"GCP Cost Audit complete! Report written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
