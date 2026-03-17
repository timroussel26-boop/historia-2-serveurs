#!/usr/bin/env bash
set -euo pipefail

DB_FILE="${1:-./historia_inventory.db}"
OUT_DIR="${2:-./backups}"

if [[ ! -f "$DB_FILE" ]]; then
  echo "Base SQLite introuvable: $DB_FILE" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
RAW_FILE="$OUT_DIR/historia-sqlite-$STAMP.db"
GZ_FILE="$RAW_FILE.gz"

cp "$DB_FILE" "$RAW_FILE"
gzip "$RAW_FILE"

echo "Backup SQLite cree: $GZ_FILE"
