# GCP Cost & Billing Audit

Strictly read-only GCP Cost & FinOps audit using Application Default Credentials (`gcloud auth application-default login`), exporting CSVs and `COST_AUDIT_REPORT.md` (no JSON).

```powershell
pip install -r requirements.txt
gcloud auth application-default login
python audit_gcp_cost.py
```
