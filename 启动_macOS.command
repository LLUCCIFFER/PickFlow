#!/usr/bin/env bash
# 片刻 · Mac 启动器
#
# 双击运行：自动装 Python + 依赖 + 检查更新 + 启动应用 + 自动开浏览器
# 没装过 Python 也没关系，会用 uv 自动下载一个独立的 Python。
#
# 第一次提示「无法打开，因为它来自身份不明的开发者」时：
#   右键（或按住 control 点击）→ 打开 → 在弹窗里再点「打开」即可，
#   之后双击就能直接运行。

set -e
export LC_ALL="${LC_ALL:-en_US.UTF-8}"
export LANG="${LANG:-en_US.UTF-8}"

cd "$(dirname "$0")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  片刻 · 启动器"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ---- 1. 找 / 装 uv（Python 工具链管理器） ----
find_uv() {
  if command -v uv &>/dev/null; then
    echo "uv"
    return
  fi
  for cand in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [ -x "$cand" ]; then
      echo "$cand"
      return
    fi
  done
}

UV="$(find_uv || true)"
if [ -z "$UV" ]; then
  echo ""
  echo "[首次准备] 正在下载 uv（Python 工具链，~30MB）..."
  echo "  这一步只在第一次运行做，之后秒过。"
  if ! command -v curl &>/dev/null; then
    echo ""
    echo "❌ 系统没有 curl 命令，无法下载 uv。"
    echo "   请打开「终端」执行：xcode-select --install"
    echo "   装完再双击本启动器。"
    echo ""
    read -n 1 -s -r -p "按任意键退出..."
    exit 1
  fi
  # 不加 -s，保留 curl 进度输出，让用户看到下载在动
  curl -LSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  UV="$(find_uv || true)"
  if [ -z "$UV" ]; then
    echo ""
    echo "❌ uv 安装失败。"
    echo "   常见原因：网络不通畅（astral.sh 走海外 CDN），请稍后重试。"
    echo "   或手动执行：curl -LSf https://astral.sh/uv/install.sh | sh"
    echo ""
    read -n 1 -s -r -p "按任意键退出..."
    exit 1
  fi
  echo "  ✓ uv 安装完成"
fi

# 把 uv 自带的 Python 加进 PATH，确保后续 launcher.py 能调到
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# ---- 2. 用 uv 跑 launcher.py（uv 自动管理 Python 版本） ----
# 不加 --quiet：让 uv 下载 Python 的进度直接给用户看
# --no-project 防止 uv 误把当前目录当 uv 项目去解析 pyproject.toml
echo ""
echo "正在准备 Python 环境并启动 launcher..."
exec "$UV" run --no-project --python ">=3.10" -- python scripts/launcher.py
