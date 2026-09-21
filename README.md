# astrbot_plugin_intentiontrigger

利用 [TypeSafe Jev](https://docs.typesafe.ai/introduction) 模型对群聊发言做**意图识别**：只有当发言表现出"要与机器人互动"的意图时，才把消息交给主 LLM 处理。

在群里，用户无需 @ 机器人、无需唤醒前缀，直接说"小星，今天天气怎么样？"、"bot 帮我查个航班"即可触发回复；而群友之间的日常闲聊不会浪费主 LLM 的调用。

## 工作原理

```
群消息 ──► 插件自定义 filter（仅放行未被 @/前缀唤醒的群消息）
        ──► handler 调用 Jev noul 原语："发言人是否想与机器人互动？"
              ├─ 概率 ≥ 阈值 ──► 置 is_at_or_wake_command = True ──► 主 LLM 处理
              └─ 概率 < 阈值 ──► 不做任何事，主 LLM 不会被调用
```

- 被 **@**、带**唤醒前缀**、**私聊**的消息不走门控，保持 AstrBot 原生行为，指令（如 `/intention`）不受影响。
- 判定基于发言语义而非关键词匹配，天然支持中文等多语言表述。

## 安装

1. 将本插件放入 `AstrBot/data/plugins/astrbot_plugin_intentiontrigger`（或通过 WebUI 插件市场安装）。
2. 在[插件配置面板](http://localhost:6185) 中填写 TypeSafe API Key（[获取地址](https://console.typesafe.ai/keys)，也可用环境变量 `TYPESAFE_API_KEY`）。
3. （推荐）在 `bot_names` 中填写机器人在群里的昵称，可显著提升称呼类消息的识别率。
4. 重载插件。

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 门控总开关 |
| `api_key` | `""` | TypeSafe API Key（或环境变量 `TYPESAFE_API_KEY`） |
| `model` | `jev-latest` | 模型 |
| `threshold` | `0.5` | noul 概率阈值，调高更保守、调低更积极 |
| `bot_names` | `[]` | 机器人昵称列表，帮助模型识别称呼 |
| `group_whitelist` / `group_blacklist` | `[]` | 群黑白名单（留空白名单 = 全部群） |
| `on_error` | `suppress` | API 失败时策略：`suppress`（当无意图）/ `pass_to_llm`（放行给 LLM） |
| `block_other_handlers` | `false` | 无意图时是否终止事件传播（拦截其他插件的群消息监听） |
| `log_decisions` | `false` | 输出每条消息的判定日志，用于调参 |

## 管理指令（管理员）

| 指令 | 说明 |
|---|---|
| `/intention status` | 查看配置与统计（检查/触发/抑制/错误计数） |
| `/intention test <文本>` | 对任意文本试跑一次判定，返回 noul 分数，用于调阈值 |
| `/intention on` / `/intention off` | 开关门控（持久化） |

## ⚠️ 重要：限流建议

本插件会让**所有群消息**进入 AstrBot 消息管道（这是实现意图识别的前提），而 AstrBot 的全局会话限流（`platform_settings.rate_limit`，默认 30 条/60 秒）会对这些消息计数。**活跃群可能耗尽限流额度，导致机器人回复被 stall/discard**。

建议在使用本插件时，将 WebUI 中的 `平台设置 → 限流` 调大或关闭（count 设为 0）。插件加载时会检测并警告。

## 调参建议

- 用 `/intention test` 对典型消息试跑：
  - "小星在吗" / "帮我总结下这段" → 应接近 1.0
  - "今天吃什么" / "哈哈哈哈" → 应接近 0.0
  - 边界消息（如群友讨论机器人本身）落在中间，按需求调节 `threshold`。
- 误触发多 → 提高阈值到 0.6~0.7；漏触发多 → 降到 0.3~0.4。
- 开启 `log_decisions` 观察真实分布。

## 本地部署替代方案

若不想依赖 TypeSafe API，可用开源的 [laya](https://huggingface.co/convaiinnovations/laya)（421M 参数、Apache 2.0、同为"typed decisions"架构）自建兼容服务，并通过 `api_base_url` 指向它（需自行实现 `/v1/systemone` 兼容层）。在 N100 + 16GB 内存设备上 CPU 推理完全可行（单次约 0.2~0.5 秒），详见仓库 `docs/laya-local-deployment.md`。

## 开发

- AstrBot 插件开发文档: <https://docs.astrbot.app/dev/star/plugin-new.html>
- TypeSafe 文档: <https://docs.typesafe.ai/>
- 本地调试：`uv venv && uv pip install -e AstrBot`，将本目录软链到 `AstrBot/data/plugins/`

## License

AGPL-3.0（沿用仓库 LICENSE）
