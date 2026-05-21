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
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git init
fi
git checkout -B main
git add .
git commit -m "chore: bootstrap ontime-dashboard repo with CI/CD" || true
if gh repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "Repo exists: $OWNER/$REPO"
else
  gh repo create "$OWNER/$REPO" --private --source=. --remote=origin --push
fi
if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "git@github.com:$OWNER/$REPO.git"
fi
git push -u origin main

git checkout -B "feature/bootstrap-initial"
git push -u origin "feature/bootstrap-initial"
echo "OK: repo initialized and branches pushed"
