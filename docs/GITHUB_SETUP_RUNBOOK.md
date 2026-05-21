# GitHub Setup Runbook (STD_15B §15B.6.8-15B.6.11)

## 1) Auth
```bash
gh auth login
```

## 2) Create repo and push
```bash
cd _runtime/repos/ontime-dashboard
bash scripts/bootstrap_github_repo.sh <github-org-or-user>
```

## 3) Configure environments and secrets
Create GitHub Environments:
- staging
- production

Required secrets/vars:
- DASHBOARD_HOST
- DASHBOARD_SERVICE_NAME
- STAGING_PORT=8081
- PROD_PORT=80
- ROLLBACK_REF_PATH

## 4) Branch protection
```bash
bash scripts/apply_branch_protection.sh <github-org-or-user>
```

## 5) Runner check
```bash
bash scripts/runner_health_check.sh
```
