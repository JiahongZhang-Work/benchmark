# Agent Hint 数据集会话特征与压测设计指南（精简版）

本文面向**自动化 agent 和压测执行者**，回答：

1. `base_evict_60k.jsonl` 这类 agent 轨迹数据，天然适合测什么、不适合测什么？
2. 怎样构造数据、并发和部署，才能让 agent_hint 的收益点真正显现？
3. 为什么 **session 亲和** 是测试正确性的前提？

关联文档：[Agent Hint 性能压测 SOP](./agent_hint_perf_sop.md)、[Agent Hint 性能压测 Runbook](./agent_hint_perf_runbook.md)。

**目标部署：`DP1 / TP8 / EP`。** 如果未来切回 DP>1，必须先解决 session 亲和，否则 A/B 结论不成立。

---

## 0. TL;DR：三条铁律

1. **Session 亲和是测试正确性的前提。** DP>1 且无亲和时，同一会话的 data-plane 和 manage 请求会落到不同 EngineCore，A/B 结果不可信。
2. **测 prefix 复用必须用完整 session，不要只用最后 N 轮。** 最后 N 轮会制造很大的 cold prefill，复用率被严重低估。
3. **`Failed=0` 不等于 agent_hint 生效。** 必须单独统计 `agent_hint_response.status` 或服务端 edit 成功率。

---

## 1. 实验分组：只做 A / B 两组

| 组 | 服务端 agent_hint | 客户端 `pass_agent_hint` | 目的 |
|---|---|---:|---|
| **A** | 关闭 | `0` | 基线 |
| **B** | 开启 | `1` | 特性组 |

- A/B 只能看**端到端收益**（特性 + 字段 + 服务端调度）。
- 本文不含第三组，因此**不能拆分字段传输开销**。
- **禁止**将 `pass_agent_hint=1` 打到 hint 关闭的服务端：manage 请求会退化成真实推理请求，结果没有参考意义。

---

## 2. `base_evict_60k.jsonl` 会话特征

### 2.1 数据结构

| 指标 | 值 |
|---|---:|
| 原始轨迹文件 | 921 |
| 转换后 data-plane | 890 |
| 转换后 manage | 100 |
| parent / session | 50 / 50，全部 **1:1** |
| 每 session 轮次 | 8 ~ 39，median 15 |
| 严格前缀累积会话 | **50 / 50** |

生成方式见 SOP；关键参数：`--evict --add-pause 0.5 --max-token-len 60000`。  
`--max-token-len 60000` 用于避免 `prompt + max_tokens` 超过服务端 `max_model_len`。

### 2.2 Prefix 复用潜力

| 指标 | 值 |
|---|---:|
| 总 prompt tokens | 16.1M |
| 可复用前缀 tokens | 14.6M（**90.69%**） |
| 真正新增 tokens | 1.5M |
| 理论 prefill 缩减 | **10.75×** |
| 单轮复用率 median / P90 | 89.47% / 98.64% |
| 复用率 ≥80% 的轮次 | 577 / 840 |

每个 session 的 prompt 严格累积：

```text
turn 1: system + user1
turn 2: turn 1 全部内容 + user2
turn 3: turn 2 全部内容 + user3
...
```

如果 prefix cache / KV 复用完全生效，理论 prefill 工作量只有原来的约 1/10.75。

### 2.3 上下文与 manage 分布

| 指标 | 值 |
|---|---:|
| 首轮 prompt tokens median | 44 |
| 最终上下文 tokens median / P90 / max | 27.7k / 39k / 59.3k |
| 原始输出 max_tokens median / P90 / max | 356 / 5.4k / 40k |
| evict / offload / prefetch | 50 / 25 / 25 |
| 只有 1 条 evict 的 session | 25 |
| 有 offload+prefetch+evict 的 session | 25 |

注意：

- 每个 parent 只有 **1 个 session**，`--evict` 不会产生真实的“中断小组 / 未中断小组”组合，只会产生**末尾 evict**。
- offload/prefetch 全部来自 `--add-pause 0.5` 的**合成插入**，每个 session 最多 1 对。
- `stage=end` 为 0 是正常现象，不代表数据构造失败。

### 2.4 特征总结

| 维度 | 判断 | 原因 |
|---|---|---|
| Prefix KV 复用 | **强** | 90.7% 理论复用，10.75× prefill 缩减 |
| 多轮上下文增长 | **强** | 首轮 44 tokens → 最终 median 27.7k |
| Offload/prefetch 机会 | **弱** | 只有 25 个合成暂停点，约 2.8% turns |
| 真实中断恢复 | **无** | 1 session/parent，只有末尾 evict |
| 跨 session 前缀共享 | **很弱** | 首轮 median 仅 44 tokens |
| 并发/显存压力 | **取决于配置** | c8+DP8 时每 rank 约 1 个活跃 session |

---

## 3. 收益点评估

- **Prefix 复用：收益点足够大。** 90.7% 的 prompt token 是已计算前缀，适合测 TTFT、prefill throughput、prefix cache hit、KV 复用率。
- **Offload/prefetch/evict：当前数据收益点偏小。** 只有 25 个合成暂停点，覆盖不到 3% 的 turn；没有真实中断；末尾 evict 对本次会话的 prefill 没有帮助。
- **并发/显存压力：需要主动制造。** c8 + DP8 时每个 rank 约 1 个活跃 session，单 session median 27.7k，HBM 通常放得下；offload/evict 的价值来自 HBM 不足，没有压力时收益自然不明显。
- **不要用 last-N 测 prefix。** 只保留最后 N 轮会把首轮变成 10k~53k 的 cold prefill，理论复用率从 90.7% 降到约 73.7%，prefill 缩减从 10.75× 降到约 3.81×。

> **测 prefix 复用必须从 `start` 开始保留完整 session。** 如果必须缩量，优先减少 session 数，而不是砍掉每个 session 的前几轮。

---

## 4. Session 亲和：最关键的正确性问题

### 4.1 定义与原理

同一 `parent_session_id` / `session_id` 的**所有请求**（data-plane + manage）必须落到同一个 EngineCore / SAM 实例。

- SAM 的 `_session_hashes` 是**每个 EngineCore 私有**的。
- manage 请求执行 edit 时，只从**当前 EngineCore 的 SAM** 取该 session 的 block hash。
- 取不到时直接失败：

```text
Could not find session ... with block ref record in SAM, failed to perform context management edit
session_id ... has 0 hash/block in SAM, fail to perform edit ...
```

### 4.2 没有亲和会发生什么

- 同一 session 的 data-plane 请求在多个 DP rank 之间跳。
- manage 请求落到一个“没见过这个 session”的 rank。
- 结果是：
  - offload/prefetch/evict 大量失败；
  - 即使某次 offload 成功，下一轮换 rank 后也复用不到；
  - local prefix cache 命中率显著低于理论值；
  - A/B 对照看起来“没有收益”，但其实是测试无效。

### 4.3 DP1 / TP8 / EP 下的情况

| 部署 | Session 亲和 | 说明 |
|---|---|---|
| **DP1 / TP8 / EP** | **天然满足** | 只有一个 EngineCore / SAM，不存在 rank 间状态分裂 |
| DP>1 | 需要显式保证 | 必须做一致性哈希 / sticky routing，否则 A/B 结果不可信 |

DP1/TP8/EP 下仍需确认：

- `api_server_count=1`
- 没有外部负载均衡把请求分发到多个 EngineCore
- 没有多端口 LB / hybrid LB 配置

### 4.4 亲和验收 Gate

正式压测前必须通过：

- [ ] 服务端为 DP1，或前端明确按 `parent_session_id` / `session_id` 做粘滞路由
- [ ] 日志中同一 session **始终出现在同一个 EngineCore**
- [ ] manage edit 成功率接近 100%
- [ ] 没有持续出现 `0 hash/block in SAM`
- [ ] local prefix cache hit 接近数据理论复用率

> **如果无法保证 session 亲和，不要用 A/B 结果判断 agent_hint 收益。**

---

## 5. DP1 / TP8 / EP 实验设计

### 5.1 推荐服务端配置

关键参数：`--data-parallel-size 1 --tensor-parallel-size 8 --enable-expert-parallel --max-model-len $MAX_MODEL_LEN --enable-prefix-caching`，以及 `AscendStoreConnector` 的 `kv-transfer-config`（与 SOP 一致）。

关键检查：

- [ ] `data-parallel-size=1`
- [ ] `api_server_count=1`
- [ ] 没有外部 LB 把请求分到多个 EngineCore
- [ ] `VLLM_ASCEND_ENABLE_AGENT_HINT` 与 A/B 组一致
- [ ] `/metrics` 可访问

### 5.2 换模型注意事项

换模型后必须重新校准：

- `tokenizer.json`：token 数、前缀长度、90.7% 复用率都会变化
- chat template：可能改变实际发给服务端的 prompt 前缀
- `max_model_len`：必须能容纳 `prompt + max_tokens`
- 模型名 / served name：不要沿用旧配置

必须重跑：数据集 token 统计、A 基线、单 session 冒烟、亲和验收。

**禁止跨模型直接比较绝对值。**

---

## 6. 更有收益点的数据与实验矩阵

### 6.1 数据改造

| 目标 | 做法 |
|---|---|
| 保留 prefix 复用收益 | 用完整 session，从 `start` 开始 |
| 增加真实中断 | 每个 parent 下放 2~4 个 session，交替出现 |
| 增加 offload/prefetch | 把 pause 覆盖提高到 20%~30% 的 turn |
| 保留长上下文 | 不要用 last-N 砍掉前几轮 |
| 制造 HBM 压力 | 提高并发，或减少 KV cache 预算 |

推荐数据变体：

- `full`：完整 session，用于测 prefix 复用
- `high-pause`：高 offload/prefetch 覆盖，用于测上下文管理
- `multi-session-parent`：每个 parent 多个 session，用于触发真实中断组合

### 6.2 实验矩阵

| 组 | 并发 | 重复 | 说明 |
|---|---|---|---|
| A | c8 / c16 / c32 | 3 | hint 关闭 |
| B | c8 / c16 / c32 | 3 | hint 开启 |

要求：

- 每次 run 使用独立 nonce 做缓存隔离
- warmup 数据与测量数据不重叠
- 同一服务实例上交替跑 A/B，减少状态漂移
- 每组保留 min / median / max

### 6.3 每组验收

`Failed=0`、`Total Requests` 等于 data-plane 数、`Concurrency` 接近配置值、details 行数匹配、manage edit 成功率接近 100%、local prefix cache hit 与理论值同量级。

---

## 7. 性能对照实验经验与坑

### 7.1 A/B 能说明什么

- **A vs B**：端到端收益（特性 + 字段 + 调度）。
- 不能拆分“字段传输开销”；需要第三组才能拆分，但本文不做。

### 7.2 必须控制的变量

- 服务端实例：A/B 尽量在同一实例上交替跑。
- 运行顺序：顺序会影响温度和缓存状态，至少做 A→B→A→B。
- 缓存：每次 run 用唯一 nonce。
- 模型 / tokenizer / max_model_len：换模型后重跑基线。

### 7.3 `Failed=0` 不代表 edit 成功

- manage 请求可能返回 HTTP 200，但 `agent_hint_response.status=False`。
- 压测的 `Failed Requests` 不会统计这种失败。
- 必须单独检查：

```bash
grep -E "Context management result|0 hash/block in SAM|Could not find session" server.log
```

### 7.4 keep-alive 的 `client_spm` 空指针

现象：

```text
Keep-alive thread error: 'NoneType' object has no attribute 'lookup'
```

原因：`client_spm` 是懒初始化的；某些 rank 只处理过短请求，`client_spm` 仍为 `None`；keep-alive 线程没有空指针保护。

影响：不会导致服务崩溃（异常被捕获），但远端 KV 的 LRU/TTL 不会被刷新，长跑时可能提前淘汰，错误日志会持续刷屏。

最小修复：在 `look_up_keys()` / keep-alive 线程中检查 `scheduler.client_spm is None` 并直接返回；更好的做法是在 `VLLM_ASCEND_ENABLE_AGENT_HINT=1` 时提前初始化 `client_spm`。

### 7.5 其他注意事项

- `mmengine` 的 lazy import 不支持 `os.environ.get()` 调用，配置里用 `_env()` + `__import__("os")`。
- 不要将 `pass_agent_hint=1` 打到 hint 关闭的服务端。
- 不要用 last-N 子集测 prefix 复用。
- 不要跨模型、跨服务实例直接比较绝对值。
- 不要只看 `Failed=0`，要看 edit 成功率和 prefix cache hit。
- 不要用 `--num-prompts` 做正式测量，会截断会话。

---

## 8. Agent 执行 SOP

### 8.1 执行清单

- [ ] 仓库文件齐全；`ais_bench` 加载本仓库代码；`/v1/models`、`/metrics` 可用
- [ ] `data-parallel-size=1`、`api_server_count=1`；`$MODEL` / `$TOKENIZER` / `$MAX_MODEL_LEN` 已确认
- [ ] data-plane / manage 计数符合预期；`parent_session_id` 无空值；每 session 有 `stage=start`
- [ ] `--evict` 时明确 `stage=end` 是否为 0；data-plane 的 `max_tokens` 非 0
- [ ] 单 session 日志中只出现一个 EngineCore；无 `0 hash/block in SAM`；edit 成功率接近 100%
- [ ] `Failed=0`；出现 session START/END；manage 返回 `agent_hint_response.status=true`
- [ ] warmup 与测量数据不重叠；每次 run 唯一 nonce；A/B 交替；metrics before/after
- [ ] 记录 run 配置、数据 hash、服务端参数

### 8.2 失败排查

| 现象 | 优先排查 |
|---|---|
| `0 hash/block in SAM` | session 亲和 / DP rank 是否一致 |
| `Keep-alive thread error` | `client_spm` 是否初始化 |
| 并发上不去 | `session_concurrency`、数据 parent 数、manage 请求 |
| B 无收益 | 亲和、HBM 压力、offload 覆盖、子集策略 |
| 生成长度异常短 | `max_tokens` 是否回退为 1 |
| 同一份数据第二次明显变快 | 缓存未隔离 / nonce 重复 |
| 换模型后结果不可比 | tokenizer / template / max_model_len 已变化 |

---

## 9. 附录

### 9.1 关键日志字符串

```text
[session-aware] enabled, session_concurrency=...
[session-aware] session ... START request stage=...
[session-aware] session ... END   request stage=...
[agent_hint][send] data_id=... session_id=... agent_hint={...}
SAM: on_blocks_allocated_for_request ...
Context management result: ... status True/False ...
Could not find session ... with block ref record in SAM
session_id ... has 0 hash/block in SAM
Keep-alive thread error: 'NoneType' object has no attribute 'lookup'
```

### 9.2 关键代码位置

| 文件 | 作用 |
|---|---|
| `ais_bench/datasets/agent/build_agent_dataset.py` | 数据集构造 |
| `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py` | dataset 配置 |
| `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py` | model 配置 |
| `vllm_ascend/core/agent_hint/session_aware_manager.py` | SAM / edit 执行 |
| `vllm_ascend/core/agent_hint/session_aware_pooling_manager.py` | SPM / keep-alive |
| `vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/pool_scheduler.py` | client_spm 初始化 |

### 9.3 运行记录

运行记录模板见 SOP 第 12 节。至少记录：模型、tokenizer、DP/TP/EP、A/B 组、并发、数据 hash、data-plane/manage 数、缓存状态、edit 成功率、local prefix hit。