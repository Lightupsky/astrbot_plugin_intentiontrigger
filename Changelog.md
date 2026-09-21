# Changelog

本文件记录 astrbot_plugin_intentiontrigger 的版本历史。最新版本的摘要同时会保留在
[README.md](README.md) 中。

## v1.2.0 — 2026-09-21

### Fixed

- **统计页不显示数据 / 手动测试报错**（`Cannot read properties of undefined (reading 'apiPost')`）：
  AstrBot 自动注入 bridge SDK 的位置在 `</body>` 前，晚于页面内联脚本执行，
  `window.AstrBotPluginPage` 求值为 `undefined`。改为在 `<head>` 显式引用
  `/api/plugin/page/bridge-sdk.js`（AstrBot 自动重写为带鉴权 URL），并加防御性等待。
- WebUI POST body 解析：dashboard 自研 HTTP 层下 `request.json(default=...)` 会静默
  失败，改用 `body() + json.loads`。
- 消息池健壮性：`_flush_after` 与判定流程补异常兜底，任何崩溃都会立即唤醒池内
  future 并记录 error 日志，不再悬挂至 `wait_for` 超时（约 30s）。
- stats.jsonl 长跑膨胀：按行数轮转（`stats_max_lines`，默认 5000），超限保留最近一半。
- 平均延迟不再被错误记录（含 15s 超时）污染：只统计成功调用，错误数单独计数。

### Added

- **意图分类风格提示**：判定时附带 choice 问题，将发言分类为
  提问 / 闲聊 / 指令 / 玩梗 / 非对bot；放行的消息通过 `on_llm_request` 钩子把对应
  风格提示追加到主 LLM 输入**末尾**（不动 system prompt，不破坏前缀缓存）。
  文案可配置（`category_hints`），可整体关闭（`enable_category_hint`）。
- **估算花费面板**：每次调用的 token 用量入库，统计页按单价
  （默认 Jev 官方 $0.042/Mtok 输入、输出免费，可配置）累计估算开销。
- `/intention test` 与 WebUI 测试框支持**多行上下文**（一行一条消息，走池判定），
  与生产模式一致，不再误导阈值调参。

## v1.1.0 — 2026-09-21

### Added

- **消息池模式（默认）**：每群攒 `pool_size`（默认 5）条消息、或超过
  `flush_interval`（默认 5 秒）后，把整段对话上下文一次性交给 Jev，池内每条消息
  各得一个独立的 noul 概率（多问并行评估）：
  - API 调用次数降为逐条判定的 1/N，从容应对 Jev 的 rate limit；
  - 碎片消息（"在吗在吗"、"快理我一下"）判定显著更准
    （实测 0.52 → 0.92、0.58 → 0.90）；
  - `one_reply_per_flush`（默认开）：同池多条达到阈值时只放行分数最高的一条。
- **WebUI 统计页**（`pages/stats`）：分数直方图（含阈值分割线）、触发率、平均延迟、
  按群统计、最近调用明细、手动试判定；数据落盘 `stats.jsonl`。
- 群白名单收紧为必填语义：`group_whitelist` 为空时插件不对任何群生效。
- `bot_names` 机器人称呼自定义（如"鸭嘴兽"），作为判定身份注入。

### 实测

30 条中文复杂场景（碎片追问 / 交叉话题 / 提及非互动 / 语气词 / 隐式命令 /
连续对话），真实 API：single（无上下文）90% vs pool（带上下文）**100%**，
且分数分离度大（该抑制 0.02~0.07，该放行 0.64~0.98）。

## v1.0.0 — 2026-09-21

### Added

- 初始实现：基于 TypeSafe Jev `noul` 原语的群聊意图门控。
  自定义 filter 仅放行未被 @ / 唤醒前缀唤醒的群消息；达到阈值置
  `event.is_at_or_wake_command = True` 交给主 LLM，否则不干预。
- `/intention status|test|on|off` 管理指令（管理员）。
- 完整配置面板（`_conf_schema.json`）：阈值、群黑白名单、故障策略、
  并发/超时、判定日志等。
- 单元测试（mock API）22 项。
- `docs/laya-local-deployment.md`：laya 在 N100 + 16GB 上的本地部署实测分析
  （结论：工程可行，零样本中文判定质量不足，建议攒数据微调后再迁移）。
