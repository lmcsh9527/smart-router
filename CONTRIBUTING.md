# Contributing

欢迎参与 TokenSaver 开发。请先阅读 [README.md](./README.md) 与 [docs/design.md](./docs/design.md) 了解项目背景与架构。

## 如何报告问题

- 先在 [FAQ](./docs/faq.md) 里查是否已有答案。
- 描述清楚：复现步骤、期望行为、实际行为、日志/报错原文、环境信息（Python 版本、依赖版本、上游 9router 是否在线）。
- 带上路由日志（`ROUTE ...` 行）有助于定位。

## 如何提 PR

1. Fork 仓库，从 `main` 开分支，命名 `feat/xxx` 或 `fix/xxx`。
2. 小步提交，每个提交只做一件事，commit message 写清动机。
3. 跑通最小验证：`minimal_classify_test.py` + 网关的简单/流式 curl 实测。
4. 更新 `CHANGELOG.md`，开 PR 说明改动与验证结果。

## 开发环境

- Python 3.12（opensquilla uv tool venv，见 README 依赖节）
- 依赖：见 [requirements.txt](./requirements.txt)
- 上游：本地 9router `http://localhost:20128/v1`

## 测试

MVP 阶段以手动实测为主：

```bash
# classify 最小验证
python3 minimal_classify_test.py

# 网关冒烟
curl http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1等于几"}]}'
```

自动化测试（pytest）待后续里程碑补齐。
