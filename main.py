"""astrbot_plugin_intentiontrigger

利用 TypeSafe Jev 模型对群聊消息做意图识别：
只有当发言表现出"要与机器人互动"的意图时，才把消息交给主 LLM 处理，
从而让机器人在群里无需 @ 也能自然应答，同时不打扰无关闲聊。

工作原理（基于 AstrBot 消息管道）：
1. WakingCheckStage 阶段评估插件 handler 的 filter，filter 通过即视为唤醒。
   本插件注册了一个自定义 filter，仅对「未被显式唤醒(@/唤醒前缀)的白名单群消息」放行，
   因此这类消息得以进入管道并到达本插件的 handler。
2. handler 将消息交给「消息池」：
   - pool 模式（默认）：每群攒 pool_size 条（或 flush_interval 秒超时）后，
     把整段对话上下文一次性发给 Jev，池内每条消息各得一个 noul 概率
     （多问并行评估），达到阈值的放行 —— 显著降低 API 调用次数并提升
     碎片消息（"在吗""快理我"）的判定准确率；
   - single 模式：逐条即时判定。
3. 被判定有互动意图的消息置 event.is_at_or_wake_command = True，交给主 LLM；
   无意图则不做任何操作，管道自然结束。
4. 被 @ / 带唤醒前缀 / 私聊的消息不走门控，保持 AstrBot 原生行为。
5. 每次调用的分数/耗时/结果进入统计环形缓冲并落盘，供 WebUI 统计页
   （pages/stats）可视化，辅助阈值调优。
"""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import error_response, json_response, request
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.custom_filter import CustomFilter
from astrbot.core.star.star_tools import StarTools

GATE_HANDLER_PRIORITY = 100  # 确保在其他监听群消息的插件 handler 之前执行
PLUGIN_NAME = "astrbot_plugin_intentiontrigger"
RECENT_CALLS_MAX = 300  # 内存环形缓冲条数

DEFAULT_INSTRUCTIONS = (
    "The `state` describes a group chat and an assistant bot (`bot`) present in it. "
    "Judge whether the referenced message is the speaker intending to interact "
    "with that bot: addressing it, calling it by name or alias, greeting it, "
    "asking or telling it something, giving it a command, or otherwise expecting "
    "the bot to reply.\n"
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

# 意图分类（choice 问题）的选项与判定标准
CATEGORY_CRITERIA = {
    "question": "The speaker asks the bot for information, help, or an answer.",
    "chitchat": "The speaker is casually chatting, greeting, or socializing with the bot.",
    "command": "The speaker orders or asks the bot to perform a task or action.",
    "meme": "The speaker is joking, teasing, meme-feeding, or playfully provoking the bot.",
    "not_addressed": "The message is not aimed at the bot at all (human-to-human chat).",
}

# 分类 → 追加到主 LLM 输入末尾的风格提示（可通过 category_hints 配置覆盖）
DEFAULT_CATEGORY_HINTS = {
    "question": "[意图提示] 用户这条消息是在向你提问/求助，请直接、清晰地解答。",
    "chitchat": "[意图提示] 用户在和你闲聊寒暄，回复轻松简短即可，不必长篇大论。",
    "command": "[意图提示] 用户在对你下达指令/任务，请执行并把结果汇报给用户。",
    "meme": "[意图提示] 用户在玩梗/逗弄你，可以放松、幽默地接梗。",
    "not_addressed": "",
}

# 模块级状态：插件实例在 __init__ 时注册自己，供自定义 filter 读取。
# filter 在 WakingCheckStage 中同步执行，必须轻量且永不抛异常。
_GATE_STATE: dict = {"impl": None}


class IntentionGateFilter(CustomFilter):
    """只对「需要门控」的群消息放行（详见 IntentionTrigger.should_gate）。"""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        try:
            impl = _GATE_STATE["impl"]
            if impl is None:
                return False
            return impl.should_gate(event)
        except Exception as e:  # 永不让 filter 异常冒泡到管道
            logger.debug(f"[intentiontrigger] gate filter error: {e}")
            return False


@dataclass
class _PoolEntry:
    """一条等待判定的群消息及其判定结果 future。"""

    text: str
    sender_id: str
    sender_name: str
    future: asyncio.Future | None = None


@dataclass
class _CallRecord:
    """一次 Jev 调用的统计记录。"""

    ts: float
    group_id: str
    mode: str
    latency_ms: float
    model: str
    usage: dict
    messages: list  # [{"text","sender","noul","triggered"}]
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "group_id": self.group_id,
            "mode": self.mode,
            "latency_ms": round(self.latency_ms, 1),
            "model": self.model,
            "usage": self.usage,
            "messages": self.messages,
            "error": self.error,
        }


@register(
    "astrbot_plugin_intentiontrigger",
    "Lightupsky",
    "利用 Jev 模型识别群聊互动意图，仅在有人想和机器人互动时才唤起主 LLM",
    "1.2.0",
)
class IntentionTrigger(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        _GATE_STATE["impl"] = self

        self._session: aiohttp.ClientSession | None = None
        self._sem: asyncio.Semaphore | None = None
        self._warned_no_key = False
        self._warned_empty_whitelist = False
        # 每群消息池与锁
        self._pools: dict[str, list[_PoolEntry]] = {}
        self._pool_locks: dict[str, asyncio.Lock] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._flush_tasks: set[asyncio.Task] = set()
        # 统计
        self.recent_calls: deque[_CallRecord] = deque(maxlen=RECENT_CALLS_MAX)
        self.stats = {"checked": 0, "triggered": 0, "suppressed": 0, "errors": 0}
        self._stats_file: Path | None = None
        self._stats_lines = 0  # stats.jsonl 当前行数（用于轮转判断）

        # 兼容旧版 AstrBot：无 config 注入时退化为空配置
        if self.config is None:
            self.config = AstrBotConfig(config_path="", schema={})

    async def initialize(self):
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.cfg("timeout_seconds", 15)),
        )
        self._sem = asyncio.Semaphore(self.cfg("max_concurrency", 8))
        try:
            self._stats_file = StarTools.get_data_dir(PLUGIN_NAME) / "stats.jsonl"
            if self._stats_file.exists():
                with open(self._stats_file, encoding="utf-8") as f:
                    self._stats_lines = sum(1 for _ in f)
        except Exception:
            self._stats_file = None

        if not self.api_key and not self._warned_no_key:
            logger.warning(
                "[intentiontrigger] 未配置 TypeSafe API Key（插件配置或环境变量 "
                "TYPESAFE_API_KEY），意图门控不会生效，机器人保持原生唤醒行为。"
            )
            self._warned_no_key = True
        if self.cfg("mode", "pool") == "pool":
            logger.info(
                "[intentiontrigger] 消息池模式: 每 %s 条或 %s 秒判定一次。",
                self.cfg("pool_size", 5),
                self.cfg("flush_interval", 5),
            )
        # 注册 WebUI 统计页接口
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/stats/overview", self._api_overview, ["GET"], "意图触发器统计概览"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/stats/recent", self._api_recent, ["GET"], "意图触发器最近调用"
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/stats/test", self._api_test, ["POST"], "意图触发器手动测试"
        )
        rl = self.context.get_config()["platform_settings"].get("rate_limit", {})
        if rl.get("count", 0) > 0:
            logger.warning(
                "[intentiontrigger] 检测到全局会话限流已开启"
                f"（{rl.get('count')} 条/{rl.get('time', 60)} 秒）。"
                "本插件会让白名单内群的所有消息进入管道，活跃群可能耗尽限流额度导致回复延迟，"
                "建议将 platform_settings.rate_limit.count 调大或设为 0。"
            )
        logger.info(
            "[intentiontrigger] 已加载。mode=%s threshold=%s model=%s",
            self.cfg("mode", "pool"),
            self.cfg("threshold", 0.5),
            self.cfg("model", "jev-latest"),
        )

    async def terminate(self):
        _GATE_STATE["impl"] = None
        # 唤醒所有等待中的消息（抑制），避免事件悬挂
        for pool in self._pools.values():
            for entry in pool:
                if not entry.future.done():
                    entry.future.set_result(None)
        self._pools.clear()
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        for task in self._flush_tasks:
            task.cancel()
        self._flush_tasks.clear()
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

    def _bot_desc(self, self_id: str = "") -> dict:
        names = [str(n).strip() for n in (self.cfg("bot_names", []) or []) if str(n).strip()]
        if not names and self_id:
            names = [str(self_id)]
        if not names:
            names = ["the bot"]
        desc = {"name": names[0], "aliases": names}
        if self_id:
            desc["bot_self_id"] = str(self_id)
        return desc

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
            return False  # 没有 key 时完全保持原生行为

        group_id = str(event.get_group_id() or "")
        whitelist = [str(g).strip() for g in (self.cfg("group_whitelist", []) or [])]
        # 白名单为空时不启用门控：必须显式列出要启用本插件的群
        if not whitelist:
            if not self._warned_empty_whitelist:
                logger.warning(
                    "[intentiontrigger] group_whitelist 为空，门控未对任何群生效。"
                    "请在插件配置中填写要启用的群号。"
                )
                self._warned_empty_whitelist = True
            return False
        if group_id not in whitelist:
            return False
        blacklist = [str(g).strip() for g in (self.cfg("group_blacklist", []) or [])]
        return group_id not in blacklist

    # ------------------------------------------------------------------ #
    # TypeSafe API 调用
    # ------------------------------------------------------------------ #

    async def _post_systemone(self, payload: dict) -> dict:
        """POST /v1/systemone，429/529 指数退避重试一次。"""
        base_url = str(self.cfg("api_base_url", "https://api.typesafe.ai")).rstrip("/")
        url = f"{base_url}/v1/systemone"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        max_attempts = 2
        for attempt in range(max_attempts):
            assert self._session and self._sem
            async with self._sem, self._session.post(
                url, json=payload, headers=headers
            ) as resp:
                body_text = await resp.text()
                if resp.status == 200:
                    return json.loads(body_text)
                if resp.status in (429, 529) and attempt < max_attempts - 1:
                    await asyncio.sleep(1.5**attempt)
                    continue
                raise RuntimeError(
                    f"TypeSafe API HTTP {resp.status}: {body_text[:200]}"
                )
        raise RuntimeError("TypeSafe API 调用重试耗尽")

    async def _judge_single(self, state: dict) -> tuple[float, str | None, dict]:
        """单条消息判定，返回 (noul 概率, 意图分类, usage)。"""
        instructions = self.cfg("custom_instructions", "") or DEFAULT_INSTRUCTIONS
        questions = {
            "wants_interaction": {
                "type": "noul",
                "instructions": instructions,
                "criteria": DEFAULT_CRITERIA,
            }
        }
        if self.cfg("enable_category_hint", True):
            questions["intent_category"] = {
                "type": "choice",
                "instructions": "Classify the speaker's intent toward the bot.",
                "criteria": CATEGORY_CRITERIA,
            }
        payload = {
            "state": state,
            "model": self.cfg("model", "jev-latest"),
            "questions": questions,
        }
        data = await self._post_systemone(payload)
        answers = data.get("answers") or {}
        answer = answers.get("wants_interaction") or {}
        value = answer.get("noul")
        if not isinstance(value, (int, float)):
            raise ValueError(f"Jev 响应缺少 noul 字段: {json.dumps(data)[:200]}")
        category = (answers.get("intent_category") or {}).get("choice")
        return float(value), category, self._usage_of(data)

    async def _judge_pool(
        self, state: dict, count: int
    ) -> tuple[list[float], list[str | None], dict]:
        """池内每条消息一个 noul + choice 问题，一次调用并行判定。

        返回 (按序 noul 概率列表, 按序意图分类列表, usage)。
        """
        instructions_tpl = self.cfg("custom_instructions", "") or DEFAULT_INSTRUCTIONS
        want_category = self.cfg("enable_category_hint", True)
        questions = {}
        for i in range(count):
            questions[f"msg_{i}"] = {
                "type": "noul",
                "instructions": (
                    f"{instructions_tpl}\n"
                    f"The message to judge is `messages[{i}]`. Judge ONLY "
                    f"messages[{i}]; use the other messages as conversational "
                    "context (e.g. short follow-ups like `在吗` or `快点啊` may "
                    "target the bot if earlier context shows the speaker calling it)."
                ),
                "criteria": DEFAULT_CRITERIA,
            }
            if want_category:
                questions[f"cat_{i}"] = {
                    "type": "choice",
                    "instructions": (
                        f"Classify the speaker's intent of `messages[{i}]` "
                        "toward the bot."
                    ),
                    "criteria": CATEGORY_CRITERIA,
                }
        payload = {
            "state": state,
            "model": self.cfg("model", "jev-latest"),
            "questions": questions,
        }
        data = await self._post_systemone(payload)
        answers = data.get("answers") or {}
        scores: list[float] = []
        categories: list[str | None] = []
        for i in range(count):
            value = (answers.get(f"msg_{i}") or {}).get("noul")
            if not isinstance(value, (int, float)):
                raise ValueError(
                    f"Jev 响应缺少 msg_{i}.noul 字段: {json.dumps(data)[:200]}"
                )
            scores.append(float(value))
            categories.append((answers.get(f"cat_{i}") or {}).get("choice"))
        return scores, categories, self._usage_of(data)

    @staticmethod
    def _usage_of(data: dict) -> dict:
        usage = data.get("usage") or {}
        return {
            "input_tokens": int(usage.get("input_tokens") or 0),
            "output_tokens": int(usage.get("output_tokens") or 0),
        }

    # ------------------------------------------------------------------ #
    # 消息池
    # ------------------------------------------------------------------ #

    def _lock_for(self, group_id: str) -> asyncio.Lock:
        if group_id not in self._pool_locks:
            self._pool_locks[group_id] = asyncio.Lock()
        return self._pool_locks[group_id]

    async def _submit_to_pool(self, group_id: str, entry: _PoolEntry):
        """消息入池；池满或超时触发一次批量判定。返回该消息的判定结果。"""
        pool_size = int(self.cfg("pool_size", 5))
        interval = float(self.cfg("flush_interval", 5))
        async with self._lock_for(group_id):
            pool = self._pools.setdefault(group_id, [])
            pool.append(entry)
            if len(pool) >= pool_size:
                entries = pool.copy()
                self._pools[group_id] = []
                timer = self._timers.pop(group_id, None)
                if timer:
                    timer.cancel()
                self._spawn_flush(group_id, entries)
            elif group_id not in self._timers:
                self._timers[group_id] = asyncio.create_task(
                    self._flush_after(group_id, interval)
                )
        try:
            return await asyncio.wait_for(
                entry.future, timeout=interval + float(self.cfg("timeout_seconds", 15)) + 10
            )
        except asyncio.TimeoutError:
            return None  # 判定超时，按无意图处理

    async def _flush_after(self, group_id: str, interval: float):
        try:
            await asyncio.sleep(interval)
            async with self._lock_for(group_id):
                self._timers.pop(group_id, None)
                pool = self._pools.get(group_id) or []
                if pool:
                    entries = pool.copy()
                    self._pools[group_id] = []
                    self._spawn_flush(group_id, entries)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # 兜底：定时器崩溃时唤醒池内所有消息，避免 future 悬挂到 wait_for 超时
            logger.error(f"[intentiontrigger] 消息池定时器异常: {e}")
            await self._drain_pool_quietly(group_id)

    async def _drain_pool_quietly(self, group_id: str):
        """异常兜底：清空该群的消息池并把所有 future 置为 None（按错误路径处理）。"""
        try:
            async with self._lock_for(group_id):
                self._timers.pop(group_id, None)
                pool = self._pools.get(group_id) or []
                self._pools[group_id] = []
        except Exception as drain_err:
            logger.error(f"[intentiontrigger] 清空消息池失败: {drain_err}")
            return
        for entry in pool:
            if entry.future and not entry.future.done():
                entry.future.set_result(None)

    def _spawn_flush(self, group_id: str, entries: list[_PoolEntry]):
        """在后台执行批量判定（不阻塞入池协程）。"""
        task = asyncio.create_task(self._flush_pool_safe(group_id, entries))
        self._flush_tasks.add(task)
        task.add_done_callback(self._flush_tasks.discard)

    async def _flush_pool_safe(self, group_id: str, entries: list[_PoolEntry]):
        """_flush_pool 的异常兜底外壳，保证任何情况下 future 都被 resolve。"""
        try:
            await self._flush_pool(group_id, entries)
        except Exception as e:
            self.stats["errors"] += 1
            logger.error(f"[intentiontrigger] 消息池判定流程异常: {e}")
            for entry in entries:
                if entry.future and not entry.future.done():
                    entry.future.set_result(None)

    async def _flush_pool(self, group_id: str, entries: list[_PoolEntry]):
        threshold = float(self.cfg("threshold", 0.5))
        state = {
            "bot": self._bot_desc(),
            "chat": {"type": "group", "group_id": group_id},
            "messages": [
                {
                    "index": i,
                    "sender_name": e.sender_name or e.sender_id,
                    "sender_id": e.sender_id,
                    "text": e.text,
                }
                for i, e in enumerate(entries)
            ],
        }
        t0 = time.monotonic()
        try:
            scores, categories, usage = await self._judge_pool(state, len(entries))
        except Exception as e:
            self.stats["errors"] += 1
            logger.warning(f"[intentiontrigger] Jev 池判定失败: {e}")
            self._record(
                _CallRecord(
                    ts=time.time(),
                    group_id=group_id,
                    mode="pool",
                    latency_ms=(time.monotonic() - t0) * 1000,
                    model=self.cfg("model", "jev-latest"),
                    usage={},
                    messages=[
                        {"text": e.text, "sender": e.sender_name, "noul": None, "triggered": False}
                        for e in entries
                    ],
                    error=str(e),
                )
            )
            pass_through = self.cfg("on_error", "suppress") == "pass_to_llm"
            for entry in entries:
                if entry.future and not entry.future.done():
                    entry.future.set_result((-1.0, pass_through, None))
            return

        latency_ms = (time.monotonic() - t0) * 1000
        triggered_flags = self._decide_triggers(scores, threshold)
        self._record(
            _CallRecord(
                ts=time.time(),
                group_id=group_id,
                mode="pool",
                latency_ms=latency_ms,
                model=self.cfg("model", "jev-latest"),
                usage=usage,
                messages=[
                    {
                        "text": e.text,
                        "sender": e.sender_name,
                        "noul": round(s, 4),
                        "triggered": trig,
                        "category": cat,
                    }
                    for e, s, trig, cat in zip(
                        entries, scores, triggered_flags, categories
                    )
                ],
                error=None,
            )
        )
        for entry, score, trig, cat in zip(
            entries, scores, triggered_flags, categories
        ):
            if entry.future and not entry.future.done():
                entry.future.set_result((score, trig, cat))

    def _decide_triggers(self, scores: list[float], threshold: float) -> list[bool]:
        """把分数转为放行标记；one_reply_per_flush 时同池只放行最高分。"""
        flags = [s >= threshold for s in scores]
        if self.cfg("one_reply_per_flush", True):
            candidates = [i for i, f in enumerate(flags) if f]
            if len(candidates) > 1:
                best = max(candidates, key=lambda i: scores[i])
                for i in candidates:
                    flags[i] = i == best
        return flags

    # ------------------------------------------------------------------ #
    # 核心：群消息意图门控
    # ------------------------------------------------------------------ #

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE, priority=GATE_HANDLER_PRIORITY
    )
    @filter.custom_filter(IntentionGateFilter, False)
    async def gate_group_message(self, event: AstrMessageEvent):
        """对未被显式唤醒的群消息做意图识别，决定是否交给主 LLM。"""
        self.stats["checked"] += 1
        threshold = float(self.cfg("threshold", 0.5))
        mode = self.cfg("mode", "pool")

        if mode == "single":
            text = (event.message_str or "").strip()
            state = {
                "bot": self._bot_desc(event.get_self_id()),
                "chat": {
                    "type": "group",
                    "group_id": str(event.get_group_id() or ""),
                    "speaker_name": event.get_sender_name() or "",
                    "speaker_id": str(event.get_sender_id()),
                },
                "message": text,
            }
            t0 = time.monotonic()
            try:
                score, category, usage = await self._judge_single(state)
            except Exception as e:
                self.stats["errors"] += 1
                logger.warning(f"[intentiontrigger] Jev 调用失败: {e}")
                self._record(
                    _CallRecord(
                        ts=time.time(),
                        group_id=str(event.get_group_id() or ""),
                        mode="single",
                        latency_ms=(time.monotonic() - t0) * 1000,
                        model=self.cfg("model", "jev-latest"),
                        usage={},
                        messages=[
                            {
                                "text": text,
                                "sender": event.get_sender_name(),
                                "noul": None,
                                "triggered": False,
                            }
                        ],
                        error=str(e),
                    )
                )
                if self.cfg("on_error", "suppress") == "pass_to_llm":
                    event.is_at_or_wake_command = True
                return
            trig = score >= threshold
            self._record(
                _CallRecord(
                    ts=time.time(),
                    group_id=str(event.get_group_id() or ""),
                    mode="single",
                    latency_ms=(time.monotonic() - t0) * 1000,
                    model=self.cfg("model", "jev-latest"),
                    usage=usage,
                    messages=[
                        {
                            "text": text,
                            "sender": event.get_sender_name(),
                            "noul": round(score, 4),
                            "triggered": trig,
                            "category": category,
                        }
                    ],
                    error=None,
                )
            )
        else:
            loop = asyncio.get_running_loop()
            entry = _PoolEntry(
                text=(event.message_str or "").strip(),
                sender_id=str(event.get_sender_id()),
                sender_name=event.get_sender_name() or "",
                future=loop.create_future(),
            )
            result = await self._submit_to_pool(str(event.get_group_id() or ""), entry)
            if result is None:  # 超时/插件卸载
                self.stats["errors"] += 1
                if self.cfg("on_error", "suppress") == "pass_to_llm":
                    event.is_at_or_wake_command = True
                return
            score, trig, category = result
            if score < 0:  # API 错误（on_error 已在 flush 端处理 pass_through 语义）
                if trig:
                    event.is_at_or_wake_command = True
                return

        if trig:
            self.stats["triggered"] += 1
            # 关键一步：让 ProcessStage 把这条消息当作被唤醒的消息交给主 LLM
            event.is_at_or_wake_command = True
            # 意图分类供 on_llm_request 钩子追加风格提示
            if category and category in CATEGORY_CRITERIA and category != "not_addressed":
                event.set_extra("intention_category", category)
            if self.cfg("log_decisions", False):
                logger.info(
                    "[intentiontrigger] ✅ 触发 (score=%.3f>=%.3f, cat=%s): %r",
                    score,
                    threshold,
                    category,
                    event.message_str,
                )
        else:
            self.stats["suppressed"] += 1
            if self.cfg("log_decisions", False):
                logger.info(
                    "[intentiontrigger] ⛔ 抑制 (score=%.3f<%.3f): %r",
                    score,
                    threshold,
                    event.message_str,
                )
            if self.cfg("block_other_handlers", False):
                # 可选：连同其他插件对普通群消息的监听一起拦截（默认关闭）
                event.stop_event()

    @filter.on_llm_request()
    async def inject_category_hint(self, event: AstrMessageEvent, req) -> None:
        """把意图分类的风格提示追加到主 LLM 输入末尾（不动 system prompt，避免破坏前缀缓存）。"""
        if not self.cfg("enable_category_hint", True):
            return
        category = event.get_extra("intention_category")
        if not category:
            return
        hints = self.cfg("category_hints", {}) or DEFAULT_CATEGORY_HINTS
        hint = (hints.get(category) if isinstance(hints, dict) else None) or (
            DEFAULT_CATEGORY_HINTS.get(category)
        )
        if not hint:
            return
        suffix = f"\n{hint}"
        if req.prompt:
            if hint in req.prompt:
                return  # 避免重试/重入时重复追加
            req.prompt = f"{req.prompt}{suffix}"
        else:
            req.prompt = hint

    # ------------------------------------------------------------------ #
    # 统计记录与 WebUI 接口
    # ------------------------------------------------------------------ #

    def _record(self, record: _CallRecord):
        """统计入内存环形缓冲并追加落盘（按行数轮转，落盘失败静默忽略）。"""
        self.recent_calls.append(record)
        if self._stats_file is None:
            return
        try:
            max_lines = int(self.cfg("stats_max_lines", 5000))
            if max_lines > 0 and self._stats_lines + 1 > max_lines:
                self._rotate_stats_file(keep=max_lines // 2)
            self._stats_lines += 1
            with open(self._stats_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.debug(f"[intentiontrigger] 统计落盘失败: {e}")

    def _rotate_stats_file(self, keep: int):
        """保留 stats.jsonl 的最后 keep 行，防止长跑膨胀。"""
        try:
            with open(self._stats_file, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > keep:
                with open(self._stats_file, "w", encoding="utf-8") as f:
                    f.writelines(lines[-keep:])
                self._stats_lines = keep
                logger.info(
                    "[intentiontrigger] stats.jsonl 已轮转，保留最近 %d 条。", keep
                )
        except Exception as e:
            logger.debug(f"[intentiontrigger] stats.jsonl 轮转失败: {e}")

    def _overview(self) -> dict:
        total_msgs = triggered = errored = 0
        latencies = []  # 仅成功调用，避免超时/错误污染健康度
        scores = []
        input_tokens = output_tokens = 0
        categories: dict[str, int] = {}
        by_group: dict[str, dict] = {}
        for rec in self.recent_calls:
            if rec.error:
                errored += 1
            else:
                latencies.append(rec.latency_ms)
            usage = rec.usage or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            g = by_group.setdefault(
                rec.group_id or "未知",
                {"calls": 0, "messages": 0, "triggered": 0},
            )
            g["calls"] += 1
            for m in rec.messages:
                total_msgs += 1
                g["messages"] += 1
                if m.get("noul") is not None:
                    scores.append(m["noul"])
                cat = m.get("category")
                if cat:
                    categories[cat] = categories.get(cat, 0) + 1
                if m.get("triggered"):
                    triggered += 1
                    g["triggered"] += 1
        bins = [0] * 10
        for s in scores:
            bins[min(int(s * 10), 9)] += 1
        price_in = float(self.cfg("price_per_mtok_input", 0.042))
        price_out = float(self.cfg("price_per_mtok_output", 0.0))
        est_cost = input_tokens / 1e6 * price_in + output_tokens / 1e6 * price_out
        return {
            "config": {
                "mode": self.cfg("mode", "pool"),
                "threshold": float(self.cfg("threshold", 0.5)),
                "pool_size": int(self.cfg("pool_size", 5)),
                "flush_interval": float(self.cfg("flush_interval", 5)),
                "one_reply_per_flush": bool(self.cfg("one_reply_per_flush", True)),
                "model": self.cfg("model", "jev-latest"),
                "bot_names": self.cfg("bot_names", []) or [],
                "group_whitelist": self.cfg("group_whitelist", []) or [],
                "api_key_configured": bool(self.api_key),
            },
            "totals": {
                "calls": len(self.recent_calls),
                "messages": total_msgs,
                "triggered": triggered,
                "suppressed": total_msgs - triggered,
                "errors": errored,
                "trigger_rate": round(triggered / total_msgs, 4) if total_msgs else None,
                "avg_latency_ms": round(sum(latencies) / len(latencies), 1)
                if latencies
                else None,
            },
            "score_histogram": {
                "bins": [f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)],
                "counts": bins,
            },
            "costs": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "est_cost_usd": round(est_cost, 6),
                "price_per_mtok_input": price_in,
                "price_per_mtok_output": price_out,
                "note": "估算仅覆盖最近 %d 次调用（内存缓冲），历史数据见 stats.jsonl"
                % RECENT_CALLS_MAX,
            },
            "categories": categories,
            "by_group": [
                {"group_id": k, **v}
                for k, v in sorted(by_group.items(), key=lambda kv: -kv[1]["messages"])
            ],
            "since_restart": {
                "checked": self.stats["checked"],
                "triggered": self.stats["triggered"],
                "suppressed": self.stats["suppressed"],
                "errors": self.stats["errors"],
            },
        }

    async def _api_overview(self):
        return json_response({"status": "ok", "data": self._overview()})

    async def _api_recent(self):
        try:
            limit = max(1, min(100, int(request.query.get("limit", 30))))
        except (TypeError, ValueError):
            limit = 30
        items = [r.to_dict() for r in list(self.recent_calls)[-limit:]]
        items.reverse()  # 最新在前
        return json_response({"status": "ok", "data": items})

    async def _api_test(self):
        # 注：dashboard 自研 HTTP 层下 request.json(default=...) 会静默失败，
        # 这里直接读原始 body 自行解析
        try:
            raw = await request.body()
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        raw = str(body.get("text", "")).strip()
        if not raw:
            return error_response("缺少 text 字段")
        if not self.api_key:
            return error_response("未配置 TypeSafe API Key")
        threshold = float(self.cfg("threshold", 0.5))
        t0 = time.monotonic()
        try:
            messages, usage = await self._judge_test_text(raw)
        except Exception as e:
            return error_response(f"调用失败: {e}")
        latency_ms = (time.monotonic() - t0) * 1000
        self._record(
            _CallRecord(
                ts=time.time(),
                group_id="(手动测试)",
                mode="multi" if len(messages) > 1 else "single",
                latency_ms=latency_ms,
                model=self.cfg("model", "jev-latest"),
                usage=usage,
                messages=[
                    {
                        "text": m["text"],
                        "sender": "tester",
                        "noul": round(m["noul"], 4),
                        "triggered": m["noul"] >= threshold,
                        "category": m.get("category"),
                    }
                    for m in messages
                ],
                error=None,
            )
        )
        return json_response(
            {
                "status": "ok",
                "data": {
                    "threshold": threshold,
                    "latency_ms": round(latency_ms, 1),
                    "mode": "multi" if len(messages) > 1 else "single",
                    "messages": [
                        {
                            "text": m["text"],
                            "noul": round(m["noul"], 4),
                            "triggered": m["noul"] >= threshold,
                            "category": m.get("category"),
                        }
                        for m in messages
                    ],
                },
            }
        )

    async def _judge_test_text(self, raw: str) -> tuple[list[dict], dict]:
        """手动测试判定：多行文本按「一行一条消息的群聊上下文」走池判定，
        与生产 pool 模式一致；单行走 single 判定。"""
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        if len(lines) > 1:
            state = {
                "bot": self._bot_desc(),
                "chat": {"type": "group", "group_id": "(手动测试)"},
                "messages": [
                    {"index": i, "sender_name": f"speaker{i + 1}", "sender_id": str(i),
                     "text": t}
                    for i, t in enumerate(lines)
                ],
            }
            scores, categories, usage = await self._judge_pool(state, len(lines))
            return (
                [
                    {"text": t, "noul": s, "category": c}
                    for t, s, c in zip(lines, scores, categories)
                ],
                usage,
            )
        state = {
            "bot": self._bot_desc(),
            "chat": {"type": "group", "speaker_name": "tester", "speaker_id": "0"},
            "message": lines[0],
        }
        score, category, usage = await self._judge_single(state)
        return ([{"text": lines[0], "noul": score, "category": category}], usage)

    # ------------------------------------------------------------------ #
    # 管理指令：/intention status|test|on|off
    # ------------------------------------------------------------------ #

    @filter.command("intention", alias={"itrig"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def intention_command(self, event: AstrMessageEvent):
        """意图触发器管理指令：/intention [status|test <文本>|on|off]"""
        parts = (event.message_str or "").strip().split(maxsplit=2)
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
                yield event.plain_result(
                    "用法: /intention test <文本>\n"
                    "多行文本（换行分隔）会按群聊上下文走池判定，每行各出分数，"
                    "与生产模式一致。"
                )
                return
            if not self.api_key:
                yield event.plain_result("尚未配置 TypeSafe API Key，无法测试。")
                return
            try:
                messages, usage = await self._judge_test_text(rest)
            except Exception as e:
                yield event.plain_result(f"调用失败: {e}")
                return
            threshold = float(self.cfg("threshold", 0.5))
            cat_names = {
                "question": "提问",
                "chitchat": "闲聊",
                "command": "指令",
                "meme": "玩梗",
                "not_addressed": "非对bot",
            }
            lines = []
            for i, m in enumerate(messages, 1):
                verdict = "✅触发" if m["noul"] >= threshold else "⛔抑制"
                cat = cat_names.get(m.get("category") or "", "")
                lines.append(
                    f"{i}. {verdict} noul={m['noul']:.3f}"
                    + (f" [{cat}]" if cat else "")
                    + f" {m['text'][:40]}"
                )
            yield event.plain_result(
                f"阈值 {threshold}，"
                f"tokens: in={usage.get('input_tokens', 0)}/out={usage.get('output_tokens', 0)}\n"
                + "\n".join(lines)
            )

        else:  # status
            ov = self._overview()
            c, t = ov["config"], ov["totals"]
            cost = ov["costs"]
            lines = [
                f"启用: {self.cfg('enable', True)}  模式: {c['mode']}"
                + (
                    f"（{c['pool_size']} 条/池，{c['flush_interval']}s 超时）"
                    if c["mode"] == "pool"
                    else ""
                ),
                f"模型: {c['model']}  阈值: {c['threshold']}",
                f"API Key: {'已配置' if c['api_key_configured'] else '❌ 未配置'}",
                f"白名单群: {c['group_whitelist'] or '（空，未生效！）'}",
                f"机器人称呼: {c['bot_names'] or '（未配置）'}",
                f"统计: 检查 {t['messages']} / 触发 {t['triggered']} / "
                f"抑制 {t['suppressed']} / 错误 {t['errors']}"
                + (
                    f" / 触发率 {t['trigger_rate']:.1%} / 平均耗时 {t['avg_latency_ms']}ms"
                    if t["avg_latency_ms"] is not None
                    else ""
                ),
                f"花费(近期): in={cost['input_tokens']} tok / out={cost['output_tokens']} tok"
                f" ≈ ${cost['est_cost_usd']:.4f}",
                "详细统计页: WebUI → 插件 → astrbot_plugin_intentiontrigger → 统计页",
            ]
            yield event.plain_result("\n".join(lines))
