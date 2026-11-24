#!/usr/bin/env bash
# 把 docs/*.md 转成 docx 与 pdf，输出到 docs/_build/。
# 优先用 pandoc；没有则退到本机 venv 里的 pypandoc。
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p docs/_build

PYBIN="${PYBIN:-/root/venv-docx/bin/python}"
have_pandoc=0
command -v pandoc >/dev/null 2>&1 && have_pandoc=1
if [ "$have_pandoc" -eq 0 ] && [ ! -x "$PYBIN" ]; then
  echo "缺少 pandoc，且 $PYBIN 不可用；请安装 pandoc 或设置 PYBIN" >&2
  exit 1
fi

for f in docs/*.md; do
  [ -e "$f" ] || continue
  base=$(basename "$f" .md)
  if [ "$have_pandoc" -eq 1 ]; then
    pandoc "$f" -o "docs/_build/$base.docx" --resource-path=.
  else
    "$PYBIN" - "$f" "docs/_build/$base.docx" <<'PY'
import sys, pypandoc
pypandoc.convert_file(sys.argv[1], 'docx', outputfile=sys.argv[2], extra_args=['--resource-path=.'])
PY
  fi
  soffice --headless --convert-to pdf --outdir docs/_build "docs/_build/$base.docx" >/dev/null 2>&1
  echo "built $base"
done
