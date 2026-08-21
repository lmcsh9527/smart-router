#!/usr/bin/env bash
# TokenSaver 健康自检：网关 / 模型池 / 档位覆盖 / 密钥文件 / 上游可达 / 常驻服务
#
# 用法：bash scripts/doctor.sh
# 环境变量：TOKENSAVER_HOST（默认 127.0.0.1）、TOKENSAVER_PORT（默认 20130）
set -uo pipefail

HOST="${TOKENSAVER_HOST:-127.0.0.1}"
PORT="${TOKENSAVER_PORT:-20130}"
BASE="http://$HOST:$PORT"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RES="$(mktemp)"

main() {
  echo "════════════════════════════════════"
  echo " TokenSaver Doctor · $BASE"
  echo "════════════════════════════════════"

  # 1) 网关在线
  MODELS="$(curl -s --max-time 5 "$BASE/v1/models" 2>/dev/null || true)"
  if echo "$MODELS" | grep -q '"data"'; then
    N="$(echo "$MODELS" | python3 -c 'import json,sys;print(len(json.load(sys.stdin).get("data",[])))' 2>/dev/null || echo '?')"
    echo "✅ 网关在线（对外模型 $N 个）"
  else
    echo "❌ 网关无响应 —— 先启动：.venv/bin/python gateway.py（或 bash install.sh --launchd）"
  fi

  # 2) 模型池 / 档位覆盖 / 密钥文件（交给 python 细查）
  python3 - "$DIR" <<'PY'
import json, os, stat, sys
d = sys.argv[1]
p = os.path.join(d, "models_pool.json")
if not os.path.exists(p):
    print("❌ 缺 models_pool.json —— 运行 bash install.sh 初始化"); raise SystemExit
try:
    pool = json.load(open(p, encoding="utf-8")).get("models", [])
except Exception as e:
    print(f"❌ models_pool.json 解析失败: {e}"); raise SystemExit
en = [m for m in pool if m.get("enabled", True)]
print(f"✅ 模型池 {len(pool)} 个条目（启用 {len(en)}）")
for t in ("c0", "c1", "c2", "c3"):
    same = sorted((m for m in en if t in (m.get("tiers") or [])),
                  key=lambda m: int(m.get("priority", 100)))
    if same:
        print("✅ 档位 %s: %s" % (t, " → ".join(str(m.get("model", "?")) for m in same)))
    else:
        print("❌ 档位 %s: 无启用模型（该档请求会失败）" % t)
for m in en:
    kf = m.get("api_key_file")
    if not kf:
        continue
    fp = os.path.join(d, kf)
    if not os.path.exists(fp) or os.path.getsize(fp) == 0:
        print("❌ %s: key 文件缺失或为空：%s" % (m.get("id"), kf))
    else:
        if stat.S_IMODE(os.stat(fp).st_mode) & 0o077:
            print("⚠️ %s 权限过宽，建议 chmod 600" % kf)
for u in sorted({(m.get("base_url") or "").rstrip("/") for m in en if m.get("base_url")}):
    print("UPSTREAM %s" % u)
PY

  # 3) 上游可达性（任何 HTTP 码都算可达，超时才算失败）
  grep '^UPSTREAM ' "$RES" 2>/dev/null | cut -d' ' -f2 | while read -r u; do
    [ -z "$u" ] && continue
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 "$u/models" 2>/dev/null)"
    if [ -z "$code" ] || [ "$code" = "000" ]; then
      echo "❌ 上游不可达: $u"
    else
      echo "✅ 上游可达(HTTP $code): $u"
    fi
  done

  # 4) 常驻服务（仅 macOS）
  if [[ "$(uname)" == "Darwin" ]]; then
    if launchctl list 2>/dev/null | grep -q "tokensaver"; then
      echo "✅ launchd 常驻已注册（开机自启 + 崩溃保活）"
    else
      echo "⚠️ 未注册 launchd 常驻（重启电脑后需手动启动）—— bash install.sh --launchd"
    fi
  fi
}

main | tee "$RES"
F="$(grep -c '❌' "$RES")"; W="$(grep -c '⚠️' "$RES")"
echo "════════════════════════════════════"
echo " 结果：${F:-0} 项失败 / ${W:-0} 项提醒"
echo "════════════════════════════════════"
rm -f "$RES"
[ "${F:-0}" -eq 0 ] && exit 0 || exit 1
