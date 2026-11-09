#!/bin/sh
# Build dist/bq1.zip for the Glue job. The job does `from bq1.features import build_features`,
# so the zip must contain bq1/__init__.py and bq1/features.py (not src/bq1/...). Run from anywhere.
set -e
ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SRC="$ROOT/src/bq1"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/bq1" "$ROOT/dist"
cp "$SRC/__init__.py" "$SRC/features.py" "$STAGE/bq1/"
rm -f "$ROOT/dist/bq1.zip"
(cd "$STAGE" && zip -q -X "$ROOT/dist/bq1.zip" bq1/__init__.py bq1/features.py)
unzip -l "$ROOT/dist/bq1.zip"
