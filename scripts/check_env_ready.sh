#!/usr/bin/env bash
set -euo pipefail

env_file="${1:?environment file path is required}"

if [[ ! -r "$env_file" ]]; then
  echo "environment file is not readable: $env_file" >&2
  exit 1
fi

if grep -Eq '^BINANCE_API_(KEY|SECRET)=FILL_' "$env_file"; then
  echo "Binance API credentials are still placeholders in $env_file" >&2
  exit 1
fi
