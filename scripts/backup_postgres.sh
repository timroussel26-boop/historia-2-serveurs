#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL est requis" >&2
  exit 1
fi

OUT_DIR="${1:-./backups}"
mkdir -p "$OUT_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$OUT_DIR/historia-postgres-$STAMP.sql.gz"

pg_dump "$DATABASE_URL" | gzip > "$OUT_FILE"
echo "Backup PostgreSQL cree: $OUT_FILE"
