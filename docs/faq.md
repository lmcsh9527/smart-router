# TokenSaver 常见问题（FAQ）

## 网关启动报错 `ModuleNotFoundError: No module named 'fastapi'`

网关必须跑在 opensquilla venv 里（它才有 opensquilla 包）：

```bash
/Users/hui/.local/share/uv/tools/opensquilla/bin/python -m pip list  # 确认 fastapi
```

安装 fastapi（国内网络建议镜像）：

```bash
/Users/hui/.local/bin/uv pip install --python /Users/hui/.local/share/uv/tools/opensquilla/bin/python fastapi \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 网关连不上 9router / 转发超时

- 确认 Docker Desktop 在跑、`lsof -iTCP:20128 -sTCP:LISTEN` 有监听。
- 实测 `GET /v1/models` 在 9router 上会超时（2026-08-19 观测），但 `POST /v1/chat/completions` 正常——网关不依赖 `/models`。
- 用 `curl -s -m 30 -X POST http://localhost:20128/v1/chat/completions ...` 直连验证上游。

## 路由结果不符合预期（简单问题分到 c2 / 复杂问题分到 c0）

- SquillaRouter 是 ML 分类器，输出带置信度；个别输入可能偏差。
- 看网关日志 `ROUTE ... tier=... confidence=... difficulty=...` 与实际文本，确认是否可复现。
- 复杂问题建议描述更完整（带代码块/明确要求），分类器更易判为高档。

## uv 报 `Failed to initialize cache` 或下载超时

- `~/.cache/uv` 权限异常时设 `UV_CACHE_DIR=/tmp/uv-cache-xxx`。
- pypi.org 慢/超时用清华镜像：`-i https://pypi.tuna.tsinghua.edu.cn/simple`。

## sqlite_vec 相关坑

【待查】OpenSquilla 历史安装中曾涉及 sqlite_vec 依赖问题；本 MVP 未使用 sqlite_vec，如遇相关报错再补充记录。
