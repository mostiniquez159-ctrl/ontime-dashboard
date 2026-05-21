# ontime-dashboard

Product repository for onTime Admin dashboard (v3).

## Deploy model (STD_15B §15B.6)
- Edit only in feature branches
- PR -> staging deploy (8081) -> smoke gate 7/7 -> approval -> production deploy (80)
- Rollback via revert/tag

## Local run
```bash
DASHBOARD_PORT=8080 python3 app.py
```
