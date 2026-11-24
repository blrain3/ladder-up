#!/usr/bin/env bash
# 把 docs/**/*.md 转成 docx 与 pdf。需要 pandoc、LibreOffice、中文字体。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p docs/_build
for f in docs/*.md; do
  [ -e "$f" ] || continue
  base=$(basename "$f" .md)
  pandoc "$f" -o "docs/_build/$base.docx" --resource-path=.
  soffice --headless --convert-to pdf --outdir docs/_build "docs/_build/$base.docx" >/dev/null 2>&1
  echo "built $base"
done
