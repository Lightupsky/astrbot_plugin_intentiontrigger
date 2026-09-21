# laya 本地部署可行性与收益分析（N100 + 16GB）

> 结论先行：**资源上完全可行，但当前直接替换不值得** —— 实测 laya-multilingual
> 对中文群聊意图的零样本判定方向性错乱。建议先 API 上线攒数据，微调后再迁移。

## 背景

- [laya](https://huggingface.co/convaiinnovations/laya)（convaiinnovations，Apache 2.0）
  是与 Jev 同构的开源「typed decisions」模型：输入 `state` + `questions`
  （choice/score/noul），输出带概率的答案。非生成式、单次前向，专为路由/门控设计。
- 仓库根 checkpoint（英文，421M，ModernBERT-large）；`multilingual` 子目录
  （322M，mmBERT-base，支持 100+ 语言，~647MB）——**中文场景必须用它**。
- 官方延迟参考：T4 上 33-40ms/问；CPU 193-464ms。

## 实测（2026-09-21，桌面 x64 CPU + laya 0.3.4 + torch 2.14 CPU）

对本插件实际使用的 state/questions 格式（含 bot 别名、说话人、消息文本）做零样本测试：

| 群消息 | 期望 | 中文 noul 指令 | 英文 noul 指令 | choice 指令 |
|---|---|---|---|---|
| 小星，今天天气怎么样？ | 高 | **0.24 ❌** | 0.79 | the_humans (0.47) ❌ |
| 今天午饭吃什么 | 低 | **0.85 ❌** | 0.88 ❌ | the_humans (0.45) ✅ |
| bot 帮我查个航班 | 高 | 0.93 ✅ | 0.96 ✅ | the_bot (0.996) ✅ |
| 哈哈哈哈笑死我了 | 低 | **0.87 ❌** | 0.88 ❌ | the_humans (0.38) ✅ |
| 这个机器人还挺好玩 | 低/中 | 0.62 ❌ | 0.20 ✅ | **the_bot (0.997) ❌** |

- 单次推理 ~145-180ms（桌面 CPU），内存占用 ~1.5-2.5GB（含 torch 运行时）。
- **判定质量不可用**：三种指令写法下均出现方向性错误，且不同写法结果互相矛盾
  （说明模型并非"指令理解偏差"而是对该任务零样本能力不足）。
- 原因分析：laya 的 RLCD 训练分布以英文邮件分诊/审核类任务为主，中文群聊
  语境（称呼识别、反问、群友讨论机器人本身）超出其分布。

## N100 + 16GB 资源可行性（推断，基于实测外推）

| 维度 | 评估 |
|---|---|
| 内存 | multilingual F16 权重 ~650MB + torch CPU 运行时 ~1-2GB，合计 **<3GB**；连同 AstrBot、系统合计 <6GB，16GB 富余 ✅ |
| 延迟 | 本机桌面 CPU ~150ms；N100（4× Gracemont E-core）单核约为桌面 40-55%，估计 **0.3-1s/次**。门控是异步非实时路径，主 LLM 本身要数秒，可接受 ✅ |
| 吞吐 | ~1-3 QPS。单群消息频率远低于此；多活跃群高峰可能排队（插件 `max_concurrency` 设 1-2，排队消化）✅ |
| 功耗 | N100 TDP 6W，空闲几乎为零，常驻无压力 ✅ |
| 磁盘 | 模型 ~650MB + 依赖 ~2GB ✅ |

## 若要本地部署：兼容层方案

laya 与 Jev 的请求/响应几乎同构，写一个薄 HTTP 服务暴露 `/v1/systemone`，
插件零改动（`api_base_url` 指向它即可）：

```python
# laya_compat_server.py — 启动: uvicorn laya_compat_server:app --host 0.0.0.0 --port 8900
import os
os.environ["USE_TF"] = "0"  # 规避 TF/abseil 加载死锁
import laya
from fastapi import FastAPI, Request

app = FastAPI()
agent = laya.load("convaiinnovations/laya", subfolder="multilingual")

@app.post("/v1/systemone")
async def systemone(req: Request):
    body = await req.json()
    res = agent.predict(body["state"], body["questions"])
    return {"model": res.get("model", "laya-local"), "answers": res["answers"],
            "usage": res.get("usage", {})}
```

注意：CPU 推理是阻塞的，高并发需加 `run_in_executor` 或线程池；laya 支持多问
合批（T4 上 50 问 337ms），可进一步降低均摊延迟。

## 建议

1. **现阶段用 TypeSafe API**：Jev 是该范式的旗舰模型，中文群聊意图理解
   预期显著更好（本插件 `log_decisions` 可留观）。注意按量计费，活跃群成本
   会累积 —— 这正是未来迁移动机。
2. **上线即开始攒数据**：开启 `log_decisions`，把真实群消息 + 判定 + （人工
   抽查的）对错标签存档，作为微调语料。
3. **攒够几百~几千条后微调 laya**：ModernBERT 架构微调成本低（N100 也可跑，
   CPU 上数小时级），届时本地部署的质量-成本-隐私三收益同时兑现；用
   `/intention test` 在同一批消息上对比 Jev 与本地 laya，达标（如准确率
   ≥ Jev-3% 以内）再切换 `api_base_url`。
4. 若只是想要"零成本"，另一个务实选项是保留 Jev API 但把 `threshold` 调高 +
   `group_whitelist` 收窄，减少调用量。

## 一句话总结

N100+16GB 跑 laya 毫无压力（内存 <3GB、延迟亚秒级、功耗 6W），**工程上可行**；
但零样本中文判定质量实测不合格，**现阶段不值得**直接替换 —— 先 API 攒数据、
微调后再切，才是收益最大路径。
