#!/usr/bin/env bash
set -euo pipefail

SRC="config.json"
cp "$SRC" /etc/sing-box/ru-pub-list-ex.json
echo "copied $SRC -> /etc/sing-box/ru-pub-list-ex.json"
