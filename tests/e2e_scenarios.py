"""复杂群聊场景端到端测试：对比 single（无上下文）与 pool（带上下文）判定质量。

使用真实 TypeSafe API。运行方式:
    TYPESAFE_API_KEY=... python tests/e2e_scenarios.py
（在 AstrBot venv 中运行；API key 只从环境变量读取，不会入库）
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = ROOT.parent / "AstrBot"
# 开发机上加本地 AstrBot 源码路径；生产环境直接用已安装的 astrbot 包
if ASTRBOT_ROOT.exists():
    sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(ROOT))

from astrbot.core.config.astrbot_config import AstrBotConfig

import main as plugin_module

# bot 昵称模拟"鸭嘴兽"场景
BOT_NAMES = ["鸭嘴兽"]

# (sender, text, expected_triggered)
# expected: True=应对 bot 互动放行, False=闲聊应抑制
SCENARIOS = [
    (
        "场景A · 碎片追问（第2-4条是对 bot 的连击）",
        [
            ("阿强", "这游戏也太难了吧", False),
            ("小明", "鸭嘴兽在吗", True),
            ("小明", "在吗在吗", True),        # 单条无实义，靠上下文
            ("小明", "快理我一下", True),      # 单条无实义，靠上下文
            ("阿强", "算了不玩了", False),
        ],
    ),
    (
        "场景B · 交叉话题（第3条对 bot，第4条跟风）",
        [
            ("老王", "今晚吃火锅还是烧烤", False),
            ("阿珍", "火锅吧，上次那家不错", False),
            ("小明", "鸭嘴兽，帮我查下明天北京天气", True),
            ("阿珍", "我也要查", True),        # 上下文: 跟着让 bot 查
            ("老王", "别查了明天不出门", False),
        ],
    ),
    (
        "场景C · 提及但不互动（讨论鸭嘴兽这种动物）",
        [
            ("阿强", "你们知道吗，鸭嘴兽是哺乳动物还会下蛋", False),
            ("老王", "真的假的", False),
            ("阿珍", "真的，鸭嘴兽可神奇了", False),
            ("小明", "涨知识了", False),
            ("老王", "下次去动物园看看", False),
        ],
    ),
    (
        "场景D · 纯语气词/无实义",
        [
            ("阿珍", "哈哈哈哈哈哈", False),
            ("阿强", "笑死", False),
            ("小明", "？？", False),
            ("老王", "啊这", False),
            ("阿珍", "6", False),
        ],
    ),
    (
        "场景E · 隐式命令（无称呼，第2条对 bot）",
        [
            ("老王", "刚看到一个特别离谱的新闻", False),
            ("小明", "翻译一下这段英文呗 Hello world how are you", True),
            ("阿珍", "啥新闻发出来看看", False),
            ("老王", "就是那个AI取代程序员的", False),
            ("阿强", "哈哈哈哈不可能", False),
        ],
    ),
    (
        "场景F · 对 bot 连续对话 + 旁人插话",
        [
            ("小明", "鸭嘴兽讲个笑话", True),
            ("小明", "不好笑，换一个", True),   # 靠上下文
            ("小明", "还是不好笑", True),       # 靠上下文
            ("小明", "行了行了就这样吧", True),  # 靠上下文（对 bot 收尾）
            ("阿强", "你们聊啥呢这么热闹", False),
        ],
    ),
]


class FakeContext:
    def get_config(self):
        return {"platform_settings": {"rate_limit": {"count": 0, "time": 60}}}

    def register_web_api(self, *args, **kwargs):
        pass


def make_plugin():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    cfg = AstrBotConfig(config_path=tempfile.mktemp(suffix=".json"), schema=schema)
    cfg.update(
        {
            "api_key": os.environ.get("TYPESAFE_API_KEY", ""),
            "bot_names": BOT_NAMES,
            "threshold": 0.5,
            "mode": "pool",
            "pool_size": 5,
            "flush_interval": 5,
        }
    )
    return plugin_module.IntentionTrigger(FakeContext(), cfg)


def evaluate(rows, threshold=0.5):
    """rows: [(sender, text, expected, score)] → (acc, fp, fn, detail)"""
    tp = fp = tn = fn = 0
    detail = []
    for sender, text, expected, score in rows:
        actual = score is not None and score >= threshold
        if expected and actual:
            tp += 1
        elif expected and not actual:
            fn += 1
        elif not expected and actual:
            fp += 1
        else:
            tn += 1
        detail.append((sender, text, expected, score, actual))
    total = len(rows)
    acc = (tp + tn) / total if total else 0
    return acc, tp, tn, fp, fn, detail


async def main() -> int:
    if not os.environ.get("TYPESAFE_API_KEY"):
        print("请先设置 TYPESAFE_API_KEY 环境变量")
        return 2

    plugin = make_plugin()
    await plugin.initialize()

    all_single_rows, all_pool_rows = [], []

    for title, msgs in SCENARIOS:
        print(f"\n===== {title} =====")

        # --- single 模式：逐条独立判定（无上下文） ---
        single_rows = []
        for sender, text, expected in msgs:
            state = {
                "bot": plugin._bot_desc("10000"),
                "chat": {"type": "group", "speaker_name": sender, "speaker_id": "0"},
                "message": text,
            }
            try:
                score = await plugin._judge_single(state)
            except Exception as e:
                print(f"  single 调用失败: {e}")
                score = None
            single_rows.append((sender, text, expected, score))
            await asyncio.sleep(1.5)  # 温和对待 rate limit

        # --- pool 模式：整池一次判定（带完整上下文） ---
        state = {
            "bot": plugin._bot_desc("10000"),
            "chat": {"type": "group", "group_id": "test"},
            "messages": [
                {"index": i, "sender_name": s, "sender_id": "0", "text": t}
                for i, (s, t, _e) in enumerate(msgs)
            ],
        }
        try:
            scores = await plugin._judge_pool(state, len(msgs))
        except Exception as e:
            print(f"  pool 调用失败: {e}")
            scores = [None] * len(msgs)
        pool_rows = [
            (s, t, e, sc) for (s, t, e), sc in zip(msgs, scores)
        ]

        acc_s, tp_s, tn_s, fp_s, fn_s, det_s = evaluate(single_rows)
        acc_p, tp_p, tn_p, fp_p, fn_p, det_p = evaluate(pool_rows)

        print(f"  {'消息':<28}{'期望':<4}{'single':>8}{'pool':>8}")
        for (s, t, e, sc, _a), (_s2, _t2, _e2, psc, _pa) in zip(det_s, det_p):
            mark = lambda v, exp: ("" if v is None else f"{v:.2f}") + (
                "" if (v is None) or ((v >= 0.5) == exp) else "✗"
            )
            print(
                f"  {s}: {t:<22}{'✅' if e else '⛔':<4}"
                f"{mark(sc, e):>8}{mark(psc, e):>8}"
            )
        print(
            f"  single 准确率 {acc_s:.0%} (误放 {fp_s} / 漏放 {fn_s})   "
            f"pool 准确率 {acc_p:.0%} (误放 {fp_p} / 漏放 {fn_p})"
        )
        all_single_rows.extend(single_rows)
        all_pool_rows.extend(pool_rows)
        await asyncio.sleep(2)

    acc_s, *_ = evaluate(all_single_rows)
    acc_p, *_ = evaluate(all_pool_rows)
    print(f"\n========== 总体: single {acc_s:.1%}  vs  pool(上下文) {acc_p:.1%} ==========")

    await plugin.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
