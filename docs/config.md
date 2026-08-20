# TokenSaver 配置说明

> 配置方式：环境变量（启动前设置）。MVP 阶段无配置文件。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TOKENSAVER_HOST` | `0.0.0.0` | 网关监听地址 |
| `TOKENSAVER_PORT` | `20130` | 网关监听端口 |
| `ROUTER_UPSTREAM_URL` | `http://localhost:20128/v1` | 9router 基地址 |
| `ROUTER_UPSTREAM_KEY` | `sk-your-9router-key` | 9router API key（必填，启动前 export） |
| `ROUTER_UPSTREAM_TIMEOUT` | `600` | 上游超时（秒），流式长响应建议 ≥300 |

## 档位 → 模型映射（代码内 TIER_MODEL_MAP）

| 档位 | 9router 模型 | 说明 |
|---|---|---|
| c0 | `jy/deepseek-v4-flash` | 最简单 → 最便宜 |
| c1 | `jy/deepseek-v4-flash-0731` | 简单 → 稍强 |
| c2 | `jy/deepseek-v4-pro` | 中等 → 主力强模型 |
| c3 | `zhipu/glm-5` | 最复杂 → 免费渠道（可换 `qz/grok-4.5` 等） |

映射后续如需可配置化（YAML/TOML），演进方向：`config.toml` 仿 OpenSquilla `[squilla_router]` 结构。

## 客户端配置

- base_url：`http://localhost:20130/v1`
- model：`auto`（智能路由）；显式模型名 = 透传

## 示例

```bash
export TOKENSAVER_PORT=20130
export ROUTER_UPSTREAM_URL=http://localhost:20128/v1
export ROUTER_UPSTREAM_KEY=sk-xxx
python3 gateway.py
```
