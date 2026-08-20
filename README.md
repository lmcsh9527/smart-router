# TokenSaver / Smart Router

OpenAI 兼容的智能路由省钱网关——自动判断问题复杂度，简单问题走便宜模型、复杂问题走高效模型，最复杂任务自动切到免费渠道，多档位降级链保障可用性，降低模型使用成本。

## 功能清单

- [x] 智能路由：SquillaRouter 自动判档（c0-c3），简单→便宜模型、复杂→高效模型
- [x] OpenAI 兼容接口：`POST /v1/chat/completions`（非流式 + SSE 流式）
- [x] 升级词检测：用户明示"用最好的模型/最高思考能力"→ 强制最高档
- [x] 显式模型透传：客户端指定模型名时直接转发（不走路由）
- [x] 多档位降级链：上游失败自动降级，保证请求能出结果
- [x] 免费渠道优先（c3 最复杂任务 → 免费强模型，如智谱 glm-5）
- [ ] 多 key 轮询（由 9router 提供，本网关透传）
- [ ] RTK 上下文压缩（后续接入 9router RTK）

## 快速开始

> 前置：本地 9router 已跑通（`http://localhost:20128/v1`）；opensquilla 已装（含 SquillaRouter 模型包）；设置 `ROUTER_UPSTREAM_KEY` 环境变量。

```bash
cd tokensaver
export ROUTER_UPSTREAM_KEY=sk-your-9router-key   # 必填

# 1. 依赖确认（fastapi 需装进 opensquilla venv，见 docs/faq.md）
# 2. 启动网关（首次会加载路由模型，约数秒）
python3 gateway.py
#   或 uvicorn gateway:app --host 0.0.0.0 --port 20130

# 3. 冒烟测试
curl http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1等于几"}]}'

# 流式
curl -N http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"写一个B树实现"}],"stream":true}'
```

给 DSH / Hermes / 任何 OpenAI 兼容客户端配置：`base_url=http://localhost:20130/v1`，模型填 `auto`。

## 档位 → 模型映射

| 档位 | 判定 | 模型 | 成本 |
|---|---|---|---|
| c0 | 极简单（置信度高） | `jy/deepseek-v4-flash` | 最便宜 |
| c1 | 简单 | `jy/deepseek-v4-flash-0731` | 便宜 |
| c2 | 中等 | `jy/deepseek-v4-pro` | 主力 |
| c3 | 最复杂 / 升级词命中 | `zhipu/glm-5`（免费） | 免费 |

> c3 免费渠道可替换：`qz/grok-4.5`（轻舟）、`md/deepseek-ai/DeepSeek-V4-Pro`（魔搭）等，改 `TIER_MODEL_MAP` 即可。

## 降级策略（可用性保障）

高档模型推理慢或上游不稳定时，网关按降级链自动降级，保证请求能出结果：

```
zhipu/glm-5 → jy/deepseek-v4-pro → jy/deepseek-v4-flash-0731 → jy/deepseek-v4-flash
jy/deepseek-v4-pro → jy/deepseek-v4-flash-0731 → jy/deepseek-v4-flash
jy/deepseek-v4-flash-0731 → jy/deepseek-v4-flash
```

- 触发条件：连接失败 / 上游 5xx / 首字节等待超时（默认 90s，`ROUTER_UPSTREAM_READ_TIMEOUT` 可调）。
- 降级后响应 `x_tokensaver` 里 `upstream_model` 是实际使用模型，另有 `degraded_from` 记录原始判定模型；日志打 `ROUTE_DEGRADED`。
- 流式一旦开始输出不降级（只能透传）。

## 实测验证（2026-08-20）

| 场景 | 请求 | 路由结果 | 响应 |
|---|---|---|---|
| 简单 | `1+1等于几` | c0 → `jy/deepseek-v4-flash` | 200，`content="2"` |
| 复杂 | `设计分布式任务调度系统架构` | c3 → `zhipu/glm-5`（免费） | 200，返回架构设计 |
| 升级词 | `用最好的模型深度分析...` | c3 → `zhipu/glm-5`（免费） | 200 |
| 流式 | `写一个B树实现` + `stream:true` | c0 → `jy/deepseek-v4-flash` | 200，SSE 流式 |

## 架构

```
客户端 (DSH/Hermes/Cursor/...) → TokenSaver 网关 (:20130) → SquillaRouter.classify() 判档
       → 档位映射模型 → 9router (:20128) → 机缘/智谱/轻舟等渠道
```

详见 [docs/design.md](./docs/design.md)、[docs/config.md](./docs/config.md)、[docs/faq.md](./docs/faq.md)。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
