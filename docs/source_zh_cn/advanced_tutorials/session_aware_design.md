# Session 级并发控制：整体设计与流程

> 面向 vLLM 流式生成场景，把并发控制从「请求级」改为「会话（session）级」。
> 本文档是**整体设计与流程**，侧重「为什么这么设计、数据怎么流、谁在哪个进程跑」；
> 具体的配置/运行/校验细节见配套文档 `session_aware_concurrency.md`。

## 一、要解决的问题

多轮对话中，同一 session 的请求之间存在**时序依赖**：后一条请求通常依赖前一条的响应。
普通请求级并发无法表达这种顺序约束，容易把同一 session 的请求打乱。

Session 级并发控制的两条核心语义：

- **同一 session 内请求严格按序发送**，任一时刻**最多 1 条在途**（at-most-1 in-flight）。
- **跨 session 可以并行**，但活跃 session 数受 `session_concurrency` 限制。

## 二、关键不变量与保证机制

| # | 需求 | 保证机制 |
|---|---|---|
| 1 | 同 session 请求按序 | 数据集预排序 + janus FIFO + per-session `asyncio.Queue` FIFO |
| 2 | 同 session at-most-1 in-flight | `session_worker` 内 `await do_request` 串行 |
| 3 | 跨 session 最多 N 个活跃 | `async with session_sem`（N=`session_concurrency`） |
| 4 | 数据加载无预取上限 | 复用请求粒度路径：整集转储 + dispatcher 全量读取 |
| 5 | session 消费到 `end` 即释放槽位 | worker 处理到 `stage=end` 立即 `break` |
| 6 | 无 `end` 的 session 兜底收尾 | 全量读完后 dispatcher 发 `None` 哨兵 |
| 7 | 可选全局 RPS 限速 | `token_bucket.acquire` 在 `do_request` 前 |
| 8 | 同 session 不跨进程 | `_get_workers_num` 在 session 模式 `return 1` |
| 9 | 控制字段不污染 generate 参数 | `do_request` 用 `pop` 取出 `session_id`/`stage` |
| 10 | `agent_hint` 仅在 `pass_agent_hint=True` 时进 body | `do_request` 按开关决定是否 `pop("agent_hint")` |

## 三、数据格式（session 数据集约定）

`.jsonl` 每行一个 JSON，`session_id`/`stage` 可放顶层列，也可放在**嵌套 `agent_hint`** 里
（推荐 agent_hint 结构，由 `get_data_list` 自动提取）：

```json
{
  "question": "帮我推荐一本机器学习的入门书",
  "answer": "推荐周志华的《机器学习》（西瓜书）",
  "agent_hint": {
    "session_id": "sess_001",
    "parent_session_id": "",
    "cache_control": {},
    "context_management": {},
    "stage": "start"
  }
}
```

**预处理要求**：数据需按 `(session_id, session 内顺序)` 排序，保证同 session 请求相邻且有序。
代码不重排，只透传。

### stage 标记约定

- `stage=start`：session 第一条请求
- `stage=stage-N`：中间请求
- `stage=end`：最后一条请求；worker 处理完后立即结束

### 提取优先级

- `session_id`：顶层 `session_id` 列 > `agent_hint.session_id` > 空
- `stage`：顶层 `stage` 列 > `agent_hint.stage` > 空

## 四、数据流总览

```
数据集（.jsonl，含 session_id/stage 标记，按 session 排序）
  │
  ▼ ① GenInferencer.get_data_list
  │     - 为每条样本注入 session_id / stage / 透传额外列（agent_hint 等）
  │     - 自动从 agent_hint.session_id / agent_hint.stage 提取
  │     - 按 session 校验 start→…→end 标记（不合规仅 warning，不致命）
  │     - 输出 data_list
  │
  ▼ ② SharedMemory                整集 pickle 转储（与请求粒度相同）
  │
  ▼ ③ janus.Queue                 同步线程 → 异步协程桥接（容量 = batch_size+1）
  │
  ▼ ④ Dispatcher 协程
  │     - 持续读 janus 直到读完（复用请求粒度加载，无预取上限）
  │     - 按 session_id 路由到各自的 per-session asyncio.Queue
  │     - 首次见到某 session 时 admit_new_session（拉起其 worker）
  │
  ▼ ⑤ per-session asyncio.Queue   每个 session 一个 FIFO 队列
  │
  ▼ ⑥ session_worker(sid)
         async with session_sem          # 限并发 session 数
         while True:
            data = await q.get()         # 同 session 严格 FIFO
            if data is None: break       # 全量读完后兜底哨兵
            if token_bucket: await acquire   # 可选全局限速
            await do_request(...)        # 串行 → at-most-1 in-flight
            if stage == "end": break     # 立即释放槽位
```

## 五、生命周期（worker 语义）

1. **admit**：dispatcher 首次读到某 session 的 `start` 请求时，为其创建 asyncio.Queue 并拉起 `session_worker`。
2. **acquire**：worker 申请 `session_sem` 并发槽位（受 `session_concurrency` 限制）。
3. **run**：同 session 内请求严格串行，梳理 stage。
4. **release**：消费到 `stage=end` 请求且运行完 → 立即 `break`，退出 `async with session_sem` 释放槽位，
   **不等队列读空、不等全局哨兵**。
5. **兜底**：无 `end` 标记的 session，在整集读完后收到 dispatcher 发的 `None` 哨兵，`break` 退出。

## 六、进程模型

```
CLI main（主进程，cli.main → TaskManager → WorkFlowExecutor）
  └─ LocalRunner
       │   TASKS.build → OpenICLApiInferTask（实例 A：仅用于拿名字/命令）
       │   task.cfg.dump(param_file)   ← 序列化配置给子进程
       └─ 子进程（python openicl_api_infer.py <param_file>）
            └─ OpenICLApiInferTask（实例 B：真正跑推理，重读 param_file）
                 └─ run()
                      ├─ 非 debug：_run_multi_process → mp.Process 子进程
                      │     └─ run_single_inferencer → inference_with_shm
                      └─ debug：_run_debug → 同进程直接 inference_with_shm
                            └─ 都调用 _session_aware_worker_loop（session 模式）
```

**为什么单进程**：多进程会把不同请求分到不同 worker，破坏 session 内顺序和 at-most-1 语义。
`_get_workers_num` 在 session 模式下强制 `return 1`。

**注意**：真正跑推理的是实例 B（从 dump 出的 param_file 重读配置），因此**传给子进程的配置必须保留**
`session_aware` / `session_concurrency` / `pass_agent_hint` 三个键。

## 七、文件清单

### 代码改动

| 文件 | 职责 |
|---|---|
| `openicl/icl_dataset_reader.py` | 新增 `get_session_id()`，对称于 `get_timestamp` |
| `models/output.py` | `Output.__init__` 新增 `self.session_id` 字段 |
| `openicl/icl_inferencer/icl_gen_inferencer.py` | `get_data_list` 注入 `session_id`/`stage`、透传额外列、校验 start/end；`do_request` 取出控制字段、按 `pass_agent_hint` 决策 |
| `openicl/icl_inferencer/icl_base_api_inferencer.py` | 新增 `_session_aware_worker_loop`；接收 3 个 session 参数；`inference_with_shm` 分发 |
| `models/api_models/vllm_custom_api.py` | `get_request_body` 合并 `**args`（透传 `agent_hint` 进 body） |
| `models/api_models/vllm_custom_api_chat.py` | 同上（chat 版合并 `args`） |
| `tasks/openicl_api_infer.py` | 读取 session 配置、透传 3 参数、session 模式强制单进程 |

### 新增配置 + 数据

| 文件 | 用途 |
|---|---|
| `datasets/session_demo/session_demo_agent.jsonl` | agent_hint 嵌套结构样例数据（20 session） |
| `configs/datasets/custom/session_demo_agent_gen.py` | dataset config（自动从 agent_hint 提取） |
| `configs/models/vllm_api/vllm_api_general_stream_session.py` | model config（含 session 3 参数） |
| `configs/api_examples/infer_vllm_api_stream_session.py` | 总装配 config |

## 八、关键配置参数

| 参数 | 含义 | Session 模式下的作用 |
|---|---|---|
| `session_aware` | 是否启用 session 并发控制 | `True` 走新 worker loop |
| `session_concurrency` | 同时活跃 session 数上限 | 控制跨 session 在途总量 |
| `pass_agent_hint` | 是否透传 `agent_hint` 到 API body | `True` 合并进 vLLM body；`False` 仅内部用 |
| `batch_size` | 退化为 janus 队列容量 | 内存缓冲 |
| `request_rate` | 全局 RPS | 可选叠加限速，`0` 关闭 |

`use_chat_api=True` 时，请求发送到 **`v1/chat/completions`**（而非默认的 `v1/completions`），
请求体用 `messages` 格式、响应解析走 chat 路径。

`session_aware` / `session_concurrency` / `pass_agent_hint` 是 **inferencer-only 开关，不是 model 参数**，
不会传给 `VLLMCustomAPI` 构造器（`VLLMCustomAPI.__init__` 不接收这三个参数）。

## 九、排查与修复记录

### 9.1 `session_aware` 在推理子进程里读成 False（已修）

**现象**：model config 里 `session_aware=True`，但真正跑推理的子进程里却是 `False`，导致
`[session-aware]` 调度、`[agent_hint]` 日志都打不出来，agent_hint 也没透传。

**根因**：`OpenICLApiInferTask.__init__` 把这三个键从 `self.model_cfg`（即 `cfg['models'][0]`，
`self.cfg` 的别名）里 `pop` 掉了 → 被 `task.cfg.dump(param_file)` 序列化的配置已丢失这三个键 →
子进程 `Config.fromfile(param_file)` 重读时拿不到 → `.get("session_aware", False)` 返回 `False`。

**修复**（`openicl_api_infer.py`）：
```python
# 只对用于 model/inferencer 构造的本地副本 pop，不动会 dump 出去的 self.cfg
self.model_cfg = copy.deepcopy(self.model_cfg)
for _key in ("session_aware", "session_concurrency", "pass_agent_hint"):
    self.model_cfg.pop(_key, None)
```
另补 `import copy`。

**效果**：dump 出去的配置保留三键，子进程能读到 `session_aware=True` / `pass_agent_hint=True`；
模型构造用的本地副本仍去掉三键，避免 `VLLMCustomAPI(**model_cfg)` 报 unexpected keyword。

### 9.2 worker 循环收尾崩溃（已修）

**现象**：dispatcher 读完全量数据后崩溃：
```
await session_queues[sid].put_nowait(None)
TypeError: object NoneType can't be used in 'await' expression
```
随后错误处理器又因 `AISLogger.error()` 需要两个位置参数而报
`AISLogger.error() missing 1 required positional argument: 'msg'`，掩盖了真实错误。

**修复**（`icl_base_api_inferencer.py`）：
1. `put_nowait` 是同步方法，去掉 `await`：
   ```python
   session_queues[sid].put_nowait(None)
   ```
2. 错误分支 `self.logger.error(f"...")` → `self.logger.warning(f"...")`（`error` 需要 `(error_code, msg)`）。

## 十、运行与验证

```bash
# 前置：起好 vLLM（OpenAI 兼容接口，监听 8080），或用 tools/infer_serve_simulator 模拟

# 方式一：跑总装配 config（agent_hint 版 dataset）
cd D:/agent/agent_hint/aisbench/benchmark
python -m ais_bench.benchmark.cli.main \
  --mode perf --config ais_bench/benchmark/configs/api_examples/infer_vllm_api_stream_session.py --debug

# 方式二：命令行指定 model + dataset
python -m ais_bench.benchmark.cli.main \
  --mode perf -m vllm_api_general_stream_session -d session_demo_agent_gen \
  --num-warmups 0 --work-dir outputs/vllm-stream-session-agent
```

典型调度日志：
```
[session-aware] admit NEW session sess_001 (data_id=0)
[session-aware] session sess_001 acquired exec slot
[session-aware] session sess_001 START request stage=start data_id=0
[agent_hint][generate]  data_id=0 session_id=sess_001 agent_hint_in_data=True ...
[agent_hint][send]      data_id=0 session_id=sess_001 agent_hint={...}
[session-aware] session sess_001 END   request stage=end   data_id=8
[session-aware] session sess_001 reached stage=end, ending
[session-aware] session sess_001 releasing slot
```

独立调度仿真器（验证不变量）：
```bash
python tools/session_aware_simulator.py
```
```
