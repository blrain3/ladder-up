#!/usr/bin/env bash
# 提交前守卫：禁止在 main 分支直接提交。
# 安装：make hooks（写入 .git/hooks/pre-commit）
set -u
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
if [ "$branch" = "main" ]; then
  cat >&2 <<'MSG'
[拒绝提交] 当前在 main 分支。
规范：main 只接收合并，开发一律在 feat/<切片> 分支上进行。
  git switch -c feat/s4-training
MSG
  exit 1
fi
exit 0
