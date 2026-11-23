#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -f .env ] || { echo "Нет .env — скопируйте .env.example в .env"; exit 1; }
exec python3 bot.py "$@"
