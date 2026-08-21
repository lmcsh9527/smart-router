# DSH Desktop 对接 TokenSaver 指南

> 目标：让任何 DSH Desktop 用户把「智能路由」接进自己的客户端——简单问题自动走轻量模型，复杂问题自动走强模型。

## 前置条件

1. TokenSaver 已安装并启动（见根目录 `README.md` 三步上手，或 `bash install.sh --launchd`）
2. 后台 http://localhost:20130/admin 里至少每个档位有一个可用模型
   （自检：`bash scripts/doctor.sh`，看到 c0/c1/c2/c3 都有 ✅ 即可）

## 方式 A · 后台一键接入（推荐）

TokenSaver 后台内置 DSH 接入向导：

1. 打开 http://localhost:20130/admin
2. 找到「DSH 接入」区块，按提示操作（自动生成 provider 配置）

## 方式 B · 手工编辑 settings.yaml

在 DSH 的 `settings.yaml` 的 `providers:` 段下追加（名字可自定义，这里叫 `tokensaver`）：

```yaml
providers:
  tokensaver:
    api: openai-completions
    baseURL: http://localhost:20130/v1
    apiKeyEnv: TOKENSAVER_KEY        # 本地网关不校验 key，环境变量存在即可
    models:
      - id: auto                     # 智能路由入口（必留）
        contextWindow: 1000000       # 按你池子里最小上下文填
```

> ⚠️ `contextWindow` 填你模型池里**最小**的那个值，避免超长上下文打到小模型上报错。

然后重启 DSH，模型选择器里就会出现 `tokensaver / auto`。

## 日常使用

| 场景 | 做法 |
|---|---|
| 让 AI 自动选模型 | 会话模型选 `tokensaver / auto`，判档全自动 |
| 想指定某个模型 | 选择器里直接选池子里暴露的具体模型 |
| 设为新会话默认 | 选择器里选中后设为默认（app 会记住） |

## 验证

```bash
# 网关侧冒烟（应返回判档日志与回复）
curl http://localhost:20130/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"auto","messages":[{"role":"user","content":"1+1等于几？只回答数字"}]}'

# 健康自检
bash scripts/doctor.sh
```

## 常见问题

| 症状 | 处理 |
|---|---|
| 选择器里看不到 tokensaver | settings.yaml 没保存好 / DSH 没重启 |
| 请求全部失败 | `bash scripts/doctor.sh` 看哪个档位缺模型、哪个上游不可达 |
| 想换端口 | 启动时 `TOKENSAVER_PORT=xxxx`，DSH 的 baseURL 同步改 |
