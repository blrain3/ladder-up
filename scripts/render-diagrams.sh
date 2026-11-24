#!/usr/bin/env bash
# 把 diagrams/*.puml 渲染为 PNG。需要 plantuml（含 Java）。
set -euo pipefail
cd "$(dirname "$0")/../diagrams"
if ! command -v plantuml >/dev/null 2>&1; then
  echo "缺少 plantuml，请先安装（apt install plantuml 或下载 jar）" >&2
  exit 1
fi
shopt -s nullglob
files=(src/*.puml)
if [ ${#files[@]} -eq 0 ]; then
  echo "diagrams/src/ 下没有 .puml 文件"
  exit 0
fi
plantuml -tpng -o .. "${files[@]}"
echo "已渲染 ${#files[@]} 张图到 diagrams/"
