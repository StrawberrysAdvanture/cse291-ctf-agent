#!/usr/bin/env bash
# Render report.md -> report.pdf with the report's styling.
#
# Pipeline: pandoc (Markdown -> self-contained HTML, embedding figures as base64)
#           then headless Chrome (HTML -> PDF, honoring report.css @page + print rules).
#
# Requirements:
#   - pandoc           (brew install pandoc)
#   - Google Chrome    (for headless print-to-pdf)
#
# Usage:  cd report/phase1 && ./build-pdf.sh
set -euo pipefail

cd "$(dirname "$0")"                      # report/phase1/
SRC="report.md"
CSS="report.css"
HTML="$(mktemp -t phase1report).html"
OUT="report.pdf"

# Locate a Chrome/Chromium binary.
CHROME=""
for c in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium" \
  "$(command -v google-chrome || true)" \
  "$(command -v chromium || true)" \
  "$(command -v chromium-browser || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then CHROME="$c"; break; fi
done
if [ -z "$CHROME" ]; then
  echo "error: no Chrome/Chromium found for PDF rendering" >&2
  exit 1
fi

# 1) Markdown -> self-contained HTML (figures embedded, our CSS inlined).
pandoc "$SRC" -f gfm -t html5 --self-contained \
  --metadata title="Phase 1 Baseline Report" \
  -c "$CSS" -o "$HTML"

# 2) HTML -> PDF.
"$CHROME" --headless=new --disable-gpu --no-pdf-header-footer \
  --print-to-pdf="$OUT" "file://$HTML"

rm -f "$HTML"
echo "wrote $OUT"
