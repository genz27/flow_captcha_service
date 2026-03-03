#!/usr/bin/env bash
set -euo pipefail

XVFB_WHD="${XVFB_WHD:-1920x1080x24}"
DISPLAY="${DISPLAY:-:99}"
export DISPLAY

Xvfb "$DISPLAY" -screen 0 "$XVFB_WHD" -ac +extension RANDR >/tmp/xvfb.log 2>&1 &
fluxbox >/tmp/fluxbox.log 2>&1 &

exec python main.py
