"""astrbot_plugin_intentiontrigger 门控逻辑端到端测试（mock API，不依赖真实 key）。

覆盖：should_gate 白名单语义、消息池攒批/超时/放行、one_reply_per_flush、
故障降级、_decide_triggers、自定义 filter。

运行方式（在 AstrBot 的 venv 中）:
    python tests/test_gate.py
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = ROOT.parent / "AstrBot"
# 开发机上加本地 AstrBot 源码路径；生产环境直接用已安装的 astrbot 包
if ASTRBOT_ROOT.exists():
    sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(ROOT))

from aiohttp import web

from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.message_type import MessageType

import main as plugin_module

PASS = []
FAIL = []

# mock 服务返回的每条消息 noul 值（池模式下按索引取）
POOL_NOUL = [0.9, 0.1, 0.8, 0.2, 0.7]
POOL_CAT = ["question", "chitchat", "command", "meme", "not_addressed"]
SINGLE_NOUL = {"v": 0.9}
CALLS = {"n": 0}  # API 调用计数
USAGE = {"input_tokens": 321, "output_tokens": 12}


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
        self._extras: dict = {}

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

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

    def register_web_api(self, *args, **kwargs):
        pass


async def fake_systemone(request: web.Request) -> web.Response:
    body = await request.json()
    assert request.headers["Authorization"].startswith("Bearer ")
    CALLS["n"] += 1
    # bot 能力信息必须存在（祈使句能力判别的依据）
    assert body["state"]["bot"]["capabilities"], "state.bot.capabilities 缺失"
    answers = {}
    for key, q in body["questions"].items():
        if q["type"] == "noul":
            if key == "wants_interaction":
                answers[key] = {"type": "noul", "noul": SINGLE_NOUL["v"]}
            else:  # 池模式 msg_i
                i = int(key.split("_")[1])
                answers[key] = {"type": "noul", "noul": POOL_NOUL[i]}
        elif q["type"] == "choice":
            if key == "intent_category":
                answers[key] = {
                    "type": "choice",
                    "choice": "question",
                    "probabilities": {"question": 1.0},
                }
            else:  # 池模式 cat_i
                i = int(key.split("_")[1])
                answers[key] = {
                    "type": "choice",
                    "choice": POOL_CAT[i],
                    "probabilities": {POOL_CAT[i]: 1.0},
                }
    return web.json_response(
        {
            "model": "jev-test",
            "answers": answers,
            "usage": USAGE,
        }
    )


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}" + (f"  ({detail})" if detail and not cond else ""))


_PLUGINS: list = []


def make_plugin(config_overrides: dict | None = None):
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    cfg = AstrBotConfig(config_path=tempfile.mktemp(suffix=".json"), schema=schema)
    base = {
        "api_key": "test-key",
        "api_base_url": "http://127.0.0.1:18971",
        "group_whitelist": ["888"],  # 新语义：白名单必填
        "bot_names": ["鸭嘴兽"],
    }
    base.update(config_overrides or {})
    cfg.update(base)
    plugin = plugin_module.IntentionTrigger(FakeContext(), cfg)
    _PLUGINS.append(plugin)
    return plugin


async def main() -> int:
    app = web.Application()
    app.router.add_post("/v1/systemone", fake_systemone)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18971)
    await site.start()

    # ---------------- should_gate 分支 ----------------
    plugin = make_plugin({"log_decisions": True})
    await plugin.initialize()
    check("白名单内普通群消息需要门控", plugin.should_gate(FakeEvent()))
    check("@机器人的消息跳过门控", not plugin.should_gate(FakeEvent(at_bot=True)))
    check("私聊消息跳过门控", not plugin.should_gate(FakeEvent(group=False)))
    check("空文本跳过门控", not plugin.should_gate(FakeEvent(text="   ")))
    check("机器人自身消息跳过门控", not plugin.should_gate(FakeEvent(sender="42")))
    check("白名单外的群跳过门控", not plugin.should_gate(FakeEvent(group_id="999")))

    p2 = make_plugin({"group_blacklist": ["888"]})
    await p2.initialize()
    check("黑名单优先于白名单", not p2.should_gate(FakeEvent()))
    p3 = make_plugin({"group_whitelist": []})
    await p3.initialize()
    check("白名单为空时门控不生效（安全默认）", not p3.should_gate(FakeEvent()))
    p4 = make_plugin({"enable": False})
    await p4.initialize()
    check("插件禁用时跳过门控", not p4.should_gate(FakeEvent()))
    p5 = make_plugin({"api_key": ""})
    await p5.initialize()
    check("无 API key 时跳过门控（降级为原生行为）", not p5.should_gate(FakeEvent()))

    # ---------------- 池模式端到端（攒满 5 条一次调用） ----------------
    POOL_NOUL[:] = [0.9, 0.1, 0.8, 0.2, 0.7]
    CALLS["n"] = 0
    events = [FakeEvent(text=f"消息{i}", sender=f"1000{i}") for i in range(5)]
    tasks = [asyncio.create_task(plugin.gate_group_message(e)) for e in events]
    await asyncio.gather(*tasks)
    check("池模式: 5 条消息只触发 1 次 API 调用", CALLS["n"] == 1, f"实际 {CALLS['n']}")
    for i, e in enumerate(events):
        want = POOL_NOUL[i] >= 0.5 and (not plugin.cfg("one_reply_per_flush", True)
                                        or i == 0)  # one_reply 只放行最高分 msg_0(0.9)
        check(f"池模式: 消息{i}(noul={POOL_NOUL[i]}) 放行={'是' if want else '否'}",
              e.is_at_or_wake_command == want)
    check("池模式: 统计 triggered=1/suppressed=4",
          plugin.stats["triggered"] == 1 and plugin.stats["suppressed"] == 4,
          str(plugin.stats))
    check("池模式: 统计调用记录 1 条含 5 条消息",
          len(plugin.recent_calls) == 1 and len(plugin.recent_calls[-1].messages) == 5)
    check("池模式: usage 已入库", plugin.recent_calls[-1].usage == USAGE)
    check("池模式: 分类已入库", plugin.recent_calls[-1].messages[0].get("category") == "question")
    check("池模式: 放行消息带分类 extra",
          events[0].get_extra("intention_category") == "question")
    check("池模式: 抑制消息不带分类 extra",
          events[1].get_extra("intention_category") is None)

    # ---------------- on_llm_request 风格提示追加 ----------------
    class FakeReq:
        def __init__(self):
            self.prompt = "原始用户消息"
            self.system_prompt = "系统提示"

    req = FakeReq()
    await plugin.inject_category_hint(FakeEvent(at_bot=True), req)
    check("llm 钩子: 无 extra 时不改 prompt", req.prompt == "原始用户消息")
    ev_hint = FakeEvent()
    ev_hint.set_extra("intention_category", "meme")
    req2 = FakeReq()
    await plugin.inject_category_hint(ev_hint, req2)
    check("llm 钩子: 追加提示到 prompt 末尾",
          req2.prompt.startswith("原始用户消息") and "玩梗" in req2.prompt)
    check("llm 钩子: 不动 system prompt", req2.system_prompt == "系统提示")
    await plugin.inject_category_hint(ev_hint, req2)
    check("llm 钩子: 重入不重复追加", req2.prompt.count("意图提示") == 1)

    # ---------------- 池模式: 不足 pool_size 条走超时 flush ----------------
    POOL_NOUL[:] = [0.05, 0.05, 0.05]
    CALLS["n"] = 0
    p6 = make_plugin({"pool_size": 5, "flush_interval": 1})
    await p6.initialize()
    ev = FakeEvent(text="闲聊一条")
    t0 = asyncio.get_running_loop().time()
    await p6.gate_group_message(ev)
    dt = asyncio.get_running_loop().time() - t0
    check("池模式: 不足池大小时超时后仍完成判定", CALLS["n"] == 1)
    check("池模式: 超时判定延迟 ≈ flush_interval", 0.8 < dt < 5, f"{dt:.1f}s")
    check("池模式: 低分消息不唤醒 LLM", ev.is_at_or_wake_command is False)

    # ---------------- one_reply_per_flush=False: 同池多条放行 ----------------
    POOL_NOUL[:] = [0.9, 0.1, 0.8]
    p7 = make_plugin({"pool_size": 3, "one_reply_per_flush": False})
    await p7.initialize()
    evs = [FakeEvent(text=f"m{i}") for i in range(3)]
    await asyncio.gather(*[p7.gate_group_message(e) for e in evs])
    check("one_reply_per_flush=False: 高分全放行",
          evs[0].is_at_or_wake_command and evs[2].is_at_or_wake_command
          and not evs[1].is_at_or_wake_command)

    # ---------------- single 模式 ----------------
    SINGLE_NOUL["v"] = 0.9
    CALLS["n"] = 0
    p8 = make_plugin({"mode": "single"})
    await p8.initialize()
    ev = FakeEvent(text="鸭嘴兽在吗")
    await p8.gate_group_message(ev)
    check("single 模式: 高意图消息唤醒 LLM", ev.is_at_or_wake_command is True)
    check("single 模式: 单独一次 API 调用", CALLS["n"] == 1)
    SINGLE_NOUL["v"] = 0.05
    ev = FakeEvent(text="闲聊")
    await p8.gate_group_message(ev)
    check("single 模式: 低意图消息不唤醒", ev.is_at_or_wake_command is False)

    # ---------------- _decide_triggers 单元 ----------------
    flags = p7._decide_triggers([0.5, 0.49, 0.51], 0.5)
    check("decide: score==threshold 触发（>= 语义）", flags == [True, False, True], str(flags))
    p9 = make_plugin({"one_reply_per_flush": True})
    await p9.initialize()
    flags = p9._decide_triggers([0.6, 0.9, 0.7], 0.5)
    check("decide: one_reply_per_flush 只放行最高分", flags == [False, True, False], str(flags))

    # ---------------- API 故障路径 ----------------
    p_fail = make_plugin({"api_base_url": "http://127.0.0.1:1", "timeout_seconds": 2})
    await p_fail.initialize()
    ev = FakeEvent()
    await p_fail.gate_group_message(ev)
    check("API 故障 + suppress → 不唤醒", ev.is_at_or_wake_command is False)
    p_fail2 = make_plugin({"api_base_url": "http://127.0.0.1:1", "timeout_seconds": 2,
                           "on_error": "pass_to_llm"})
    await p_fail2.initialize()
    ev = FakeEvent()
    await p_fail2.gate_group_message(ev)
    check("API 故障 + pass_to_llm → 放行给 LLM", ev.is_at_or_wake_command is True)

    # ---------------- 自定义 filter 走 _GATE_STATE ----------------
    flt = plugin_module.IntentionGateFilter(raise_error=False)
    check("filter: 白名单内普通群消息放行", flt.filter(FakeEvent(), cfg=None))
    check("filter: @ 消息不放行", not flt.filter(FakeEvent(at_bot=True), cfg=None))

    # ---------------- 统计 overview ----------------
    ov = p8._overview()
    check("overview: 含配置与直方图", "config" in ov and "score_histogram" in ov
          and "totals" in ov)
    check("overview: bot_names 正确", ov["config"]["bot_names"] == ["鸭嘴兽"])
    check("overview: 含花费估算与分类分布",
          "costs" in ov and "categories" in ov and ov["costs"]["est_cost_usd"] > 0)
    check("bot_desc: 含兜底能力描述",
          "capabilities" in p8._bot_desc() and p8._bot_desc()["capabilities"])
    p_cap = make_plugin({"bot_capabilities": ["聊天问答", "查询快递"]})
    await p_cap.initialize()
    check("bot_desc: 自定义能力生效",
          p_cap._bot_desc()["capabilities"] == ["聊天问答", "查询快递"])
    await p_cap.terminate()

    # ---------------- 平均延迟排除错误记录 ----------------
    err_rec = plugin_module._CallRecord(
        ts=time.time(), group_id="g", mode="pool", latency_ms=15000.0,
        model="m", usage={}, messages=[{"text": "x", "sender": "s", "noul": None,
                                        "triggered": False}], error="timeout")
    p8.recent_calls.append(err_rec)
    ov2 = p8._overview()
    ok_lat = [r.latency_ms for r in p8.recent_calls if not r.error]
    check("overview: 平均延迟只统计成功调用",
          abs(ov2["totals"]["avg_latency_ms"] - round(sum(ok_lat) / len(ok_lat), 1)) < 0.01,
          f"{ov2['totals']['avg_latency_ms']}")

    # ---------------- 多行判定测试（与生产池判定一致） ----------------
    POOL_NOUL[:] = [0.1, 0.95, 0.2]
    POOL_CAT[:] = ["chitchat", "question", "not_addressed"]
    CALLS["n"] = 0
    msgs, usage = await p8._judge_test_text("这游戏好难\n鸭嘴兽在吗\n走了走了")
    check("多行测试: 走池判定一次调用", CALLS["n"] == 1)
    check("多行测试: 每行各出分数",
          [round(m["noul"], 2) for m in msgs] == [0.1, 0.95, 0.2])
    check("多行测试: 每行带分类", msgs[1]["category"] == "question")
    check("多行测试: usage 返回", usage == USAGE)
    SINGLE_NOUL["v"] = 0.42
    msgs1, _ = await p8._judge_test_text("单行消息")
    check("单行测试: 走 single 判定", len(msgs1) == 1 and msgs1[0]["noul"] == 0.42)

    # ---------------- stats.jsonl 轮转 ----------------
    import tempfile as _tf
    from pathlib import Path as _Path
    rot_file = _Path(_tf.mktemp(suffix=".jsonl"))
    rot_file.write_text("line\n" * 10, encoding="utf-8")
    p8._stats_file = rot_file
    p8._stats_lines = 10
    p8.config["stats_max_lines"] = 10
    p8._record(plugin_module._CallRecord(
        ts=time.time(), group_id="g", mode="pool", latency_ms=1.0, model="m",
        usage={}, messages=[], error=None))
    content_lines = rot_file.read_text(encoding="utf-8").strip().splitlines()
    check("轮转: 超限时保留最近一半+新记录", len(content_lines) == 6,
          f"{len(content_lines)} 行")
    check("轮转: 计数已重置", p8._stats_lines == 6)
    p8._stats_file = None

    for p in _PLUGINS:
        await p.terminate()
    await runner.cleanup()

    print(f"\n通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
