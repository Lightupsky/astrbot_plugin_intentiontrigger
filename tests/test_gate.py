"""astrbot_plugin_intentiontrigger 门控逻辑端到端测试。

不依赖真实 TypeSafe API：用本地 aiohttp 服务模拟 /v1/systemone。
运行方式（在 AstrBot 的 venv 中）:
    python tests/test_gate.py
"""

import asyncio
import json
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = ROOT.parent / "AstrBot"
sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(ROOT))

from aiohttp import web

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.message_type import MessageType

import main as plugin_module

PASS = []
FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  ({detail})" if detail and not cond else ""))


class FakeEvent:
    """实现被插件用到的事件接口子集。"""

    def __init__(self, text="hello bot", group=True, at_bot=False, sender="10001",
                 self_id="42", group_id="888", sender_name="tester"):
        self.message_str = text
        self.is_at_or_wake_command = at_bot
        self._stopped = False
        self._sender = sender
        self._self = self_id
        self._group = group
        self._group_id = group_id
        self._sender_name = sender_name

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE if self._group else MessageType.FRIEND_MESSAGE

    def get_sender_id(self) -> str:
        return self._sender

    def get_self_id(self) -> str:
        return self._self

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def stop_event(self):
        self._stopped = True

    def is_stopped(self) -> bool:
        return self._stopped


class FakeContext:
    def get_config(self):
        return {"platform_settings": {"rate_limit": {"count": 0, "time": 60}}}


NOUL_VALUE = {"v": 0.9}


async def fake_systemone(request: web.Request) -> web.Response:
    body = await request.json()
    assert request.headers["Authorization"].startswith("Bearer ")
    assert body["model"]
    assert body["questions"]["wants_interaction"]["type"] == "noul"
    return web.json_response(
        {
            "model": "jev-test",
            "answers": {"wants_interaction": {"type": "noul", "noul": NOUL_VALUE["v"]}},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
    )


def make_plugin(config_overrides: dict | None = None):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    cfg = AstrBotConfig(config_path=tempfile.mktemp(suffix=".json"), schema=schema)
    cfg.update(config_overrides or {})
    plugin = plugin_module.IntentionTrigger(FakeContext(), cfg)
    _PLUGINS.append(plugin)
    return plugin


_PLUGINS: list = []


async def main() -> int:
    # 起 mock API
    app = web.Application()
    app.router.add_post("/v1/systemone", fake_systemone)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18971)
    await site.start()
    base = "http://127.0.0.1:18971"

    plugin = make_plugin(
        {"api_key": "test-key", "api_base_url": base, "log_decisions": True}
    )
    await plugin.initialize()

    # ---- should_gate 分支 ----
    ev = FakeEvent()
    check("普通群消息需要门控", plugin.should_gate(ev))
    check("@机器人的消息跳过门控", not plugin.should_gate(FakeEvent(at_bot=True)))
    check("私聊消息跳过门控", not plugin.should_gate(FakeEvent(group=False)))
    check("空文本跳过门控", not plugin.should_gate(FakeEvent(text="   ")))
    check("机器人自身消息跳过门控", not plugin.should_gate(FakeEvent(sender="42")))

    p2 = make_plugin({"api_key": "test-key", "api_base_url": base, "group_blacklist": ["888"]})
    await p2.initialize()
    check("黑名单群跳过门控", not p2.should_gate(FakeEvent()))
    p3 = make_plugin({"api_key": "test-key", "api_base_url": base, "group_whitelist": ["777"]})
    await p3.initialize()
    check("白名单外的群跳过门控", not p3.should_gate(FakeEvent()))
    check("白名单内的群需要门控", p3.should_gate(FakeEvent(group_id="777")))
    p4 = make_plugin({"api_key": "test-key", "api_base_url": base, "enable": False})
    await p4.initialize()
    check("插件禁用时跳过门控", not p4.should_gate(FakeEvent()))
    p5 = make_plugin({"api_base_url": base})  # 无 key
    await p5.initialize()
    check("无 API key 时跳过门控(降级为原生行为)", not p5.should_gate(FakeEvent()))

    # ---- handler 端到端 ----
    ev = FakeEvent(text="小星，今天天气怎么样？")
    await plugin.gate_group_message(ev)
    check("高意图消息 → is_at_or_wake_command 置 True", ev.is_at_or_wake_command is True)
    check("高意图消息未被停止", not ev.is_stopped())
    check("统计: checked=1, triggered=1",
          plugin.stats["checked"] == 1 and plugin.stats["triggered"] == 1)

    NOUL_VALUE["v"] = 0.05
    ev2 = FakeEvent(text="今天午饭吃什么")
    await plugin.gate_group_message(ev2)
    check("低意图消息 → 不唤醒 LLM", ev2.is_at_or_wake_command is False)
    check("低意图消息默认不拦截其他插件", not ev2.is_stopped())
    check("统计: suppressed=1", plugin.stats["suppressed"] == 1)

    # block_other_handlers 开启时会 stop
    p6 = make_plugin({"api_key": "test-key", "api_base_url": base, "block_other_handlers": True})
    await p6.initialize()
    ev3 = FakeEvent(text="闲聊")
    await p6.gate_group_message(ev3)
    check("block_other_handlers=True 时低意图消息被停止", ev3.is_stopped())

    # 阈值边界：score == threshold 触发
    NOUL_VALUE["v"] = 0.5
    ev4 = FakeEvent()
    await plugin.gate_group_message(ev4)
    check("score == threshold 触发（>= 语义）", ev4.is_at_or_wake_command is True)

    # ---- API 故障路径 ----
    plugin_fail = make_plugin({"api_key": "test-key", "api_base_url": "http://127.0.0.1:1",
                               "timeout_seconds": 2})
    await plugin_fail.initialize()
    ev5 = FakeEvent()
    await plugin_fail.gate_group_message(ev5)
    check("API 故障 + on_error=suppress → 不唤醒", ev5.is_at_or_wake_command is False)
    plugin_fail2 = make_plugin({"api_key": "test-key", "api_base_url": "http://127.0.0.1:1",
                                "timeout_seconds": 2, "on_error": "pass_to_llm"})
    await plugin_fail2.initialize()
    ev6 = FakeEvent()
    await plugin_fail2.gate_group_message(ev6)
    check("API 故障 + on_error=pass_to_llm → 放行给 LLM", ev6.is_at_or_wake_command is True)

    # ---- 自定义 filter 走 _GATE_STATE ----
    flt = plugin_module.IntentionGateFilter(raise_error=False)
    check("filter: 普通群消息放行", flt.filter(FakeEvent(), cfg=None))
    check("filter: @ 消息不放行", not flt.filter(FakeEvent(at_bot=True), cfg=None))

    await plugin.terminate()
    for p in _PLUGINS:
        await p.terminate()
    await runner.cleanup()

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
