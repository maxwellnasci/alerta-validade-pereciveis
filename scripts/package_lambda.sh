#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$ROOT/build/lambda_function.zip}"
mkdir -p "$(dirname "$OUT")"
(cd "$ROOT/src" && zip -q -X "$OUT" lambda_function.py)
echo "Pacote criado: $OUT"
