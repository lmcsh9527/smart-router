# TokenSaver 架构设计

> 状态：MVP 已实现核心链路（v0.1.0，2026-08-19）

## 一句话架构

```
客户端 (DSH / Hermes / OpenAI SDK)
        │  POST /v1/chat/completions  (model=auto)
        ▼
TokenSaver 网关 (FastAPI, :20130)
        │  提取最后一条 user 消息
        ▼
SquillaRouter.classify()  (V4Phase3Strategy, 四档 c0-c3)
        │  tier + confidence + difficulty
        ▼
档位 → 模型映射  (c0/c1→flash, c2→flash-0731, c3→pro)
        ▼
9router (http://localhost:20128/v1) → 各渠道模型
        │  OpenAI 兼容响应（SSE 流式 / JSON 透传）
        ▼
客户端
```

## 接口定义

### 网关暴露

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI 兼容聊天补全，`model=auto` 触发智能路由 |
| GET | `/v1/models` | 列出可用模型（auto + 映射后模型） |
| GET | `/healthz` | 健康检查（含路由器可用性） |

### 路由判定

- 输入：最后一条 `role=user` 消息文本（支持字符串与多模态分段数组）
- 引擎：`V4Phase3Strategy.classify(message, valid_tiers=["c0","c1","c2","c3"])`
- 返回：`(tier, confidence, source, extra)`，其中 `extra` 含 `route_class` / `difficulty` / `thinking_mode` / `prompt_policy`
- 映射：`c0/c1 → jy/deepseek-v4-flash`，`c2 → jy/deepseek-v4-flash-0731`，`c3 → jy/deepseek-v4-pro`

### 转发

- 上游：`POST {ROUTER_UPSTREAM_URL}/chat/completions`
- 请求体：透传客户端 body，仅替换 `model` 字段为路由结果
- 鉴权：`Authorization: Bearer {ROUTER_UPSTREAM_KEY}`
- 流式：`stream=true` 时字节透传 SSE（`text/event-stream`）
- 路由元数据：响应头 `X-TokenSaver-Tier` / `X-TokenSaver-Upstream-Model`；非流式 body 顶层 `x_tokensaver`

## 关键设计决策

1. **分类器单例**：模型加载一次（约数秒），全局复用，避免每请求重载。
2. **模型显式指定 = 透传**：`model` 非 auto 时不做路由，直接转发客户端指定模型，方便调试与逃生。
3. **响应兼容优先**：body 保持 OpenAI 格式，路由信息放 header / 自定义顶层字段，不破坏标准客户端。

## 待办 / 演进

- [ ] classify 改线程池/独立进程（当前同步 predict 会占事件循环）
- [ ] 多 key 轮询（9router key 池）
- [ ] RTK 上下文压缩
- [ ] 免费渠道优先策略
- [ ] 自动化测试（pytest）
