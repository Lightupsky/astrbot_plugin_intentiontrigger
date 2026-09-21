# astrbot_plugin_intentiontrigger

利用 [TypeSafe Jev](https://docs.typesafe.ai/introduction) 模型对群聊发言做**意图识别**：只有当发言表现出"要与机器人互动"的意图时，才把消息交给主 LLM 处理。

在群里，用户无需 @ 机器人、无需唤醒前缀，直接说"鸭嘴兽，今天天气怎么样？"、"帮我查个航班"即可触发回复；而群友之间的日常闲聊不会浪费主 LLM 的调用。

## v1.2.0 新特性

- **修复统计页不显示数据/手动测试报错**：bridge SDK 改为在 `<head>` 显式引用（AstrBot 自动注入发生在 `</body>` 前，晚于页面脚本执行导致 `window.AstrBotPluginPage` 为 undefined）。
- **意图分类风格提示**：判定时附带 choice 问题将发言分类为 提问/闲聊/指令/玩梗，放行的消息把对应提示**追加到主 LLM 输入末尾**（不动 system prompt，不破坏前缀缓存），回复风格更贴合；文案可在配置中自定义，也可整体关闭。
- **估算花费面板**：记录每次调用的 token 用量，统计页按单价（默认 Jev 官方 $0.042/Mtok 输入、输出免费）累计估算开销。
- **多行上下文测试**：`/intention test` 与统计页测试框支持多行输入（一行一条消息），按群聊上下文走池判定，与生产模式一致，不再误导调参。
- **健壮性**：stats.jsonl 按行数轮转（默认 5000 行，保留最近一半）；平均延迟只统计成功调用；消息池定时器/判定流程补异常兜底，future 不再悬挂至超时。

## v1.1.0 特性

- **消息池模式（默认）**：每群攒 `pool_size`（默认 5）条消息、或超过 `flush_interval`（默认 5 秒）后，把整段对话上下文一次性交给 Jev，池内每条消息各得一个独立的 noul 概率（多问并行评估）。相比逐条判定：
  - **API 调用次数降低约 5 倍**，从容应对 Jev 的 rate limit；
  - **碎片消息判定更准**——实测"在吗在吗"（无上下文 0.52）→（带上下文 0.92）、"还是不好笑"（0.45 → 0.89）；
  - `one_reply_per_flush`（默认开）：同一池内多条消息都达到阈值时只放行分数最高的一条，避免连珠炮式重复回复。
- **WebUI 统计页**：插件页面 → `stats`，实时展示判定分数直方图（含阈值分割线）、触发率、平均延迟、按群统计、最近每次调用的明细，并支持在页面上手动输入文本试判定——调阈值不再靠猜。
- **群白名单（必填）**：只对白名单内的群启用，留空时插件不对任何群生效（安全默认）。
- **机器人称呼自定义**：`bot_names` 填"鸭嘴兽"等昵称，作为 bot 身份参与判定，称呼类消息识别的关键。

## 工作原理

```
白名单群消息 ──► 自定义 filter（仅放行未被 @/前缀唤醒的消息）
            ──► 进入每群消息池（攒 N 条 / 超时 T 秒）
            ──► 一次调用 Jev：state=整段上下文，questions=每条消息一个 noul
                  ├─ noul_i ≥ 阈值 ──► 置 is_at_or_wake_command = True ──► 主 LLM 处理
                  └─ noul_i < 阈值 ──► 不做任何事，主 LLM 不会被调用
```

- 被 **@**、带**唤醒前缀**、**私聊**的消息不走门控，保持 AstrBot 原生行为，指令（如 `/intention`）不受影响。
- 判定基于发言语义而非关键词匹配，多语言支持好（中文实测见 `tests/e2e_scenarios.py`）。

## 快速开始

1. 安装插件（放入 `data/plugins/` 或从插件市场）。
2. 在[插件配置面板](http://localhost:6185)中：
   - 填写 **TypeSafe API Key**（[获取](https://console.typesafe.ai/keys)，或用环境变量 `TYPESAFE_API_KEY`）；
   - 填写**群白名单**（`group_whitelist`，必填，否则插件不生效）；
   - 填写**机器人称呼**（`bot_names`，如 `["鸭嘴兽", "鸭鸭"]`）。
3. 重载插件，完成。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 门控总开关 |
| `mode` | `pool` | `pool` 消息池模式（推荐）/ `single` 逐条即时判定 |
| `pool_size` | `5` | 攒多少条群消息判定一次 |
| `flush_interval` | `5` | 池子超时秒数（冷群兜底） |
| `one_reply_per_flush` | `true` | 同一池只放行分数最高的一条 |
| `api_key` | `""` | TypeSafe API Key（或环境变量 `TYPESAFE_API_KEY`） |
| `model` | `jev-latest` | 模型 |
| `threshold` | `0.5` | noul 阈值，配合统计页直方图调整 |
| `bot_names` | `[]` | 机器人称呼列表（如"鸭嘴兽"），**强烈建议填写** |
| `group_whitelist` | `[]` | **必填**，只对这些群启用门控 |
| `group_blacklist` | `[]` | 黑名单，优先于白名单 |
| `on_error` | `suppress` | API 失败时：`suppress`（当无意图）/ `pass_to_llm`（放行） |
| `block_other_handlers` | `false` | 无意图时是否终止事件传播 |
| `log_decisions` | `false` | 输出每条判定日志 |

## 管理指令（管理员）

| 指令 | 说明 |
|---|---|
| `/intention status` | 查看配置与统计摘要 |
| `/intention test <文本>` | 对任意文本试跑判定，返回 noul 分数 |
| `/intention on` / `/intention off` | 开关门控（持久化） |

## 统计页面（阈值调优）

WebUI → 插件 → astrbot_plugin_intentiontrigger → **stats** 页面：

- **分数直方图**：所有判定消息的 noul 分布 + 当前阈值分割线。理想状态是两侧分明（闲聊挤在 0~0.2，互动挤在 0.8+）；若中间重叠多，优先检查 `bot_names` 是否配置、考虑调 `threshold`。
- **触发率/延迟/错误数**：一眼判断插件健康度。
- **最近调用明细**：每次池判定的每条消息、分数、放行与否，误判案例直接可见。
- **手动测试框**：输入任意文本立即得到判定分数（计入统计）。
- 数据同时追加落盘到 `data/plugin_data/astrbot_plugin_intentiontrigger/stats.jsonl`，可离线分析。

## 实测效果（真实 API，30 条中文消息）

`tests/e2e_scenarios.py` 覆盖 6 类复杂场景（碎片追问、交叉话题、提及机器人但非互动、纯语气词、隐式命令、连续对话+旁人插话）：

| 模式 | 准确率 | 说明 |
|---|---|---|
| single（无上下文） | 90% | 漏判"还是不好笑"(0.45)、误判"啥新闻发出来看看"(0.53) |
| **pool（带上下文）** | **100%** | 该抑制的 0.02~0.07，该放行的 0.64~0.98，分离度极大 |

复现：`TYPESAFE_API_KEY=... python tests/e2e_scenarios.py`

## ⚠️ 重要：限流建议

本插件会让**白名单群的所有消息**进入 AstrBot 消息管道，而 AstrBot 的全局会话限流（`platform_settings.rate_limit`，默认 30 条/60 秒）会对这些消息计数。**活跃群可能耗尽限流额度，导致机器人回复被 stall/discard**。建议将 WebUI 中的 `平台设置 → 限流` 调大或关闭（count 设为 0）。插件加载时会检测并警告。

## 本地部署替代方案

若不想依赖 TypeSafe API，可用开源的 [laya](https://huggingface.co/convaiinnovations/laya)（421M 参数、Apache 2.0、同为"typed decisions"架构）自建兼容服务，并通过 `api_base_url` 指向它（需自行实现 `/v1/systemone` 兼容层）。在 N100 + 16GB 内存设备上 CPU 推理完全可行（单次约 0.2~0.5 秒），但**零样本中文判定质量实测不佳**，建议攒数据微调后再迁移，详见 `docs/laya-local-deployment.md`。

## 开发

- AstrBot 插件开发文档: <https://docs.astrbot.app/dev/star/plugin-new.html>
- TypeSafe 文档: <https://docs.typesafe.ai/>
- 本地调试：`uv venv && uv pip install -e AstrBot`，将本目录放入/链接到 `AstrBot/data/plugins/`（注意：Windows 下 junction 会导致 Pages 发现失效，用真实目录拷贝）
- 单元测试（mock API）：`python tests/test_gate.py`

## License

AGPL-3.0（沿用仓库 LICENSE）
