#!/usr/bin/env python3
"""
Azure Cost & Billing Audit (Strictly Read-Only).

Zero destructive actions: performs only GET requests and read-only Resource Graph /
Cost Management queries. Exports CSVs and COST_AUDIT_REPORT.md (no JSON).
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from billing_client import (
    ARM_BASE,
    AzureCostClient,
    CostAuditContext,
    fmt_dt,
    log,
    md_table,
    parse_iso_dt,
    write_csv,
)

REQUIRED_COST_TAGS = ("owner", "environment", "costcenter", "project")


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return round(ordered[idx], 2)


def collect_subscriptions(
    client: AzureCostClient, ctx: CostAuditContext, filter_subs: Optional[List[str]]
) -> List[Dict[str, Any]]:
    log("Discovering accessible Azure subscriptions...")
    subs, err = client.arm_get_all(f"{ARM_BASE}/subscriptions?api-version=2022-12-01")
    if err:
        ctx.warn(f"Failed to list subscriptions: {err}")
        return []
    active = [s for s in subs if s.get("state") == "Enabled"]
    if filter_subs:
        wanted = {s.lower() for s in filter_subs}
        active = [s for s in active if s.get("subscriptionId", "").lower() in wanted]
    log(f"  Found {len(active)} enabled subscription(s) in scope.")
    return active


def collect_cost_management(
    client: AzureCostClient,
    ctx: CostAuditContext,
    subs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    log(f"Querying Azure Cost Management (AmortizedCost, last {ctx.days} days)...")
    end_dt = ctx.now
    start_dt = end_dt - dt.timedelta(days=ctx.days)
    rows: List[Dict[str, Any]] = []

    for sub in subs:
        sub_id = sub["subscriptionId"]
        sub_name = sub.get("displayName", sub_id)
        url = f"{ARM_BASE}/subscriptions/{sub_id}/providers/Microsoft.CostManagement/query?api-version=2023-11-01"
        body = {
            "type": "AmortizedCost",
            "timeframe": "Custom",
            "timePeriod": {
                "from": start_dt.strftime("%Y-%m-%dT00:00:00Z"),
                "to": end_dt.strftime("%Y-%m-%dT23:59:59Z"),
            },
            "dataset": {
                "granularity": "None",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"}
                },
                "grouping": [
                    {"type": "Dimension", "name": "ResourceGroupName"},
                    {"type": "Dimension", "name": "ServiceName"},
                ],
            },
        }
        resp = client.request("POST", url, json_body=body, timeout=60)
        if resp.status_code >= 400:
            ctx.warn(
                f"Cost Management query failed for {sub_name} ({sub_id}) "
                f"[requires Cost Management Reader]: HTTP {resp.status_code}"
            )
            continue

        data = resp.json().get("properties", {})
        cols = [c.get("name", "") for c in data.get("columns", [])]
        for r in data.get("rows", []):
            rec = dict(zip(cols, r))
            cost_val = float(rec.get("Cost") or 0.0)
            if cost_val <= 0:
                continue
            rows.append({
                "Subscription": sub_name,
                "SubscriptionId": sub_id,
                "ResourceGroup": rec.get("ResourceGroupName") or "(Subscription / Unassigned)",
                "ServiceName": rec.get("ServiceName") or "Unknown",
                "AmortizedCost": round(cost_val, 2),
                "Currency": rec.get("Currency") or "USD",
            })
        # Respect Cost Management API rate limits
        time.sleep(0.5)

    rows.sort(key=lambda x: x["AmortizedCost"], reverse=True)
    log(f"  Collected {len(rows)} non-zero Cost Management rows.")
    return rows


def collect_orphaned_and_idle(
    client: AzureCostClient,
    ctx: CostAuditContext,
    sub_ids: List[str],
    sub_name_by_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    log("Scanning for orphaned & idle resources via Resource Graph (read-only)...")
    findings: List[Dict[str, Any]] = []

    # 1. Unattached Managed Disks
    disks, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.compute/disks"
        | where tostring(properties.diskState) == "Unattached"
        | project subscriptionId, resourceGroup, name, location,
            sku = tostring(sku.name), sizeGb = toint(properties.diskSizeGB),
            timeCreated = tostring(properties.timeCreated)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Disks query failed: {err}")
    for d in disks:
        findings.append({
            "Category": "Unattached Managed Disk",
            "Subscription": sub_name_by_id.get(d.get("subscriptionId", ""), d.get("subscriptionId", "")),
            "ResourceGroup": d.get("resourceGroup", ""),
            "Name": d.get("name", ""),
            "Location": d.get("location", ""),
            "Details": f"SKU={d.get('sku')}, SizeGB={d.get('sizeGb')}, Created={d.get('timeCreated')}",
            "RecommendedAction": "Verify with owner; snapshot if needed, then delete",
        })

    # 2. Stopped (not deallocated) VMs
    stopped_vms, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.compute/virtualmachines"
        | where tostring(properties.extended.instanceView.powerState.code) == "PowerState/stopped"
        | project subscriptionId, resourceGroup, name, location,
            vmSize = tostring(properties.hardwareProfile.vmSize)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Stopped VM query failed: {err}")
    for vm in stopped_vms:
        findings.append({
            "Category": "Stopped (Not Deallocated) VM",
            "Subscription": sub_name_by_id.get(vm.get("subscriptionId", ""), vm.get("subscriptionId", "")),
            "ResourceGroup": vm.get("resourceGroup", ""),
            "Name": vm.get("name", ""),
            "Location": vm.get("location", ""),
            "Details": f"Size={vm.get('vmSize')} (Still billing compute!)",
            "RecommendedAction": "Deallocate VM if idle instead of OS-only shutdown",
        })

    # 3. Unattached Public IPs
    pips, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.network/publicipaddresses"
        | where isnull(properties.ipConfiguration.id) or tostring(properties.ipConfiguration.id) == ""
        | project subscriptionId, resourceGroup, name, location,
            sku = tostring(sku.name), ip = tostring(properties.ipAddress)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Unattached Public IP query failed: {err}")
    for p in pips:
        findings.append({
            "Category": "Unattached Public IP",
            "Subscription": sub_name_by_id.get(p.get("subscriptionId", ""), p.get("subscriptionId", "")),
            "ResourceGroup": p.get("resourceGroup", ""),
            "Name": p.get("name", ""),
            "Location": p.get("location", ""),
            "Details": f"SKU={p.get('sku')}, IP={p.get('ip') or 'Dynamic'}",
            "RecommendedAction": "Release unused static Public IP",
        })

    # 4. Stale Snapshots
    snaps, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.compute/snapshots"
        | project subscriptionId, resourceGroup, name, location,
            sizeGb = toint(properties.diskSizeGB),
            sku = tostring(sku.name),
            timeCreated = tostring(properties.timeCreated)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Snapshot query failed: {err}")
    for s in snaps:
        created = parse_iso_dt(s.get("timeCreated"))
        age = (ctx.now - created).days if created else None
        if age is not None and age >= ctx.stale_days:
            findings.append({
                "Category": f"Stale Snapshot (>={ctx.stale_days}d)",
                "Subscription": sub_name_by_id.get(s.get("subscriptionId", ""), s.get("subscriptionId", "")),
                "ResourceGroup": s.get("resourceGroup", ""),
                "Name": s.get("name", ""),
                "Location": s.get("location", ""),
                "Details": f"Age={age}d, SizeGB={s.get('sizeGb')}, SKU={s.get('sku')}",
                "RecommendedAction": "Review retention need; delete if obsolete",
            })

    # 5. Empty App Service Plans
    asps, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.web/serverfarms"
        | where toint(properties.numberOfSites) == 0
        | project subscriptionId, resourceGroup, name, location,
            sku = tostring(sku.name), tier = tostring(sku.tier)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"App Service Plan query failed: {err}")
    for asp in asps:
        findings.append({
            "Category": "Empty App Service Plan",
            "Subscription": sub_name_by_id.get(asp.get("subscriptionId", ""), asp.get("subscriptionId", "")),
            "ResourceGroup": asp.get("resourceGroup", ""),
            "Name": asp.get("name", ""),
            "Location": asp.get("location", ""),
            "Details": f"SKU={asp.get('sku')} ({asp.get('tier')}), Sites=0",
            "RecommendedAction": "Delete empty App Service Plan if no longer needed",
        })

    # 6. Unused Load Balancers
    lbs, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.network/loadbalancers"
        | where array_length(properties.backendAddressPools) == 0
        | project subscriptionId, resourceGroup, name, location, sku = tostring(sku.name)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Load Balancer query failed: {err}")
    for lb in lbs:
        findings.append({
            "Category": "Unused Load Balancer",
            "Subscription": sub_name_by_id.get(lb.get("subscriptionId", ""), lb.get("subscriptionId", "")),
            "ResourceGroup": lb.get("resourceGroup", ""),
            "Name": lb.get("name", ""),
            "Location": lb.get("location", ""),
            "Details": f"SKU={lb.get('sku')}, BackendPools=0",
            "RecommendedAction": "Verify and remove unused Load Balancer",
        })

    log(f"  Found {len(findings)} orphaned or idle resource(s).")
    return findings


def collect_vm_rightsizing_and_advisor(
    client: AzureCostClient,
    ctx: CostAuditContext,
    sub_ids: List[str],
    sub_name_by_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    log("Collecting running VM CPU metrics (14-day Avg & P95) and Azure Advisor Cost recommendations...")
    rows: List[Dict[str, Any]] = []

    vms, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.compute/virtualmachines"
        | where tostring(properties.extended.instanceView.powerState.code) == "PowerState/running"
        | project id, subscriptionId, resourceGroup, name,
            vmSize = tostring(properties.hardwareProfile.vmSize)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Running VM query failed: {err}")

    end_str = ctx.now.strftime("%Y-%m-%dT%H:%M:%SZ")
    start_str = (ctx.now - dt.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    for vm in vms[:200]:  # Cap metric calls for safety on very large estates
        vm_id = vm.get("id", "")
        m_url = (
            f"{ARM_BASE}{vm_id}/providers/Microsoft.Insights/metrics"
            f"?api-version=2023-10-01&metricnames=Percentage%20CPU"
            f"&timespan={start_str}/{end_str}&interval=PT1H&aggregation=Average,Maximum"
        )
        resp = client.request("GET", m_url)
        if resp.status_code >= 400:
            continue
        avgs: List[float] = []
        maxs: List[float] = []
        for ts_entry in resp.json().get("value", [{}])[0].get("timeseries", []):
            for pt in ts_entry.get("data", []):
                if pt.get("average") is not None:
                    avgs.append(float(pt["average"]))
                if pt.get("maximum") is not None:
                    maxs.append(float(pt["maximum"]))
        if not avgs:
            continue
        avg_cpu = round(sum(avgs) / len(avgs), 2)
        p95_cpu = percentile(maxs or avgs, 95)
        if p95_cpu is not None and p95_cpu < 15.0:
            rows.append({
                "Source": "14d VM CPU Metrics",
                "Subscription": sub_name_by_id.get(vm.get("subscriptionId", ""), vm.get("subscriptionId", "")),
                "ResourceGroup": vm.get("resourceGroup", ""),
                "ResourceName": vm.get("name", ""),
                "Details": f"Size={vm.get('vmSize')} | AvgCPU={avg_cpu}% | P95MaxCPU={p95_cpu}%",
                "Recommendation": "Underutilized CPU (verify Memory/IOPS before downsizing)",
            })

    advisor, adv_err = client.resource_graph(
        """
        advisorresources
        | where type == "microsoft.advisor/recommendations"
        | where tostring(properties.category) == "Cost"
        | project subscriptionId,
            impact = tostring(properties.impact),
            resourceName = tostring(properties.impactedValue),
            problem = tostring(properties.shortDescription.problem),
            solution = tostring(properties.shortDescription.solution),
            savings = tostring(properties.extendedProperties.savingsAmount),
            currency = tostring(properties.extendedProperties.savingsCurrency)
        """,
        sub_ids,
    )
    if adv_err:
        ctx.warn(f"Azure Advisor query failed: {adv_err}")
    for a in advisor:
        sav = f"Est. Savings: {a.get('savings')} {a.get('currency')}" if a.get("savings") else ""
        rows.append({
            "Source": f"Azure Advisor ({a.get('impact', 'Cost')})",
            "Subscription": sub_name_by_id.get(a.get("subscriptionId", ""), a.get("subscriptionId", "")),
            "ResourceGroup": "",
            "ResourceName": a.get("resourceName", ""),
            "Details": f"{a.get('problem', '')} {sav}".strip(),
            "Recommendation": a.get("solution", ""),
        })

    return rows


def collect_licensing_and_commitments(
    client: AzureCostClient,
    ctx: CostAuditContext,
    sub_ids: List[str],
    sub_name_by_id: Dict[str, str],
) -> List[Dict[str, Any]]:
    log("Checking Azure Hybrid Benefit (AHB) and Reservations / Savings Plans...")
    rows: List[Dict[str, Any]] = []

    win_vms, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.compute/virtualmachines"
        | where tostring(properties.storageProfile.osDisk.osType) == "Windows"
        | where isnull(properties.licenseType) or tostring(properties.licenseType) == ""
        | project subscriptionId, resourceGroup, name, vmSize = tostring(properties.hardwareProfile.vmSize)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"AHB VM query failed: {err}")
    for vm in win_vms:
        rows.append({
            "Type": "Azure Hybrid Benefit Gap (Windows VM)",
            "Subscription": sub_name_by_id.get(vm.get("subscriptionId", ""), vm.get("subscriptionId", "")),
            "Name": f"{vm.get('resourceGroup')}/{vm.get('name')}",
            "Details": f"Size={vm.get('vmSize')}, licenseType=None (paying PAYG Windows license)",
            "Action": "Enable Windows_Server AHB if eligible under Software Assurance",
        })

    # Reservations (tenant-level; optional permission)
    res_orders, r_err = client.arm_get_all(f"{ARM_BASE}/providers/Microsoft.Capacity/reservationOrders?api-version=2022-11-01")
    if r_err:
        ctx.warn("Could not list Azure Reservations (requires Reservations Reader if applicable).")
    else:
        for ro in res_orders:
            props = ro.get("properties", {})
            rows.append({
                "Type": "Azure Reservation Order",
                "Subscription": "(Tenant / Billing Scope)",
                "Name": props.get("displayName") or ro.get("name", ""),
                "Details": f"Term={props.get('term')}, State={props.get('provisioningState')}, Expiry={props.get('expiryDate')}",
                "Action": "Review utilization in Azure Reservations blade",
            })

    return rows


def collect_storage_logs_budgets_and_tags(
    client: AzureCostClient,
    ctx: CostAuditContext,
    subs: List[Dict[str, Any]],
    sub_ids: List[str],
    sub_name_by_id: Dict[str, str],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    log("Checking Storage redundancy, Log Analytics caps, Subscription Budgets, and Tagging...")
    ops_rows: List[Dict[str, Any]] = []

    sas, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.storage/storageaccounts"
        | where tostring(sku.name) contains "GRS"
        | project subscriptionId, resourceGroup, name, sku = tostring(sku.name)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Storage redundancy query failed: {err}")
    for sa in sas:
        ops_rows.append({
            "Area": "Storage Redundancy (GRS)",
            "Subscription": sub_name_by_id.get(sa.get("subscriptionId", ""), sa.get("subscriptionId", "")),
            "Name": f"{sa.get('resourceGroup')}/{sa.get('name')}",
            "Details": f"SKU={sa.get('sku')} (Geo-redundant; ~2x LRS cost)",
            "Recommendation": "Use LRS/ZRS for non-prod or reproducible data; add Blob Lifecycle policies",
        })

    laws, err = client.resource_graph(
        """
        resources
        | where type == "microsoft.operationalinsights/workspaces"
        | project subscriptionId, resourceGroup, name,
            retention = toint(properties.retentionInDays),
            dailyCapGb = todouble(properties.workspaceCapping.dailyQuotaGb)
        """,
        sub_ids,
    )
    if err:
        ctx.warn(f"Log Analytics query failed: {err}")
    for w in laws:
        cap = w.get("dailyCapGb")
        cap_str = "Unlimited (-1)" if cap in (None, -1, -1.0) else f"{cap} GB/day"
        if cap in (None, -1, -1.0) or (w.get("retention") or 0) > 90:
            ops_rows.append({
                "Area": "Log Analytics Workspace",
                "Subscription": sub_name_by_id.get(w.get("subscriptionId", ""), w.get("subscriptionId", "")),
                "Name": f"{w.get('resourceGroup')}/{w.get('name')}",
                "Details": f"Retention={w.get('retention')}d, DailyCap={cap_str}",
                "Recommendation": "Review top-ingesting tables; consider Basic Logs / daily cap for dev",
            })

    for sub in subs:
        sid = sub["subscriptionId"]
        sname = sub.get("displayName", sid)
        budgets, b_err = client.arm_get_all(f"{ARM_BASE}/subscriptions/{sid}/providers/Microsoft.Consumption/budgets?api-version=2023-11-01")
        if b_err:
            ctx.warn(f"Could not check budgets for {sname}: {b_err}")
            continue
        if not budgets:
            ops_rows.append({
                "Area": "Missing Budget Alert",
                "Subscription": sname,
                "Name": sid,
                "Details": "0 Budgets configured on subscription",
                "Recommendation": "Create a monthly budget with 80%/100% forecast alerts",
            })

    # Untagged resources
    untagged_rows: List[Dict[str, Any]] = []
    res_tags, t_err = client.resource_graph(
        """
        resources
        | project subscriptionId, resourceGroup, name, type, tags
        """,
        sub_ids,
    )
    if t_err:
        ctx.warn(f"Tagging query failed: {t_err}")
    for r in res_tags:
        tags_lower = {str(k).lower() for k in (r.get("tags") or {}).keys()}
        missing = [t for t in REQUIRED_COST_TAGS if t not in tags_lower]
        if missing:
            untagged_rows.append({
                "Subscription": sub_name_by_id.get(r.get("subscriptionId", ""), r.get("subscriptionId", "")),
                "ResourceGroup": r.get("resourceGroup", ""),
                "ResourceType": r.get("type", ""),
                "Name": r.get("name", ""),
                "MissingCostTags": ", ".join(missing),
            })

    return ops_rows, untagged_rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Azure Cost & Billing Audit (Strictly Read-Only)")
    parser.add_argument("--output-dir", default="./cost_audit_output", help="Output folder (default: ./cost_audit_output)")
    parser.add_argument("--days", type=int, default=30, help="Cost Management lookback in days (default: 30)")
    parser.add_argument("--stale-days", type=int, default=60, help="Snapshot age threshold in days (default: 60)")
    parser.add_argument("--subscription", nargs="*", help="Optional subscription IDs (default: all)")
    parser.add_argument("--tenant-id", help="Optional Azure tenant ID")
    parser.add_argument("--interactive", action="store_true", help="Use interactive browser sign-in")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    ctx = CostAuditContext(output_dir=out_dir, csv_dir=csv_dir, days=args.days, stale_days=args.stale_days)

    log(f"Starting Read-Only Azure Cost & Billing Audit -> {out_dir}")
    client = AzureCostClient(tenant_id=args.tenant_id, interactive=args.interactive)

    subs = collect_subscriptions(client, ctx, args.subscription)
    sub_ids = [s["subscriptionId"] for s in subs]
    sub_name_by_id = {s["subscriptionId"]: s.get("displayName", s["subscriptionId"]) for s in subs}

    cost_rows = collect_cost_management(client, ctx, subs)
    orphan_rows = collect_orphaned_and_idle(client, ctx, sub_ids, sub_name_by_id)
    vm_advisor_rows = collect_vm_rightsizing_and_advisor(client, ctx, sub_ids, sub_name_by_id)
    lic_rows = collect_licensing_and_commitments(client, ctx, sub_ids, sub_name_by_id)
    ops_rows, untagged_rows = collect_storage_logs_budgets_and_tags(client, ctx, subs, sub_ids, sub_name_by_id)

    write_csv(csv_dir / "01_cost_by_rg_and_service.csv", ["Subscription", "SubscriptionId", "ResourceGroup", "ServiceName", "AmortizedCost", "Currency"], cost_rows)
    write_csv(csv_dir / "02_orphaned_and_idle_resources.csv", ["Category", "Subscription", "ResourceGroup", "Name", "Location", "Details", "RecommendedAction"], orphan_rows)
    write_csv(csv_dir / "03_vm_rightsizing_and_advisor.csv", ["Source", "Subscription", "ResourceGroup", "ResourceName", "Details", "Recommendation"], vm_advisor_rows)
    write_csv(csv_dir / "04_licensing_and_commitments.csv", ["Type", "Subscription", "Name", "Details", "Action"], lic_rows)
    write_csv(csv_dir / "05_storage_logs_and_budgets.csv", ["Area", "Subscription", "Name", "Details", "Recommendation"], ops_rows)
    write_csv(csv_dir / "06_untagged_resources.csv", ["Subscription", "ResourceGroup", "ResourceType", "Name", "MissingCostTags"], untagged_rows)

    total_spend = round(sum(r["AmortizedCost"] for r in cost_rows), 2)
    lines = [
        "# Azure Cost & Billing Audit Report",
        "",
        f"- **Generated (UTC):** {fmt_dt(ctx.now)}",
        f"- **Lookback Window:** {ctx.days} days (Snapshot stale threshold: {ctx.stale_days} days)",
        f"- **Subscriptions Audited:** {len(subs)}",
        f"- **Total Amortized Spend Observed:** {total_spend:,.2f}",
        "",
        "## Executive Summary",
        "",
        md_table(
            ["Audit Area", "Findings", "Detailed CSV"],
            [
                ["Cost Breakdown (RG x Service)", str(len(cost_rows)), "[01_cost_by_rg_and_service.csv](./csv/01_cost_by_rg_and_service.csv)"],
                ["Orphaned & Idle Resources", str(len(orphan_rows)), "[02_orphaned_and_idle_resources.csv](./csv/02_orphaned_and_idle_resources.csv)"],
                ["Underutilized VMs & Advisor Cost Recs", str(len(vm_advisor_rows)), "[03_vm_rightsizing_and_advisor.csv](./csv/03_vm_rightsizing_and_advisor.csv)"],
                ["Hybrid Benefit & Commitments", str(len(lic_rows)), "[04_licensing_and_commitments.csv](./csv/04_licensing_and_commitments.csv)"],
                ["Storage GRS, Log Caps & Missing Budgets", str(len(ops_rows)), "[05_storage_logs_and_budgets.csv](./csv/05_storage_logs_and_budgets.csv)"],
                ["Untagged Resources", str(len(untagged_rows)), "[06_untagged_resources.csv](./csv/06_untagged_resources.csv)"],
            ],
        ),
    ]

    if ctx.warnings:
        lines.extend(["## Coverage & Permission Notes", ""])
        for w in ctx.warnings:
            lines.append(f"- {w}")
        lines.append("")

    lines.extend([
        "## Top 20 Cost Drivers (Resource Group x Service)",
        "",
        md_table(
            ["Subscription", "Resource Group", "Service", "Amortized Cost"],
            [[r["Subscription"], r["ResourceGroup"], r["ServiceName"], f"{r['AmortizedCost']:,.2f} {r['Currency']}"] for r in cost_rows[:20]],
        ),
        "## Top 25 Orphaned & Idle Resources (No Auto-Delete Performed)",
        "",
        md_table(
            ["Category", "Subscription", "Resource Group", "Name", "Details"],
            [[r["Category"], r["Subscription"], r["ResourceGroup"], r["Name"], r["Details"]] for r in orphan_rows[:25]],
        ),
    ])

    report_path = out_dir / "COST_AUDIT_REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Cost audit complete! Report written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
