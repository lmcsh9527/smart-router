# Changelog

## [0.1.0] - 2026-08-19 - MVP 初始版本

- 项目骨架建立（README / LICENSE / .gitignore / CONTRIBUTING / docs）
- OpenAI 兼容网关：`POST /v1/chat/completions`（非流式 + SSE 流式）
- SquillaRouter V4 Phase3 判档接入（c0-c3）→ 档位映射 9router 模型
- 降级重试链：高档模型超时/失败自动降级到低档
- 实测：简单→flash、复杂→pro、流式 SSE 全部通过
