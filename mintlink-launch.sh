#!/usr/bin/env bash
# Starts MintLink; keeps the terminal open if it exits with an error.
cd "$(dirname "$(readlink -f "$0")")" || exit 1
python3 mintlink.py "$@"
s=$?
if [ $s -ne 0 ]; then echo; read -rp "MintLink stopped (exit $s). Press Enter to close. "; fi
