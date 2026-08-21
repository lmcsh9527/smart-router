#!/usr/bin/env bash
# TokenSaver 一键安装器 —— 任何 macOS / Linux 用户开箱即用
#
# 用法：
#   bash install.sh              # 安装依赖 + 初始化配置
#   bash install.sh --launchd    # 追加：macOS 开机自启 + 崩溃保活
#   bash install.sh --doctor     # 装完立即健康自检
# 环境变量：TOKENSAVER_PYTHON 可指定 Python 解释器
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "══════════════════════════════════════"
echo " TokenSaver 智能路由网关 · 安装器"
echo "══════════════════════════════════════"

LAUNCHD=0; DOCTOR=0
for arg in "$@"; do
  case "$arg" in
    --launchd) LAUNCHD=1 ;;
    --doctor)  DOCTOR=1 ;;
    --help|-h) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg"; exit 1 ;;
  esac
done

# ── 1. Python 检测（≥3.10，含常见安装路径）──────────────
ver_ok() { "$1" -c 'import sys; raise SystemExit(0 if (sys.version_info[0],sys.version_info[1])>=(3,10) else 1)' 2>/dev/null; }
PY="${TOKENSAVER_PYTHON:-}"
if [ -n "$PY" ] && ! ver_ok "$PY"; then PY=""; fi
if [ -z "$PY" ]; then
  for cand in python3.13 python3.12 python3.11 python3.10 python3 \
      /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
      /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
      /usr/bin/python3; do
    if [[ "$cand" == */* ]]; then
      if [ -x "$cand" ] && ver_ok "$cand"; then PY="$cand"; break; fi
      continue
    fi
    command -v "$cand" >/dev/null 2>&1 || continue
    if ver_ok "$cand"; then PY="$cand"; break; fi
  done
fi
[ -z "$PY" ] && { echo "❌ 未找到 Python ≥3.10（可用 TOKENSAVER_PYTHON=/路径/python3.x 指定）"; exit 1; }
echo "✅ Python: $($PY --version 2>&1)"

# ── 2. 虚拟环境 + 依赖 ──────────────────────────────────
if [ ! -x ".venv/bin/python" ]; then
  echo "→ 创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
VPY="$SCRIPT_DIR/.venv/bin/python"
echo "→ 安装依赖（fastapi / uvicorn / httpx）..."
"$VPY" -m pip install -q --upgrade pip
"$VPY" -m pip install -q -r requirements.txt
echo "✅ 依赖就绪"

# ── 3. 初始化配置（绝不覆盖已有配置）────────────────────
if [ ! -f "models_pool.json" ]; then
  cp models_pool.example.json models_pool.json
  echo "✅ 已从模板生成 models_pool.json —— 到后台添加你的渠道即可"
else
  echo "✅ models_pool.json 已存在，跳过（不覆盖）"
fi

# ── 4. launchd 常驻（可选，仅 macOS）───────────────────
if [ "$LAUNCHD" -eq 1 ]; then
  if [[ "$(uname)" != "Darwin" ]]; then
    echo "⚠️ 非 macOS，跳过 launchd（Linux 请自行配 systemd 单元）"
  else
    PLIST="$HOME/Library/LaunchAgents/com.tokensaver.gateway.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<XML
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tokensaver.gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VPY</string>
    <string>$SCRIPT_DIR/gateway.py</string>
  </array>
  <key>WorkingDirectory</key><string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$SCRIPT_DIR/gateway.log</string>
  <key>StandardErrorPath</key><string>$SCRIPT_DIR/gateway.log</string>
</dict>
</plist>
XML
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null || launchctl load "$PLIST"
    echo "✅ launchd 已注册：开机自启 + 崩溃保活"
    echo "   （$PLIST）"
  fi
fi

# ── 5. 完成指引 ─────────────────────────────────────────
cat <<'EOF'

────────────────────────────────────────
🎉 安装完成！接下来三步：

1) 启动（二选一）
   前台试跑：  .venv/bin/python gateway.py
   后台常驻：  bash install.sh --launchd   （macOS）

2) 打开后台添加你的模型渠道
   http://localhost:20130/admin
   → 模型池 → 添加渠道（Base URL + Key → 获取模型 → 入池 → 设档位）

3) 客户端接入（任何 OpenAI 兼容工具）
   Base URL: http://localhost:20130/v1
   Model:    auto          ← 自动判档路由
   API Key:  随意填（本地网关不校验）

健康自检：bash scripts/doctor.sh
DSH 对接：docs/dsh-integration.md
配置说明：docs/config.md
────────────────────────────────────────
EOF

if [ "$DOCTOR" -eq 1 ]; then
  echo "→ 运行健康自检 ..."
  bash scripts/doctor.sh || true
fi
