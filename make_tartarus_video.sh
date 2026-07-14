#!/usr/bin/env bash
# Wrapper for make_tartarus_video.py - generate a vocabulary-drill video.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/utils/make_tartarus_video.py" "$@"
