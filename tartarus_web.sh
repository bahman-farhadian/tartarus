#!/usr/bin/env bash
# Wrapper for tartarus_web.py - serves the Tartarus web UI on
# http://127.0.0.1:9999 (localhost only).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/tartarus_web.py" "$@"
