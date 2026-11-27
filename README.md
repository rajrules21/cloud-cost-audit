# Multi-Cloud Cost & FinOps Auditor

A modular, **strictly read-only** Cloud Cost & Billing Audit toolkit for identifying wasted spend, orphaned resources, right-sizing opportunities (using **Avg + P95** utilization), licensing/commitment gaps, and missing budget guardrails across **Azure, AWS, and GCP**.

All findings are exported as **CSV files** (no JSON) alongside an executive **`COST_AUDIT_REPORT.md`** with clickable links to every dataset.

---

## Multi-Cloud Coverage

| Cloud Provider | Module | Scope Covered |
| :--- | :--- | :--- |
| **Microsoft Azure** | [`azure/`](./azure) | Cost Management (`AmortizedCost` by Resource Group & Service), Orphaned Disks / Public IPs / Snapshots / Empty App Service Plans / Unused Load Balancers, Stopped-not-Deallocated VMs, 14-day VM CPU (Avg & P95), Azure Advisor Cost Recommendations, Azure Hybrid Benefit gaps, Storage redundancy & lifecycle policies, Log Analytics retention/caps, Subscription Budgets, and Untagged Resources |
| **Amazon Web Services (AWS)** | [`aws/`](./aws) | Cost Explorer (`AmortizedCost` by Service & Region), unattached EBS volumes, unassociated Elastic IPs, stale snapshots, stopped EC2 instances, 14-day CloudWatch CPU (Avg & P95), Savings Plans & RI utilization, S3 lifecycle, CloudWatch Log Group retention, AWS Budgets, and Untagged Resources |
| **Google Cloud Platform (GCP)** | [`gcp/`](./gcp) | Cloud Billing account & project linkage, unattached Persistent Disks, unused Static External IPs, stale snapshots, stopped VMs, 14-day Cloud Monitoring CPU (Avg & P95), Committed Use Discounts (CUDs), GCS lifecycle, Billing Budgets, and Unlabeled Resources |

---

## Repository Structure

```text
cloud-cost-audit/
├── README.md
├── LICENSE
├── .gitignore
├── azure/
│   ├── audit_azure_cost.py     # Azure Cost Management, orphans, VM P95 CPU, AHB & budgets
│   ├── billing_client.py       # Shared read-only ARM / Resource Graph / Cost Management client
│   └── requirements.txt
├── aws/
│   ├── audit_aws_cost.py       # AWS Cost Explorer, orphans, CloudWatch P95 CPU, SP/RI & budgets
│   ├── aws_billing_client.py   # Shared read-only boto3 billing & metric helpers
│   ├── requirements.txt
│   └── README.md
└── gcp/
    ├── audit_gcp_cost.py       # GCP Billing, orphans, Cloud Monitoring P95 CPU, CUDs & budgets
    ├── gcp_billing_client.py   # Shared read-only GCP REST client (ADC)
    ├── requirements.txt
    └── README.md
```

---

## Quick Start

### 1. Microsoft Azure ([`azure/`](./azure))
- **Permissions:** `Reader` + `Monitoring Reader` + `Cost Management Reader` *(optional: `Reservations Reader`)*
```powershell
cd azure
pip install -r requirements.txt
python audit_azure_cost.py --interactive
```

### 2. Amazon Web Services ([`aws/`](./aws))
- **Permissions:** `ReadOnlyAccess` + `ce:GetCostAndUsage` + `ce:GetSavingsPlansUtilization` + `ce:GetReservationUtilization` + `budgets:ViewBudget`
```powershell
cd aws
pip install -r requirements.txt
python audit_aws_cost.py --profile my-billing-profile
```

### 3. Google Cloud Platform ([`gcp/`](./gcp))
- **Permissions:** `roles/viewer` + `roles/billing.viewer` + `roles/monitoring.viewer`
```powershell
cd gcp
pip install -r requirements.txt
gcloud auth application-default login
python audit_gcp_cost.py
```

---

## Safety Guarantee
- **No Auto-Deletion:** Scripts never delete, stop, resize, or modify any cloud resource. All findings are exported to CSV for owner verification and controlled cleanup.
- `.gitignore` excludes `cost_audit_output*/`, `aws_cost_output*/`, `gcp_cost_output*/`, `*.csv`, and `.env` files.
