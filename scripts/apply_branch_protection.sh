#!/usr/bin/env bash
set -euo pipefail
OWNER="${1:-}"
REPO="ontime-dashboard"
if [[ -z "$OWNER" ]]; then
  echo "usage: $0 <github-org-or-user>"
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh auth missing. Run: gh auth login"
  exit 2
fi
# Require PR reviews + block force push + require status checks
# Use gh api for branch protection settings
# Note: checks contexts can be adjusted after first workflow run
gh api \
  --method PUT \
  -H "Accept: application/vnd.github+json" \
  "/repos/$OWNER/$REPO/branches/main/protection" \
  -f required_pull_request_reviews.dismiss_stale_reviews=true \
  -f required_pull_request_reviews.required_approving_review_count=1 \
  -f enforce_admins=true \
  -f required_linear_history=true \
  -f allow_force_pushes=false \
  -f allow_deletions=false \
  -f restrictions= \
  -f required_status_checks.strict=true \
  -f required_status_checks.contexts[]="deploy-staging" \
  -f required_status_checks.contexts[]="deploy-production"
echo "OK: main branch protection applied"
