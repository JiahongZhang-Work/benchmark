# Session 级并发控制（Session-Aware Concurrency）

针对 vLLM 流式生成场景，把并发控制从"请求级"改为"会话（session）级"：

- 同一 session 内的请求**严格按序发送**，任一时刻**最多 1 条在途**（at-most-1 in-flight）
- 跨 session 并发受 `session_concurrency` 限制（同时活跃的 session 数上限）
- **session 生命周期由 `stage` 标记决定**：session 第一条请求标 `start`，中间为 `stage-N`，
  最后一条为 `end`。session **消费到 `end` 请求且运行完即结束**，立刻把并发槽位让给新 session，
  **不需要把整个 session 的队列读空 / 不需要等全局哨兵**
- **数据加载完全复用请求粒度路径**：整集转储 + producer/janus 全量读取，**没有 prefetch / 预取上限**
- 可选叠加全局 `request_rate`（RPS）限速；`agent_hint` 是否透传由 `pass_agent_hint` 开关控制

## 一、数据流总览

```
数据集（含 session_id 列，已按 (session_id, session 内顺序) 排序）
  └─ GenInferencer.get_data_list      为每条样本注入 session_id / stage
      └─ SharedMemory                 把 data_list pickle 进共享内存（与请求粒度相同，整集转储）
          └─ janus.Queue              单进程内存缓冲（容量 = batch_size+1）
              └─ dispatcher 协程       持续读 janus 直到读完（复用请求粒度加载），按 session_id 路由
                  └─ per-session asyncio.Queue   （每个 session 一个 FIFO 队列）
                      └─ session_worker(sid)
                            async with session_sem    # 限并发 session 数 (session_concurrency)
                            while True:
                               await q.get()          # 同 session 严格 FIFO
                               if token_bucket: await acquire  # 可选全局限速
                               await do_request(...)  # 串行 → at-most-1 in-flight
                            # 消费到 stage=end → 立即 break，释放槽位
```

关键点：

- **数据加载与请求粒度完全相同**：整集 `pickle` 进共享内存 → producer → janus，主流程复用，不重排数据。
- `batch_size` 在 session 模式下**只是 janus 队列容量**，不决定"某个 session 加载多少请求"。
- **没有 prefetch / 预取上限**。dispatcher 像请求粒度一样把所有请求读到底、按 session_id 路由到各自
  per-session 队列；并发完全由 `session_sem`（`session_concurrency`）+ `stage` 生命周期控制：
  - 同一 session 串行消费（at-most-1 in-flight）
  - session 消费到 `stage=end` 即结束，释放 `session_sem` 槽位给其他 session
  - 无 `end` 标记的 session 在整集读完后收到 `None` 哨兵，兜底收尾

## 二、文件清单

### 代码改动（6 个已有文件）

| 文件 | 改动 |
|---|---|
| `ais_bench/benchmark/openicl/icl_dataset_reader.py` | 新增 `get_session_id()`，对称于 `get_timestamp` |
| `ais_bench/benchmark/models/output.py` | `Output.__init__` 新增 `self.session_id` 字段 |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py` | `get_data_list` 注入 `session_id` + `stage`，自动从 `agent_hint.session_id`/`agent_hint.stage` 提取，透传额外字段，并按 session 校验 start/end 标记；`do_request` 取出 `session_id`/`stage`（控制字段不进 body），按 `pass_agent_hint` 决定是否透传 `agent_hint`；检测 `manage_request=True` 的控制面请求（如 evict）——`pass_agent_hint=False` 时直接跳过不发，`pass_agent_hint=True` 时发送但跳过 `report_cache_info` 不计入统计 |
| `ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py` | 新增 `_session_aware_worker_loop`（复用请求粒度加载 + start/end 生命周期）；`__init__` 接收 `session_aware/session_concurrency/pass_agent_hint`；`inference_with_shm` 分发 |
| `ais_bench/benchmark/tasks/openicl_api_infer.py` | 读取 session 配置并把 3 个参数透传到 inferencer_cfg；session 模式强制单进程 |
| `ais_bench/benchmark/models/api_models/vllm_custom_api.py` | `get_request_body` 合并 `args`（透传 `agent_hint` 等额外字段进 body） |
| `ais_bench/benchmark/models/api_models/vllm_custom_api_chat.py` | 同上，chat 版合并 `args` |

### 新增文件（配置 + 临时数据）

| 文件 | 用途 |
|---|---|
| `ais_bench/datasets/session_demo/session_demo_agent.jsonl` | 基于 `agent_hint` 嵌套结构的数据集（20 个 session，180 条请求，每 session 8-10 条）；可用 `tools/gen_session_demo.py` 重新生成 |
| `ais_bench/benchmark/configs/datasets/custom/session_demo_agent_gen.py` | dataset config（`input_columns` 不含 `session_id`，自动从 `agent_hint.session_id/.stage` 提取并透传 `agent_hint`） |
| `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py` | model config，派生自 `vllm_api_general_stream.py` |
| `ais_bench/benchmark/configs/api_examples/infer_vllm_api_stream_session.py` | 总装配 config（装载 agent_hint 版 dataset） |
| `ais_bench/benchmark/configs/api_examples/infer_vllm_api_stream_session_single_turn.py` | 单轮 session 数据集专用总装配 config（`single_turn_datasets`，输出目录独立） |
| `ais_bench/benchmark/configs/datasets/custom/session_single_turn_gen.py` | 单轮 session 数据集 config（`abbr='session-single-turn'`），数据来自 `session_single_turn.jsonl` |
| `ais_bench/datasets/session_demo/session_single_turn.jsonl` | 单轮数据集：20 个 session，每个 session 2 条记录（1 推理 + 1 驱逐），可用 `tools/gen_session_single_turn.py` 重新生成 |
| `tools/gen_session_single_turn.py` | 生成单轮 session 数据集脚本（推理请求 `manage_request=False` + 驱逐请求 `manage_request=True`） |
| `tools/bench_dataset_direct.py` | 直接发送数据集脚本（绕过 aisbench 框架，测纯 HTTP 发送的开销，用于和框架对比） |

> 本实现只保留 **agent_hint 嵌套结构**的数据形状：`session_id`/`stage` 放在 `agent_hint` 里，
> 由 `get_data_list` 自动提取；`agent_hint` 在 `pass_agent_hint=True` 时透传到请求 body。

## 三、配置说明

### model config（`vllm_api_general_stream_session.py`）

```python
dict(
    attr="service",
    type=VLLMCustomAPI,
    stream=True,
    session_aware=True,           # 启用 session 模式
    session_concurrency=5,        # 同时活跃（执行）的 session 数上限
    pass_agent_hint=False,        # True=把 agent_hint 透传到 vLLM body；False=仅内部用（提取 session_id/stage，控制面请求直接跳过）
    batch_size=5,                 # session 模式下退化为 janus 队列容量，建议 = session_concurrency
    request_rate=0,               # 0=不限速；设 >0 叠加全局 RPS 限速
    ...
)
```

| 参数 | 含义 | Session 模式下的作用 |
|---|---|---|
| `session_aware` | 是否启用 session 并发控制 | `True` 走新 worker loop |
| `session_concurrency` | 同时活跃（执行）的 session 数 | 控制跨 session in-flight 总量（每个 session 1 条在途） |
| `pass_agent_hint` | 是否透传 `agent_hint` 到 API body | `True` 合并进 vLLM 请求 body（含控制面请求）；`False` 仅用于内部提取 `session_id`/`stage`，且 `manage_request=True` 的控制面请求**直接跳过**（不发、不计入统计） |
| `batch_size` | | 退化为 janus 队列容量（内存缓冲） |
| `request_rate` | 全局 RPS | 可选叠加限速，`0` 关闭 |
| `use_timestamp` | 是否用数据集时间戳控制节奏 | session 模式下不冲突，可与 session 并发叠加 |

> 注意：session 模式**没有 prefetch / 预取上限**，数据加载完全复用请求粒度路径（整集转储 + 全量读取）。
> 并发只由 `session_concurrency` + `stage` 生命周期控制。

### dataset config（`session_demo_agent_gen.py`）

session 的 `session_id`/`stage` 放在 **`agent_hint` 嵌套 dict** 里，
`input_columns` **不需要**写 `session_id`/`stage` —— 系统会自动从 `agent_hint` 提取：

```json
{"question": "...", "answer": "...",
 "agent_hint": {
   "session_id": "sess_001",
   "parent_session_id": "sess_001_msg_0",
   "cache_control": {},        // 保留字段，可留空
   "context_management": {},   // 保留字段，可留空
   "stage": "start"            // start / stage-N / end，控制 session 起止
 }}
```

```python
agent_demo_reader_cfg = dict(
    input_columns=['question'],     # 不含 session_id / stage
    output_column=None,
)
```

**行为**：
- `session_id` 从 `agent_hint.session_id` 自动提取（顶层列优先，其次 `agent_hint.session_id`，都没有则置空）
- `stage` 从 `agent_hint.stage`（或顶层 `stage` 列）自动提取，用于 session 生命周期判定
- `agent_hint` 透传与否由 `pass_agent_hint` 决定：
  - `pass_agent_hint=True`：`agent_hint` 作为**额外字段**原样合并进 vLLM 请求 body
  - `pass_agent_hint=False`（默认）：仅用于内部提取 `session_id`/`stage`，**不透传**
- `question`/`answer`/`session_id`/`stage`/`timestamp` 等消耗/控制字段**不会**进 body
- 运行时会对每个 session 校验：第一个有意义的 `stage` 应为 `start`、最后一个应为 `end`，
  否则只在日志里 warning（不致命）

**session_id 提取优先级**：顶层 `session_id` 列 > `agent_hint.session_id` > 空。
**stage 提取优先级**：顶层 `stage` 列 > `agent_hint.stage` > 空。

### 自己的数据集要求

1. 数据文件需含 `session_id`（要么顶层列，要么 `agent_hint.session_id`；jsonl 每行一个 JSON）
2. **每条请求需带 `stage` 标记**（顶层列或 `agent_hint.stage`）：首条 `start`、末条 `end`、中间 `stage-N`
3. **预处理时按 `(session_id, session 内顺序)` 排序**，保证同 session 请求相邻且有序
   （代码不重排，只透传；同一 session 的请求必须是连续的）
4. 若想让 `agent_hint` 等字段进 vLLM 请求 body，把 `pass_agent_hint` 设为 `True` 即可

### 控制面请求（manage_request / evict）

数据集里可以混入**控制面请求**，用于在真实推理之间插入 session 级 KV cache 管理（如 evict）：

```json
{"question": "",
 "agent_hint": {
   "session_id": "single-011",
   "context_management": {
     "manage_request": true,
     "edits": [{"type": "evict", "target": "session"}]
   },
   "stage": "end"
 }}
```

语义（`icl_gen_inferencer.py::do_request` 处理）：

- **检测**：`context_management.manage_request == true` 的请求标记为控制面请求
- **`pass_agent_hint=False`**：直接 `return`，**不发 HTTP、不进统计**（agent_hint 不透传时这种请求毫无意义）
- **`pass_agent_hint=True`**：正常发送（vLLM 端执行 edits，如 evict 释放 KV block），但**跳过 `report_cache_info`**，不进入任何性能统计
- 注意 **`manage_request=True` 的请求不会被当作推理执行**（服务器端直接跑 edits），所以统计里的 N 只包括真正的推理请求

### 新增数据集（快速步骤）

**做法：复制同结构 config，改 2 个字段。**

以 `session_single_turn_gen.py`（带 agent_hint 的单轮数据集）为模板：

1. **准备数据文件**：把 jsonl 放到 `ais_bench/datasets/<your_dir>/<your_name>.jsonl`
   （结构需和模板一致：带 `question` + `agent_hint`，每行一个 JSON）
2. **复制 config 文件**并改两行：
   ```bash
   cp ais_bench/benchmark/configs/datasets/custom/session_single_turn_gen.py \
      ais_bench/benchmark/configs/datasets/custom/my_new_dataset.py
   ```
   - `abbr='session-single-turn'` → `abbr='my-new'`（唯一标识，仅 config 内部使用）
   - `path='ais_bench/datasets/session_demo/...'` → `path='ais_bench/datasets/<your_dir>/<your_name>.jsonl'`
   - 若 jsonl 列名不同，同步改 `reader_cfg.input_columns`（如 `['question']`）和 `infer_cfg.prompt_template.template`（如 `'{question}'`）
   - `single_turn_datasets` → `my_new_datasets`（如果 config 驱动引用）
3. **使用**，二选一：
   ```bash
   # 方式 A：命令行按文件名（推荐，零 config 改动）
   python -m ais_bench.benchmark.cli.main --mode perf -m vllm_api_general_stream_session \
     -d my_new_dataset --num-warmups 0 --work-dir outputs/my-new
   # 方式 B：config 驱动（改 api_examples 里的总装配，import 新 dataset 并赋给 datasets）
   ```

> `-d` 匹配的是**文件名**（`my_new_dataset`），不是 `abbr`。abbr 只影响输出标志和方式 B 的引用名。
> 配置文件里 `path` 相对源码根目录，也支持绝对路径。

若你的 jsonl **不带 agent_hint**（纯 `{question: ...}`），模板换成 `send_benchmark` 的 `challenger_send_benchmark_gen.py`，
或直接命令行 `--custom-dataset-path` + `--custom-dataset-data-type qa --custom-dataset-infer-method gen` 零配置跑。
注意：`--custom-dataset-*` 的模板**透传不了 agent_hint**（session 列透传依赖 `get_data_list` 的 extra-fields 逻辑）。

## 四、运行

### 4.1 前置：确认 vLLM 服务就绪

默认连接 `0.0.0.0:31025`（`vllm_api_general_stream_session.py` 里的 `host_ip`/`host_port`，
可用 `--url` 覆盖 / 直接改 config）。两种后端可选：

**方式 A：真实 vLLM 服务**
```bash
# 起好你的 vLLM（OpenAI 兼容接口），监听 31025
vllm serve <model> --port 31025
```

**方式 B：用假服务模拟（无需真实模型，方便看时序）**
```bash
cd D:/agent/agent_hint/benchmark/tools/infer_serve_simulator
pip install -r requirements.txt        # 首次装依赖
bash launch_service.sh                 # gunicorn 监听 0.0.0.0:8080
```
- 可调 `api/api_config.yaml` 里的 `stream_latency.ttft / tpot` 控制延迟，便于观察 session 内时序
- 若用模拟器（8080），记得把 model config 的 `host_port` 改成 `8080`

### 4.2 运行（三种方式）

**方式一：直接跑总装配 config**

config 驱动模式：把数据集路径在 config 文件里写死（`datasets` 键），一条命令跑
```bash
cd D:/agent/agent_hint/aisbench/benchmark

# agent_hint 版多轮 dataset（session_demo_agent_gen.py）
python -m ais_bench.benchmark.cli.main \
  --mode perf --num-warmups 0 \
  ais_bench/benchmark/configs/api_examples/infer_vllm_api_stream_session.py

# 单轮 dataset（session_single_turn_gen.py，含 evict 记录）
python -m ais_bench.benchmark.cli.main \
  --mode perf --num-warmups 0 \
  ais_bench/benchmark/configs/api_examples/infer_vllm_api_stream_session_single_turn.py
```

**方式二：命令行指定单个 model + dataset（按文件名匹配）**

不写 config，用 `-m <model_config文件名>` + `-d <dataset_config文件名>`（**匹配文件名，不带 `.py`**）：
```bash
python -m ais_bench.benchmark.cli.main \
  --mode perf -m vllm_api_general_stream_session -d session_single_turn_gen \
  --num-warmups 0 --work-dir outputs/vllm-stream-session-agent
```
- 使用单轮 dataset 就 `-d session_single_turn_gen`；多轮就 `-d session_demo_agent_gen`
- 注意：`-d` 匹配文件名，与 config 里的 `abbr` 无关（想在多个 config 里选一个用 abbr，需走方式一的 config）

**方式三：直接发送脚本（绕过 aisbench 框架，对比框架开销）**

`tools/bench_dataset_direct.py` 直接读 `session_single_turn.jsonl`，用固定并发原样发请求，
测纯 HTTP 发送的 TTFT / TPOT / E2EL，用于和 aisbench 框架结果对比：
```bash
# 带 agent_hint（发推理 + 驱逐请求，模拟 aisbench "on"）
python tools/bench_dataset_direct.py

# 不带 agent_hint（只发 20 条推理请求，模拟 "off"）
python tools/bench_dataset_direct.py --no-agent-hint

# 自定义并发 / 数据集 / URL
python tools/bench_dataset_direct.py --concurrency 5 \
  --dataset ais_bench/datasets/session_demo/session_single_turn.jsonl \
  --url http://0.0.0.0:31025/v1/chat/completions
```
语义对齐 aisbench：按 `session_id` 分组、`concurrency` 个 session 同时在跑、一个 session
推理 → 驱逐完成后才释放槽位；驱逐请求发送但不计入统计。

> `-m`/`-d` 让 CLI 从 `--config-dir`（默认 `benchmark/configs`）按名字找 model/dataset config。
> 完整命令参数见 `python -m ais_bench.benchmark.cli.main -h`；`--config` 不存在——config 是位置参数。

### 4.3 参数说明

| 参数 | 作用 |
|---|---|
| `--mode perf` | 开启 perf 模式，`output.record_time_point` 才记录时间点，用于算 TTFT/ITL |
| `--num-warmups 0` | 跳过预热，快速验证 |
| `--work-dir` | 输出目录（预测、日志、统计） |
| `-m` / `--models` | model config 文件名（不带 `.py`），如 `vllm_api_general_stream_session` |
| `-d` / `--datasets` | dataset config 文件名（不带 `.py`），如 `session_single_turn_gen` |
| `-w` | 输出目录的简写 |

### 4.4 观察 session 调度日志

运行时在 `_session_aware_worker_loop` 里打了 `[session-aware]` 日志（info 级别），可直接看到调度时序：

```
[session-aware] session_aware=True, session_concurrency=5, pass_agent_hint=False
[session-aware] skip manage_request since pass_agent_hint=False, session_id=single-011 data_id=22
[session-aware] admit NEW session sess_001 (data_id=0)
[session-aware] session sess_001 ACQUIRED slot
[session-aware] session sess_001 START request stage=start data_id=0
[session-aware] session sess_001 END   request stage=end   data_id=3
[session-aware] session sess_001 reached stage=end, ending
[session-aware] session sess_001 releasing slot
```

- `admit NEW session X`：dispatcher 读到底、为 session 建队列并启动其 worker（无 prefetch 等待）
- `ACQUIRED slot`：session 拿到 `session_sem` 并发槽位（受 `session_concurrency` 限）
- `START/END`：单条请求发/收（同 session 顺序）
- `reached stage=end, ending` / `releasing slot`：session 消费到 `end` 并运行完，立即让出槽位

结果输出在 `--work-dir` 下（`predictions/`、`logs/`）。

### 4.5 用模拟器验证调度时序（无需真实 vLLM）

`tools/session_aware_simulator.py` 独立复刻 `_session_aware_worker_loop` 的调度逻辑，
可验证"运行中 session + 旧 session 补请求 + end 释放槽位 + 无 end 标记兜底"等场景及不变量：

```bash
cd D:/agent/agent_hint/aisbench/benchmark
python tools/session_aware_simulator.py
```

输出末尾的 `===== VALIDATION =====` 会校验：
- 所有请求恰好送达、恰好发送一次
- 同 session 严格串行（无并行在途违规）
- 活跃 session 数从未超过 `session_concurrency`

## 五、单进程 vs 多进程控制

**Session 模式强制单进程**（`openicl_api_infer.py::_get_workers_num`）：

```python
def _get_workers_num(self):
    if self.session_aware:
        return 1          # 强制单进程
    ...
```

**原因**：多进程会把不同请求分发到不同 worker 进程，而 `session_aware` 需要"同一 session 的所有请求
落在同一进程、按序消费"，否则会破坏 session 内顺序和 at-most-1 in-flight 语义。

**非 session 模式**下仍按原逻辑多进程：
`workers_num = ceil(concurrency / 500)`，并 `min(..., MAX_WORKERS_NUM = cpu*0.8)`，
也可用 `global_consts.WORKERS_NUM` 强制覆盖。多进程并发靠 `multiprocessing` + 共享 GlobalRamValue/共享内存。

## 六、关键不变量

| 需求 | 保证机制 |
|---|---|
| 同 session 请求按序 | 数据集预排序 + janus FIFO + per-session `asyncio.Queue` FIFO |
| 同 session at-most-1 in-flight | `session_worker` 内 `await do_request` 串行 |
| 跨 session 最多 N 个活跃 | `async with session_sem`（N=`session_concurrency`） |
| 数据加载无 prefetch 上限 | 复用请求粒度路径：整集转储 + dispatcher 全量读取，无预取/缓存逻辑 |
| session 消费到 `end` 即结束释放槽位 | `session_worker` 处理到 `stage=end` 立即 `break`，退出 `async with session_sem` 释放槽位 |
| 无 `end` 标记的 session 兜底收尾 | 整集读完后 dispatcher 向各 session 队列发 `None` 哨兵，worker `break` 退出 |
| 全局 RPS（可选） | `token_bucket.acquire` 在 `do_request` 前 |
| 同 session 不跨进程 | `_get_workers_num` session 模式 `return 1` |
| `session_id`/`stage` 不污染 generate 参数 | `do_request` 用 `data.pop("session_id")`/`data.pop("stage")` 取出 |
| `agent_hint` 仅在 `pass_agent_hint=True` 时进 body | `do_request` 按 `self.pass_agent_hint` 决定是否 `pop("agent_hint")` |
| 控制面请求（`manage_request=True`）不计入统计 | `do_request` 检测 `is_manage_req`，跳过 `report_cache_info`；`pass_agent_hint=False` 时直接 `return` |
| 统计样本数 = 真实推理请求数 | evict/管理请求不 contribute 到结果，perf 表 N = 推理请求条数 |
