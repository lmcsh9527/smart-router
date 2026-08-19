# TokenSaver

OpenAI 兼容的智能路由省钱网关——自动判断问题复杂度，简单问题走便宜模型、复杂问题走高效模型，多 key 轮询 + 上下文压缩，降低模型使用成本。

## 功能清单

- [x] 智能路由：SquillaRouter 自动判档（c0-c3），简单→便宜模型、复杂→高效模型
- [x] OpenAI 兼容接口：`POST /v1/chat/completions`（非流式 + SSE 流式）
- [ ] 多 key 轮询（待开发）
- [ ] RTK 上下文压缩（待开发）
- [ ] 免费渠道优先（待开发）

## 快速开始

> 前置：本地 9router 已跑通（`http://localhost:20128/v1`）；opensquilla 已装（含 SquillaRouter 模型包）。

```bash
cd "/Volumes/Data 1/deepseek harness/01-工程部/TokenSaver"

# 1. 依赖确认（fastapi 需装进 opensquilla venv，见 docs/faq.md）
# 2. 启动网关（首次会加载路由模型，约数秒）
/Users/hui/.local/share/uv/tools/opensquilla/bin/python gateway.py
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

给 DSH / Hermes 配置：`base_url=http://localhost:20130/v1`，模型填 `auto`。

## 降级策略（可用性保障）

高档模型（如 pro）推理慢或上游不稳定时，网关按降级链自动降级，保证请求能出结果：

```
jy/deepseek-v4-pro → jy/deepseek-v4-flash-0731 → jy/deepseek-v4-flash
jy/deepseek-v4-flash-0731 → jy/deepseek-v4-flash
```

- 触发条件：连接失败 / 上游 5xx / 首字节等待超时（默认 90s，`ROUTER_UPSTREAM_READ_TIMEOUT` 可调）。
- 降级后响应 `x_tokensaver` 里 `upstream_model` 是实际使用模型，另有 `degraded_from` 记录原始判定模型；日志打 `ROUTE_DEGRADED`。
- 流式一旦开始输出不降级（只能透传）。

## 实测验证（2026-08-19）

| 场景 | 请求 | 路由结果 | 响应 |
|---|---|---|---|
| 简单 | `1+1等于几` | c0 → `jy/deepseek-v4-flash` | 200，`content="2。"` |
| 复杂 | `设计分布式任务调度系统架构` + `max_tokens:200` | c3 → `jy/deepseek-v4-pro` | 200，返回架构设计（~11s） |
| 流式 | `用一句话解释什么是路由网关` + `stream:true` | c0 → `jy/deepseek-v4-flash` | 200，SSE 129 行 chunk |

> 注：9router 的 pro 模型无 max_tokens 时推理很长（>90s 首字节），网关会触发降级链；这是上游行为，非网关缺陷。

## 架构

```
客户端 → TokenSaver 网关 (:20130) → SquillaRouter.classify() 判档
       → 档位映射模型 → 9router (:20128) → 各渠道
```

详见 [docs/design.md](./docs/design.md)、[docs/config.md](./docs/config.md)、[docs/faq.md](./docs/faq.md)。

## 许可证

MIT，见 [LICENSE](./LICENSE)。
