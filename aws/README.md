# AWS Cost & Billing Audit

Strictly read-only AWS Cost & FinOps audit exporting CSVs and `COST_AUDIT_REPORT.md` (no JSON).

```powershell
pip install -r requirements.txt
python audit_aws_cost.py
# Or specify an AWS CLI profile:
python audit_aws_cost.py --profile my-billing-profile --days 30
```
