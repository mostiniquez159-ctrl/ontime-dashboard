#!/usr/bin/env bash
set -euo pipefail
systemctl is-active ontime-dashboard.service
systemctl is-active actions.runner.* 2>/dev/null || true
python3 -m py_compile app.py
echo "OK: local service + syntax check done"
