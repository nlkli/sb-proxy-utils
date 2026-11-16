#!/usr/bin/env bash
set -euo pipefail

sudo systemctl restart sing-box
sleep 2

if systemctl is-active --quiet sing-box; then
    echo "sing-box restarted OK"
else
    echo "sing-box failed to start" >&2
    sudo journalctl -u sing-box -n 30 --no-pager >&2
    exit 1
fi
