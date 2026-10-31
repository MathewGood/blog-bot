#!/bin/zsh
# blogbot 웹 GUI 실행 파일 — Finder에서 더블클릭하거나 터미널에서 ./blogbot.command
cd "$(dirname "$0")"

if [ ! -x ".venv/bin/python" ]; then
  echo "가상환경이 없습니다. 먼저 설치하세요:"
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

exec .venv/bin/python -m blogbot web --lan
