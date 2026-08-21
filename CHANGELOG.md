# Changelog

## [0.1.0] - 2026-08-19 - MVP 初始版本

- 项目骨架建立（README / LICENSE / .gitignore / CONTRIBUTING / docs）
- OpenAI 兼容网关：`POST /v1/chat/completions`（非流式 + SSE 流式）
- SquillaRouter V4 Phase3 判档接入（c0-c3）→ 档位映射 9router 模型
- 降级重试链：高档模型超时/失败自动降级到低档
- 实测：简单→flash、复杂→pro、流式 SSE 全部通过

## [v3.1] 2026-08-20 机缘直连 + opensquilla vendor 化

### 架构变更
- 上游从 9router(20128) 改为**机缘直连** https://tokenrhythm.studio/v1，去掉 combo 并发抢答
- **opensquilla 深度融合**：squilla_router 提取到 `vendor/opensquilla/`（模型资产 75MB），
  脱离外部 pip 包，import 语句不变（vendor 进 sys.path）
- 模型映射：c0/c1→deepseek-v4-flash，c2/c3→deepseek-v4-pro（机缘直连，无 jy/ 前缀）

### Bug 修复
- **401 根因**：ROUTER_UPSTREAM_KEY 环境变量残留 9router key（35位 sk-8f...），
  被优先读取打机缘导致 401。修复：jy_api_key 文件（600 权限）优先，环境变量仅兜底。

## [v4.0] 2026-08-20 模型池 + 后台管理页面

### 新增
- **模型池** `models_pool.json`（600 权限）：手工维护多模型，每个模型独立
  base_url/api_key(文件或直填)/model/tiers/priority/enabled
- **自动路由查池**：classify 判档 → 池内匹配档位模型（priority 升序）→ 转发；
  池未命中回退默认映射；降级链按池构建（同档优先→其他兜底）
- **后台管理页面** http://localhost:20130/admin
  - 添加/编辑/删除模型（名称/base_url/key/模型名/档位/优先级/启用）
  - 一键测试连通（最小请求验证）
  - 路由状态预览（各档位当前首选）
- **管理 API**：GET/POST /admin/api/pool、PUT/DELETE /admin/api/pool/{id}、
  POST /admin/api/pool/{id}/test、GET /admin/api/status

### 实测
- 简单任务 → c0 → deepseek-v4-flash（机缘直连）✅
- 复杂任务 → c3 → deepseek-v4-pro ✅
- 添加测试模型(flash-0731) → 测试连通 200 → 删除 ✅
- /admin 页面正常渲染 ✅

## [v4.1] 2026-08-20 快速发现模型

### 新增
- **/admin/api/discover**：填 base_url + key（文件或直填）→ 调该服务 /v1/models 自动拉取模型列表
- **后台页「快速发现模型」**：URL + Key → 点「获取模型」→ 列表勾选 → 每行可配档位/优先级 → 「批量添加所选」入池（已有模型自动跳过）
- 手工填写模型保留（折叠区）

### 实测
- 机缘 discover：17 个模型一键拉出（deepseek-v4-flash/pro、glm-5/5.1、kimi-k2.5/2.6、minimax 系列…）
- 批量添加 glm-5(c2,c3) → 自动路由不受影响 → 清理 ✅
- 75MB 本地 classify 模型保留（零延迟零成本判档），opensquilla 全包实为 137MB，vendor 只取路由相关 75MB

## [v4.2] 2026-08-20 供应商库

### 新增
- **供应商库** `providers.json`（600 权限）：保存服务商 name/base_url/key（文件引用或直填）
- **发现自动保存**：快速发现模型成功后自动保存供应商（同 base_url 复用，不重复建），返回 provider_id
- **供应商复用**：下次添加模型时从「已有供应商」下拉直接选，自动带出 Base URL 和 Key，免重输
- **供应商管理**：/admin 页面新增供应商库表格（名称/URL/Key 脱敏/编辑/删除），
  删除供应商不影响已添加模型（保留地址/Key 快照）
- **模型池关联**：模型记录 provider_id，池表格显示所属供应商
- **API**：GET/POST /admin/api/providers、PUT/DELETE /admin/api/providers/{id}

### 实测
- discover 机缘 → 自动保存供应商「机缘」(p-1671461f59, key 脱敏 sk_t***3hMHZg) ✅
- 同 URL 再次 discover → 复用同一供应商 ✅
- 批量添加模型带 provider_id → 保存成功 ✅
- 存量模型 jy-flash/jy-pro 补录 provider_id ✅

## [v4.3] 2026-08-20 实时调用 + 交互修复

### 新增
- **实时调用情况**：/admin 页面新增「实时调用」卡片，每 3 秒自动刷新，
  显示最近 50 次调用（时间/状态/耗时/档位/来源/路由模型）+ 统计
  （总数/成功率/按模型/按档位分布）
- 后端：middleware 记录 /v1/chat/completions 调用（内存 deque 200 条，重启清零），
  GET /admin/api/usage 提供数据；响应头新增 X-TokenSaver-Source

### 修复
- **编辑按钮"没反应"**：点击后自动展开手工表单并滚动到表单（原表单在折叠区，
  填充了但用户看不到）
- **测试按钮"没反应"**：点击立即显示"测试中…（最长30s）"并滚动到结果卡片
- **供应商选择 bug**：手工表单无 provider_name 框，fillFromProvider 防御处理

## [v4.4] 2026-08-20 功能验证测试（1+1=?）

### 改进
- **测试不再只是连通性检查**：改为发短消息「1+1等于几？」（system 要求只输出答案数字），
  校验三关：HTTP 200 + 回复 content 非空 + 包含答案「2」
- **结果持久化**：测试结果写入模型 last_test（ok/status/耗时/时间/详情/回复内容），
  存 models_pool.json，刷新页面后按钮颜色保留
- **按钮变色**：验证通过 → 绿色「✓ 通过 Xs」；失败 → 红色「✗ 失败」；
  悬停显示详情；点击重新测试
- 测试超时放宽到 60s（pro 模型思考可能久），max_tokens=200 防思考截断

### 实测
- 机缘 deepseek-v4-flash → ✅ 绿（回复 '2'，6.7s）
- 机缘 deepseek-v4-pro → ✅ 绿（回复 '2'，2.0s）
- 错误 key → ❌ 红（HTTP 401）
- 轻舟 grok-4.6 / grok-imagine-image-lite → ❌ 红（HTTP 200 但无回复内容，正是"通≠能用"的体现）

## [v4.5] 2026-08-20 glm5.2 误判修复 + 启用/禁用按钮

### 修复
- **glm5.2 误判根因**：池里 base_url 是 `https://lightboat.dpdns.org`（不带 /v1），
  测试请求打到 `/chat/completions` 返回轻舟网页 HTML（200 但无内容）→ 误判失败。
  直连 `/v1/chat/completions` 正常返回 '2'
- **discover 规范化**：发现时若成功路径是 {base}/v1/models，保存的 base_url 自动补 /v1
- **test 端点路径兜底**：base 不含 /v1 时先试 {base}/chat/completions，
  200 但无内容再试 {base}/v1/chat/completions
- **存量修复**：轻舟 6 个模型 + 1 个供应商 base_url 补 /v1

### 新增
- **启用/禁用可点击切换**：模型池状态列改为按钮，点击在绿色「启用」和红色「禁用」间切换
  （PUT /admin/api/pool/{id} enabled），悬停提示

### 实测
- glm5.2 → ✅ 绿（回复 '2'，2.1s）
- 切换启用/禁用 → API 正常（True↔False）

## [v4.6] 2026-08-20 模型类型识别 + 生图专用测试

### 新增
- **模型类型识别** infer_model_type：按模型名关键词推断 text / image / embedding
  （image 关键词：image/imagine/dalle/sdxl/flux/stable-diffusion/tts/audio 等）
- **发现/添加时自动记录 model_type**，存量模型已补录
- **测试按类型分派**：
  - text/embedding → 1+1=? 文本验证
  - image → 调 /v1/images/generations 生小图（256x256），校验 data 非空
- 前端模型表格显示类型 tag（🖼 生图 / 📊 向量 / 💬 文本）

### 修复
- **grok-imagine-image-lite 不再被 1+1 误判**：识别为生图模型后走生图接口，
  当前轻舟生图上游 502（暂时不可用）如实标红，而非"无回复内容"误判

### 实测
- grok-imagine-image-lite → image 类型，走 images/generations，502（上游暂不可用）
- glm5.2 → text 类型，仍 1+1=? 验证，✅ 绿（回复 '2'）

## [v4.7] 2026-08-20 上下文长度自动获取 + 优先级就地编辑

### 新增
- **上下文长度自动获取**：
  - discover 时从 /v1/models 的 context_window 字段读
  - 缺失时查本地 opensquilla 模型目录（~/.opensquilla/state/model_catalog/*.json）
  - 机缘 17 模型自动带出（deepseek-v4-flash/pro 1M、glm-5 1M、glm-5.1/minimax-m2.7 200K 等）
  - 模型池新增 context_window 字段，表格显示（1M/200K/未知）
- **优先级就地编辑**：优先级列改为数字输入框，直接改数字回车/失焦即保存（PUT），
  数字小优先，无需进编辑表单
- **编辑按钮明确化**：title 注明「编辑完整配置（名称/地址/Key/模型名/档位/备注）」，
  并回填供应商下拉

### 实测
- discover 机缘 → deepseek-v4-flash/pro ctx=1000000、glm-5.1 ctx=200000 ✅
- 优先级 PUT 7→5 ✅

## [v4.8] 2026-08-20 Token/费用统计（机缘用量页风格）

### 新增
- **Token 统计**：非流式请求自动记录输入/输出/总 Token（从上游 usage 提取）
- **费用计算**：模型池新增 price_in/price_out（¥/百万 token），编辑表单可填，
  费用 = 入 token/1M×入价 + 出 token/1M×出价；未配置价格显示 —
- **实时调用增强**：统计卡新增「总 Token」「总费用」；明细表新增
  「Token 入/出」列（如 286K/577）和「费用」列（¥0.034538）
- 路由元数据 x_tokensaver 增加 usage（tokens/cost）

### 说明
- 价格为演示值（机缘 flash 1/2 ¥/M），请按各渠道真实价格在编辑表单修改
- 流式响应暂不统计 token（SSE 透传，需 stream_options.include_usage 支持，后续可加）

## [v4.9] 2026-08-20 档位内联编辑

### 变更
- **档位列改为可勾选 pill**：每个模型旁直接显示 c0/c1/c2/c3 四个小按钮，
  点击切换（勾选即保存 PUT tiers），无需进编辑表单
- 档位语义确认：c0-c3 为 opensquilla 分类器内置输出粒度（TEXT_TIERS），
  档位=模型服务的需求等级（可多选），优先级=同档位内排序（数字小优先），两者职责不同

## [v5.0] 2026-08-20 省钱三件套

### 新增
- **按费用分布**：实时调用分布区新增「按费用」条，显示每个模型花了多少钱（需配价格）
- **浪费告警**：判档简单（c0/c1）却走了高档模型（名含 pro/think/max 等）→ 黄色告警，
  提示可能升级词误触/配置问题
- **流式 Token 记账**：解析 SSE 末尾 usage chunk（stream_options.include_usage），
  流式请求也能统计 Token 和费用
- **路由配置修正**：机缘 flash→c0/c1（简单）、pro→c2/c3（复杂）、flash-0731→c0/c1 备胎

### 实测（4 请求）
- 简单 c1→flash / 复杂 c3→pro / 升级词 c3→pro / 流式 c0→flash
- 总 token 207，总费用 ¥0.000793，waste=0 degraded=0 fail=0
