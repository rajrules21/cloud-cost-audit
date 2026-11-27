# Multi-Cloud Cost & FinOps Auditor

A modular, **strictly read-only** Cloud Cost & Billing Audit toolkit for identifying wasted spend, orphaned resources, right-sizing opportunities, licensing gaps, and missing budget guardrails across multi-cloud environments.

All findings are exported as **CSV files** (no JSON) alongside an executive **`COST_AUDIT_REPORT.md`** with clickable links to every dataset.

---

## Multi-Cloud Coverage

| Cloud Provider | Status | Scope Covered |
| :--- | :--- | :--- |
| **Microsoft Azure** | **Implemented** ([`azure/`](./azure)) | Cost Management (`AmortizedCost` by Resource Group & Service), Orphaned Disks / Public IPs / Snapshots / Empty App Service Plans / Unused Load Balancers, Stopped-not-Deallocated VMs, 14-day VM CPU P95/Avg Utilization, Azure Advisor Cost Recommendations, Azure Hybrid Benefit gaps, Storage redundancy & lifecycle policies, Log Analytics retention/caps, Subscription Budgets, and Untagged Resources |
| **Amazon Web Services (AWS)** | **Implemented** ([`aws/`](./aws)) | Cost Explorer (`AmortizedCost` by Service & Region), unattached EBS volumes, unassociated Elastic IPs, stale snapshots, stopped EC2 instances, 14-day CloudWatch CPU (Avg & P95), Savings Plans/RI utilization, S3 lifecycle, CloudWatch Log Group retention, AWS Budgets & untagged resources |
| **Google Cloud Platform (GCP)** | **Implemented** ([`gcp/`](./gcp)) | Cloud Billing account & project linkage, unattached Persistent Disks, unused Static External IPs, stale snapshots, stopped VMs, 14-day Cloud Monitoring CPU (Avg & P95), Committed Use Discounts (CUDs), GCS lifecycle, Billing Budgets & unlabeled resources |

---

## Quick Start (Azure)

```powershell
cd azure
pip install -r requirements.txt

# Authenticate interactively in browser (no Azure CLI required) and audit all subscriptions
python audit_azure_cost.py --interactive
```

### Recommended Read-Only Permissions
- `Reader` + `Monitoring Reader` + `Cost Management Reader` at the Tenant Root Group or target subscriptions.
- *(Optional)* `Reservations Reader` / `Savings Plan Reader` for commitment utilization.

### Output (`./cost_audit_output/`)
- `COST_AUDIT_REPORT.md` — Executive summary, quick-win counts, permission/coverage notes, and links to all CSVs.
- `csv/01_cost_by_rg_and_service.csv` — Amortized spend breakdown by Subscription, Resource Group, and Service.
- `csv/02_orphaned_and_idle_resources.csv` — Unattached disks, stopped-not-deallocated VMs, unattached Public IPs, stale snapshots, empty App Service Plans, and unused Load Balancers.
- `csv/03_vm_rightsizing_and_advisor.csv` — 14-day VM CPU (Avg & P95) metrics + Azure Advisor Cost recommendations.
- `csv/04_licensing_and_commitments.csv` — Azure Hybrid Benefit gaps and Reservation/Savings Plan status.
- `csv/05_storage_logs_and_budgets.csv` — Storage redundancy/lifecycle, Log Analytics caps/retention, and Subscription Budget coverage.
- `csv/06_untagged_resources.csv` — Resources missing cost-attribution tags (`Owner`, `Environment`, `CostCenter`, `Project`).
