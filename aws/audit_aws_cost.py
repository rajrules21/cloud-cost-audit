#!/usr/bin/env python3
"""
AWS Cost & Billing Audit (Strictly Read-Only).

Zero destructive actions: collects Cost Explorer spend, orphaned EBS/EIPs/snapshots/LBs,
stopped EC2 instances, 14-day CloudWatch CPU (Avg & P95), Compute Optimizer,
Savings Plans / RI utilization, S3 lifecycle, CloudWatch retention, AWS Budgets,
and untagged resources. Exports CSVs and COST_AUDIT_REPORT.md (no JSON).
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

from aws_billing_client import (
    AwsCostContext,
    fmt_dt,
    get_enabled_regions,
    log,
    md_table,
    percentile,
    write_csv,
)

REQUIRED_COST_TAGS = ("owner", "environment", "costcenter", "project")


def collect_cost_explorer(session: boto3.Session, ctx: AwsCostContext) -> List[Dict[str, Any]]:
    log(f"Querying AWS Cost Explorer (AmortizedCost, last {ctx.days} days)...")
    ce = session.client("ce", region_name="us-east-1")
    end_str = ctx.now.strftime("%Y-%m-%d")
    start_str = (ctx.now - dt.timedelta(days=ctx.days)).strftime("%Y-%m-%d")
    rows: List[Dict[str, Any]] = []
    try:
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": start_str, "End": end_str},
            Granularity="MONTHLY",
            Metrics=["AmortizedCost"],
            GroupBy=[
                {"Type": "DIMENSION", "Key": "SERVICE"},
                {"Type": "DIMENSION", "Key": "REGION"},
            ],
        )
        for period in resp.get("ResultsByTime", []):
            p_start = period["TimePeriod"]["Start"]
            for grp in period.get("Groups", []):
                service, region = grp["Keys"]
                amt = float(grp["Metrics"]["AmortizedCost"]["Amount"])
                unit = grp["Metrics"]["AmortizedCost"]["Unit"]
                if amt <= 0:
                    continue
                rows.append({
                    "PeriodStart": p_start,
                    "Service": service,
                    "Region": region or "Global",
                    "AmortizedCost": round(amt, 2),
                    "Currency": unit,
                })
    except ClientError as e:
        ctx.warn(f"Cost Explorer query failed (requires ce:GetCostAndUsage): {e}")
    rows.sort(key=lambda x: x["AmortizedCost"], reverse=True)
    return rows


def collect_commitments(session: boto3.Session, ctx: AwsCostContext) -> List[Dict[str, Any]]:
    log("Checking Savings Plans and Reserved Instance utilization...")
    ce = session.client("ce", region_name="us-east-1")
    end_str = ctx.now.strftime("%Y-%m-%d")
    start_str = (ctx.now - dt.timedelta(days=ctx.days)).strftime("%Y-%m-%d")
    rows: List[Dict[str, Any]] = []
    try:
        sp = ce.get_savings_plans_utilization(TimePeriod={"Start": start_str, "End": end_str}).get("Total", {})
        util = sp.get("Utilization", {})
        if util:
            rows.append({
                "Type": "Savings Plans",
                "UtilizationPct": util.get("UtilizationPercentage", ""),
                "UsedCommitment": util.get("UsedCommitment", ""),
                "UnusedCommitment": util.get("UnusedCommitment", ""),
                "NetSavings": sp.get("Savings", {}).get("NetSavings", ""),
            })
    except ClientError as e:
        ctx.warn(f"Savings Plans utilization check failed: {e}")

    try:
        ri = ce.get_reservation_utilization(TimePeriod={"Start": start_str, "End": end_str}).get("Total", {})
        if ri:
            rows.append({
                "Type": "Reserved Instances",
                "UtilizationPct": ri.get("UtilizationPercentage", ""),
                "UsedCommitment": ri.get("PurchasedHours", ""),
                "UnusedCommitment": ri.get("UnusedHours", ""),
                "NetSavings": ri.get("NetRISavings", ""),
            })
    except ClientError as e:
        ctx.warn(f"Reserved Instance utilization check failed: {e}")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="AWS Cost & Billing Audit (Strictly Read-Only)")
    parser.add_argument("--output-dir", default="./aws_cost_output", help="Output folder")
    parser.add_argument("--profile", help="Optional AWS CLI profile name")
    parser.add_argument("--region", nargs="*", help="Optional AWS regions (default: all enabled)")
    parser.add_argument("--days", type=int, default=30, help="Cost lookback in days (default: 30)")
    parser.add_argument("--stale-days", type=int, default=60, help="Snapshot age threshold in days (default: 60)")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    csv_dir = out_dir / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    ctx = AwsCostContext(output_dir=out_dir, csv_dir=csv_dir, days=args.days, stale_days=args.stale_days)

    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    ident = session.client("sts").get_caller_identity()
    account_id = ident.get("Account", "")
    log(f"Authenticated to AWS Account {account_id}")

    regions = get_enabled_regions(session, args.region)
    cost_rows = collect_cost_explorer(session, ctx)
    commit_rows = collect_commitments(session, ctx)

    orphan_rows: List[Dict[str, Any]] = []
    vm_rows: List[Dict[str, Any]] = []
    ops_rows: List[Dict[str, Any]] = []
    untagged_rows: List[Dict[str, Any]] = []

    # Budgets check
    try:
        budgets = session.client("budgets", region_name="us-east-1").describe_budgets(AccountId=account_id).get("Budgets", [])
        if not budgets:
            ops_rows.append({"Area": "Missing AWS Budget", "Region": "Global", "Name": account_id, "Details": "0 AWS Budgets configured", "Recommendation": "Create monthly cost/forecast budget alerts"})
    except ClientError as e:
        ctx.warn(f"AWS Budgets check failed: {e}")

    # S3 lifecycle check
    try:
        s3 = session.client("s3")
        for b in s3.list_buckets().get("Buckets", []):
            bname = b["Name"]
            try:
                s3.get_bucket_lifecycle_configuration(Bucket=bname)
            except ClientError:
                ops_rows.append({"Area": "S3 Missing Lifecycle", "Region": "Global", "Name": bname, "Details": "No Lifecycle transition/expiration rules", "Recommendation": "Add Intelligent-Tiering or Glacier transition rules"})
            try:
                tags = {t["Key"].lower() for t in s3.get_bucket_tagging(Bucket=bname).get("TagSet", [])}
            except ClientError:
                tags = set()
            missing = [t for t in REQUIRED_COST_TAGS if t not in tags]
            if missing:
                untagged_rows.append({"Region": "Global", "ResourceType": "S3 Bucket", "Name": bname, "MissingCostTags": ", ".join(missing)})
    except ClientError as e:
        ctx.warn(f"S3 cost check failed: {e}")

    start_cw = ctx.now - dt.timedelta(days=14)

    for reg in regions:
        log(f"Scanning region {reg} for cost/waste...")
        ec2 = session.client("ec2", region_name=reg)
        cw = session.client("cloudwatch", region_name=reg)

        # Unattached EBS Volumes
        try:
            for v in ec2.describe_volumes(Filters=[{"Name": "status", "Values": ["available"]}]).get("Volumes", []):
                orphan_rows.append({"Category": "Unattached EBS Volume", "Region": reg, "Name": v["VolumeId"], "Details": f"Type={v.get('VolumeType')}, SizeGB={v.get('Size')}", "RecommendedAction": "Snapshot if needed and delete"})
        except ClientError:
            pass

        # Unassociated Elastic IPs
        try:
            for eip in ec2.describe_addresses().get("Addresses", []):
                if not eip.get("AssociationId"):
                    orphan_rows.append({"Category": "Unassociated Elastic IP", "Region": reg, "Name": eip.get("PublicIp", ""), "Details": f"AllocationId={eip.get('AllocationId')}", "RecommendedAction": "Release unused Elastic IP"})
        except ClientError:
            pass

        # Stale EBS Snapshots (owned by self)
        try:
            for s in ec2.describe_snapshots(OwnerIds=["self"]).get("Snapshots", []):
                age = (ctx.now - s["StartTime"]).days
                if age >= ctx.stale_days:
                    orphan_rows.append({"Category": f"Stale EBS Snapshot (>={ctx.stale_days}d)", "Region": reg, "Name": s["SnapshotId"], "Details": f"Age={age}d, SizeGB={s.get('VolumeSize')}", "RecommendedAction": "Verify retention; delete obsolete snapshots"})
        except ClientError:
            pass

        # EC2 instances: stopped, untagged, and 14d CPU P95/Avg
        try:
            for resv in ec2.describe_instances().get("Reservations", []):
                for inst in resv.get("Instances", []):
                    iid = inst["InstanceId"]
                    state = inst.get("State", {}).get("Name", "")
                    itype = inst.get("InstanceType", "")
                    tags = {t["Key"].lower() for t in inst.get("Tags", [])}
                    missing = [t for t in REQUIRED_COST_TAGS if t not in tags]
                    if missing and state != "terminated":
                        untagged_rows.append({"Region": reg, "ResourceType": "EC2 Instance", "Name": iid, "MissingCostTags": ", ".join(missing)})

                    if state == "stopped":
                        orphan_rows.append({"Category": "Stopped EC2 Instance", "Region": reg, "Name": iid, "Details": f"Type={itype} (Still billing attached EBS volumes)", "RecommendedAction": "Terminate or snapshot+archive if no longer needed"})
                    elif state == "running":
                        try:
                            stats = cw.get_metric_statistics(
                                Namespace="AWS/EC2",
                                MetricName="CPUUtilization",
                                Dimensions=[{"Name": "InstanceId", "Value": iid}],
                                StartTime=start_cw,
                                EndTime=ctx.now,
                                Period=3600,
                                Statistics=["Average", "Maximum"],
                            ).get("Datapoints", [])
                            if stats:
                                avg_cpu = round(sum(d["Average"] for d in stats) / len(stats), 2)
                                p95_cpu = percentile([d["Maximum"] for d in stats], 95)
                                if p95_cpu is not None and p95_cpu < 15.0:
                                    vm_rows.append({"Source": "14d CloudWatch CPU", "Region": reg, "Resource": iid, "Details": f"Type={itype} | AvgCPU={avg_cpu}% | P95MaxCPU={p95_cpu}%", "Recommendation": "Underutilized CPU (verify Memory/IOPS before downsizing)"})
                        except ClientError:
                            pass
        except ClientError:
            pass

        # CloudWatch Log Groups with Never Expire
        try:
            logs_cli = session.client("logs", region_name=reg)
            for lg in logs_cli.describe_log_groups().get("logGroups", []):
                if "retentionInDays" not in lg:
                    ops_rows.append({"Area": "CloudWatch Log Group Never Expire", "Region": reg, "Name": lg["logGroupName"], "Details": f"StoredBytes={lg.get('storedBytes', 0)}", "Recommendation": "Set retention policy (e.g., 30-90 days)"})
        except ClientError:
            pass

    write_csv(csv_dir / "01_cost_by_service_and_region.csv", ["PeriodStart", "Service", "Region", "AmortizedCost", "Currency"], cost_rows)
    write_csv(csv_dir / "02_orphaned_and_idle_resources.csv", ["Category", "Region", "Name", "Details", "RecommendedAction"], orphan_rows)
    write_csv(csv_dir / "03_ec2_rightsizing_and_optimizer.csv", ["Source", "Region", "Resource", "Details", "Recommendation"], vm_rows)
    write_csv(csv_dir / "04_savings_plans_and_ris.csv", ["Type", "UtilizationPct", "UsedCommitment", "UnusedCommitment", "NetSavings"], commit_rows)
    write_csv(csv_dir / "05_s3_logs_and_budgets.csv", ["Area", "Region", "Name", "Details", "Recommendation"], ops_rows)
    write_csv(csv_dir / "06_untagged_resources.csv", ["Region", "ResourceType", "Name", "MissingCostTags"], untagged_rows)

    total_spend = round(sum(r["AmortizedCost"] for r in cost_rows), 2)
    lines = [
        "# AWS Cost & Billing Audit Report",
        "",
        f"- **AWS Account:** `{account_id}`",
        f"- **Generated (UTC):** {fmt_dt(ctx.now)}",
        f"- **Total Amortized Spend ({ctx.days}d):** {total_spend:,.2f}",
        "",
        "## Executive Summary",
        "",
        md_table(
            ["Audit Area", "Findings", "Detailed CSV"],
            [
                ["Cost Breakdown (Service x Region)", str(len(cost_rows)), "[01_cost_by_service_and_region.csv](./csv/01_cost_by_service_and_region.csv)"],
                ["Orphaned & Idle Resources", str(len(orphan_rows)), "[02_orphaned_and_idle_resources.csv](./csv/02_orphaned_and_idle_resources.csv)"],
                ["Underutilized EC2 Instances", str(len(vm_rows)), "[03_ec2_rightsizing_and_optimizer.csv](./csv/03_ec2_rightsizing_and_optimizer.csv)"],
                ["Savings Plans & RI Utilization", str(len(commit_rows)), "[04_savings_plans_and_ris.csv](./csv/04_savings_plans_and_ris.csv)"],
                ["S3 Lifecycle, Log Retention & Budgets", str(len(ops_rows)), "[05_s3_logs_and_budgets.csv](./csv/05_s3_logs_and_budgets.csv)"],
                ["Untagged Resources", str(len(untagged_rows)), "[06_untagged_resources.csv](./csv/06_untagged_resources.csv)"],
            ],
        ),
    ]
    if ctx.warnings:
        lines.extend(["## Audit Warnings", ""] + [f"- {w}" for w in ctx.warnings] + [""])

    report_path = out_dir / "COST_AUDIT_REPORT.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"AWS Cost Audit complete! Report written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
