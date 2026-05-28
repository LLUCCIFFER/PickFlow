#!/usr/bin/env bash
# 片刻 — 启动脚本
#
# 第一次运行：自动创建 .venv 并装依赖
# 后续运行：直接启动
# 任何参数都会透传给 app.py（如 --port 8080 --no-browser）

set -e
cd "$(dirname "$0")/.."

VENV=".venv"
REQ="requirements.txt"
STAMP=".pic_selecter_deps.stamp"

# ---------- 找 python ----------
if command -v python3 &>/dev/null; then
  PY=python3
elif command -v python &>/dev/null; then
  PY=python
else
  echo "❌ 未找到 python3 / python。装一个 Python 3.10+ 再试：" >&2
  echo "   https://www.python.org/downloads/" >&2
  exit 1
fi

# ---------- 创建 venv ----------
if [ ! -d "$VENV" ]; then
  echo "▶ 创建虚拟环境：$VENV"
  "$PY" -m venv "$VENV"
  rm -f "$STAMP"
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

# ---------- 检查 / 安装依赖 ----------
# requirements.txt 比 stamp 新就重装；stamp 不存在也重装。
needs_install=0
if [ ! -f "$STAMP" ]; then
  needs_install=1
elif [ "$REQ" -nt "$STAMP" ]; then
  needs_install=1
fi

if [ "$needs_install" = "1" ]; then
  echo "▶ 安装依赖（首次或 requirements.txt 已更新）..."
  pip install -q --disable-pip-version-check -r "$REQ"
  touch "$STAMP"
fi

# ---------- 启动 ----------
echo "▶ 启动 pic_selecter..."
exec python app.py "$@"
