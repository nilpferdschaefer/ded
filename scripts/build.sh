#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install --quiet pyinstaller
python3 -m PyInstaller --onefile --name ded-linux-amd64 ded.py
