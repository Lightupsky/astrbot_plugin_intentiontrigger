"""astrbot_plugin_intentiontrigger

利用 TypeSafe Jev 模型对群聊消息做意图识别：
只有当发言表现出"要与机器人互动"的意图时，才把消息交给主 LLM 处理，
从而让机器人在群里无需 @ 也能自然应答，同时不打扰无关闲聊。

工作原理（基于 AstrBot 消息管道）：
1. WakingCheckStage 阶段评估插件 handler 的 filter，filter 通过即视为唤醒。
   本插件注册了一个自定义 filter，仅对「未被显式唤醒(@/唤醒前缀)的群消息」放行，
   因此这类消息得以进入管道并到达本插件的 handler。
2. handler 中调用 Jev（noul 原语）判定互动意图概率：
   - 概率 >= 阈值：置 event.is_at_or_wake_command = True，消息交由主 LLM 处理；
   - 概率 <  阈值：不做任何操作，管道自然结束，主 LLM 不会被调用。
3. 被 @ / 带唤醒前缀 / 私聊的消息不走门控，保持 AstrBot 原生行为。
"""

import asyncio

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter

GATE_HANDLER_PRIORITY = 100  # 确保在其他监听群消息的插件 handler 之前执行

DEFAULT_INSTRUCTIONS = (
    "The `state` contains one message from a group chat, and a description of "
    "an assistant bot (`bot`) that is present in that group.\n"
    "Does the speaker of `message` intend to interact with that bot: addressing "
    "it, calling it by name or alias, greeting it, asking or telling it "
    "something, giving it a command, or otherwise expecting the bot to reply?\n"
    "The message may be in any language (Chinese, English, Japanese, ...). "
    "Judge the speaker's intent, not keyword matches alone: mentions of the "
    "bot's name between humans, rhetorical questions and human-to-human chat "
    "do not count; questions, requests or summons aimed at the bot do count."
)

DEFAULT_CRITERIA = {
    "true": (
        "The utterance is directed at the bot: it greets, asks, tells or "
        "commands the bot, addresses it by name/alias, replies to it, or "
        "clearly expects a response from the bot."
    ),
    "false": (
        "The utterance is human-to-human conversation, self-talk, or content "
        "not aimed at the bot at all."
    ),
}

# 模块级状态：插件实例在 __init__ 时注册自己，供自定义 filter 读取。
# filter 在 WakingCheckStage 中同步执行，必须轻量且永不抛异常。
_GATE_STATE: dict = {"impl": None}


class IntentionGateFilter(CustomFilter):
    """只对「需要门控」的群消息放行（详见 IntentionTrigger.should_gate）。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:  # noqa: A002
        try:
            impl = _GATE_STATE["impl"]
            if impl is None:
                return False
            return impl.should_gate(event)
        except Exception as e:  # 永不让 filter 异常冒泡到管道
            logger.debug(f"[intentiontrigger] gate filter error: {e}")
            return False


@register(
    "astrbot_plugin_intentiontrigger",
    "Lightupsky",
    "利用 Jev 模型识别群聊互动意图，仅在有人想和机器人互动时才唤起主 LLM",
    "1.0.0",
)
class IntentionTrigger(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        _GATE_STATE["impl"] = self

        self._session: aiohttp.ClientSession | None = None
        self._sem: asyncio.Semaphore | None = None
        self._warned_no_key = False
        # 简易统计
        self.stats = {"checked": 0, "triggered": 0, "suppressed": 0, "errors": 0}

        # 兼容旧版 AstrBot：无 config 注入时退化为空配置
        if self.config is None:
            self.config = AstrBotConfig(config_path="", schema={})

    async def initialize(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg("timeout_seconds", 10)),
        )
        self._sem = asyncio.Semaphore(self.cfg("max_concurrency", 8))
        if not self.api_key and not self._warned_no_key:
            logger.warning(
                "[intentiontrigger] 未配置 TypeSafe API Key（插件配置或环境变量 "
                "TYPESAFE_API_KEY），意图门控不会生效，机器人保持原生唤醒行为。"
            )
            self._warned_no_key = True
        rl = self.context.get_config()["platform_settings"].get("rate_limit", {})
        if rl.get("count", 0) > 0:
            logger.warning(
                "[intentiontrigger] 检测到全局会话限流已开启"
                f"（{rl.get('count')} 条/{rl.get('time', 60)} 秒）。"
                "本插件会让所有群消息进入管道，活跃群可能耗尽限流额度导致回复延迟，"
                "建议将 platform_settings.rate_limit.count 调大或设为 0。"
            )
        logger.info(
            "[intentiontrigger] 已加载。threshold=%s model=%s",
            self.cfg("threshold", 0.5),
            self.cfg("model", "jev-latest"),
        )

    async def terminate(self):
        _GATE_STATE["impl"] = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #

    def cfg(self, key: str, default=None):
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        if value is None or value == "":
            return default
        return value

    @property
    def api_key(self) -> str:
        import os

        return self.cfg("api_key", "") or os.environ.get("TYPESAFE_API_KEY", "")

    def should_gate(self, event: AstrMessageEvent) -> bool:
        """判断一条消息是否需要经过意图门控（在 WakingCheckStage 中同步调用）。"""
        if not self.cfg("enable", True):
            return False
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            return False
        # 已被显式唤醒（@、唤醒前缀、Reply 机器人）的消息保持原生行为，无需门控
        if event.is_at_or_wake_command:
            return False
        # 忽略机器人自己的消息
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False
        if not (event.message_str or "").strip():
            return False
        if not self.api_key:
            return False  # 没有 key 时完全保持原生行为（唤醒被丢弃，事件照常停止）

        group_id = str(event.get_group_id() or "")
        whitelist = [str(g) for g in (self.cfg("group_whitelist", []) or [])]
        blacklist = [str(g) for g in (self.cfg("group_blacklist", []) or [])]
        if group_id in blacklist:
            return False
        if whitelist and group_id not in whitelist:
            return False
        return True

    def _build_state(self, event: AstrMessageEvent) -> dict:
        bot_names = [str(n) for n in (self.cfg("bot_names", []) or []) if str(n)]
        if not bot_names:
            bot_names = [str(event.get_self_id())]
        return {
            "bot": {
                "name": bot_names[0],
                "aliases": bot_names,
                "bot_self_id": str(event.get_self_id()),
            },
            "chat": {
                "type": "group",
                "speaker_name": event.get_sender_name() or "",
                "speaker_id": str(event.get_sender_id()),
            },
            "message": (event.message_str or "").strip(),
        }

    async def _call_jev(self, state: dict) -> float:
        """调用 TypeSafe System One 接口，返回「想与机器人互动」的 noul 概率。"""
        instructions = self.cfg("custom_instructions", "") or DEFAULT_INSTRUCTIONS
        payload = {
            "state": state,
            "model": self.cfg("model", "jev-latest"),
            "questions": {
                "wants_interaction": {
                    "type": "noul",
                    "instructions": instructions,
                    "criteria": DEFAULT_CRITERIA,
                }
            },
        }
        base_url = str(self.cfg("api_base_url", "https://api.typesafe.ai")).rstrip("/")
        url = f"{base_url}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        max_attempts = 2
        for attempt in range(max_attempts):
            assert self._session and self._sem
            async with self._sem:
                async with self._session.post(url, json=payload, headers=headers) as resp:
                    body_text = await resp.text()
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        answer = (data.get("answers") or {}).get("wants_interaction") or {}
                        value = answer.get("noul")
                        if not isinstance(value, int | float):
                            raise ValueError(f"Jev 响应缺少 noul 字段: {body_text[:200]}")
                        return float(value)
                    if resp.status in (429, 529) and attempt < max_attempts - 1:
                        await asyncio.sleep(1.5**attempt)  # 指数退避
                        continue
                    raise RuntimeError(
                        f"TypeSafe API HTTP {resp.status}: {body_text[:200]}"
                    )
        raise RuntimeError("TypeSafe API 调用重试耗尽")

    # ------------------------------------------------------------------ #
    # 核心：群消息意图门控
    # ------------------------------------------------------------------ #

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=GATE_HANDLER_PRIORITY
    )
    @filter.custom_filter(IntentionGateFilter, False)
    async def gate_group_message(self, event: AstrMessageEvent):
        """对未被显式唤醒的群消息做意图识别，决定是否交给主 LLM。"""
        threshold = float(self.cfg("threshold", 0.5))
        self.stats["checked"] += 1
        state = self._build_state(event)
        text = state["message"]

        try:
            score = await self._call_jev(state)
        except Exception as e:
            self.stats["errors"] += 1
            logger.warning(f"[intentiontrigger] Jev 调用失败: {e}")
            if self.cfg("on_error", "suppress") == "pass_to_llm":
                event.is_at_or_wake_command = True
                logger.info(
                    "[intentiontrigger] on_error=pass_to_llm，消息直接放行: %r", text
                )
            return

        if score >= threshold:
            self.stats["triggered"] += 1
            # 关键一步：让 ProcessStage 把这条消息当作被唤醒的消息交给主 LLM
            event.is_at_or_wake_command = True
            if self.cfg("log_decisions", False):
                logger.info(
                    "[intentiontrigger] ✅ 触发 (score=%.3f>=%.3f): %r", score, threshold, text
                )
        else:
            self.stats["suppressed"] += 1
            if self.cfg("log_decisions", False):
                logger.info(
                    "[intentiontrigger] ⛔ 抑制 (score=%.3f<%.3f): %r", score, threshold, text
                )
            if self.cfg("block_other_handlers", False):
                # 可选：连同其他插件对普通群消息的监听一起拦截（默认关闭）
                event.stop_event()

    # ------------------------------------------------------------------ #
    # 管理指令：/intention status|test|on|off
    # ------------------------------------------------------------------ #

    @filter.command("intention", alias={"itrig"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def intention_command(self, event: AstrMessageEvent):
        """意图触发器管理指令：/intention [status|test <文本>|on|off]"""
        parts = (event.message_str or "").strip().split(maxsplit=2)
        # parts 形如 ["intention", <子命令>, <剩余文本>]；也可能只有 ["intention"]
        sub = parts[1].lower() if len(parts) >= 2 else "status"
        rest = parts[2] if len(parts) >= 3 else ""

        if sub == "on":
            self.config["enable"] = True
            self.config.save_config()
            yield event.plain_result("意图门控已开启。")
        elif sub == "off":
            self.config["enable"] = False
            self.config.save_config()
            yield event.plain_result("意图门控已关闭，机器人恢复原生唤醒行为。")

        elif sub == "test":
            if not rest:
                yield event.plain_result("用法: /intention test <要测试的群消息文本>")
                return
            if not self.api_key:
                yield event.plain_result("尚未配置 TypeSafe API Key，无法测试。")
                return
            state = {
                "bot": {
                    "name": "bot",
                    "aliases": [str(event.get_self_id())],
                    "bot_self_id": str(event.get_self_id()),
                },
                "chat": {"type": "group", "speaker_name": "tester", "speaker_id": "0"},
                "message": rest,
            }
            try:
                score = await self._call_jev(state)
            except Exception as e:
                yield event.plain_result(f"调用失败: {e}")
                return
            threshold = float(self.cfg("threshold", 0.5))
            verdict = "✅ 会触发主 LLM" if score >= threshold else "⛔ 不会触发"
            yield event.plain_result(
                f"noul={score:.3f}（阈值 {threshold}）\n判定: {verdict}"
            )

        else:  # status
            rl = self.context.get_config()["platform_settings"].get("rate_limit", {})
            lines = [
                f"启用: {self.cfg('enable', True)}",
                f"模型: {self.cfg('model', 'jev-latest')} @ {self.cfg('api_base_url', 'https://api.typesafe.ai')}",
                f"阈值: {self.cfg('threshold', 0.5)}",
                f"API Key: {'已配置' if self.api_key else '❌ 未配置'}",
                f"统计: 检查 {self.stats['checked']} / 触发 {self.stats['triggered']} / "
                f"抑制 {self.stats['suppressed']} / 错误 {self.stats['errors']}",
                f"全局限流: {rl.get('count', 0)} 条/{rl.get('time', 60)}s "
                f"{'（⚠ 活跃群建议调大或关闭）' if rl.get('count', 0) > 0 else '（已关闭）'}",
            ]
            yield event.plain_result("\n".join(lines))
