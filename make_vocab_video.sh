#!/usr/bin/env bash
# Wrapper for make_vocab_video.py - generate a vocabulary-drill video.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/make_vocab_video.py" "$@"
