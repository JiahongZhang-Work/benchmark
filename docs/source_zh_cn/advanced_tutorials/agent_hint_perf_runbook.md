# Agent Hint 性能压测 Runbook（Agent 可执行版）

本文是给自动化 agent（或第一次接触该特性的开发者）使用的**可执行操作手册**：只写"做什么、怎么判断成功、失败怎么排查"。特性原理与指标含义见同目录的 [Session 感知并发与 Agent Hint 测评指南](./session_aware_agent_hint.md)。

适用目标：用真实 agent 多轮会话数据回放，测试 vLLM 侧 `agent_hint`（会话感知 / 上下文管理）特性的性能。

---

## 0. 端到端流程图

```text
原始日志（model_io 目录 / 纯 question JSONL）
        |
        |  Step 1：手动执行 build_agent_dataset.py（不进 ais_bench 主流程）
        v
agent_dataset_out.jsonl（每行含 agent_hint，Jiuwen 模式还含 max_tokens）
        |
        |  Step 2：把文件路径写进 dataset 配置的 path
        v
dataset 配置（CustomDataset）
        |
        |  Step 3：确认 session 模式模型配置
        v
ais_bench -m perf
        |
        v
outputs/<timestamp>/performances/<model_abbr>/<dataset_abbr>.{json,csv}
```

最终产物：

| 产物 | 位置 |
|---|---|
| 构造好的数据集 | `build_agent_dataset.py -o` 指定的路径 |
| 每请求明细 | `<work_dir>/performances/<model_abbr>/<dataset_abbr>_details.jsonl` |
| 性能汇总 | `<work_dir>/performances/<model_abbr>/<dataset_abbr>.json` / `.csv` |
| 失败请求 | `<work_dir>/performances/<model_abbr>/<dataset_abbr>_failed.jsonl` |

`work_dir` 默认是 `outputs/default/<timestamp>`（`ais_bench/benchmark/cli/config_manager.py:867-890`）。

---

## 1. Preflight：执行前自检

所有命令默认在**仓库根目录**执行（`benchmark/`）。

```bash
# 1) 当前目录应是仓库根目录
test -f ais_bench/datasets/agent/build_agent_dataset.py && echo OK_SCRIPT
test -f ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py && echo OK_MODEL_CFG
test -f ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py && echo OK_DATA_CFG

# 2) ais_bench 可用
ais_bench --help >/dev/null && echo OK_CLI

# 3) vLLM 服务可用（按实际 host/port 替换）
curl -s http://localhost:31036/v1/models | head -c 200
```

自检清单：

- [ ] 仓库根目录正确
- [ ] `ais_bench` 命令可执行
- [ ] 目标 vLLM 服务已启动，且暴露 `/v1/chat/completions`
- [ ] 已知模型的 host、port、服务端模型名
- [ ] 原始数据已准备好（见第 2 节）
- [ ] 如果服务端未实现 `agent_hint` 处理，本次只能得到"普通请求压测"结果，不能评估特性收益

---

## 2. 输入契约

数据来源有两种，由 `-t/--src-dataset-type` 选择。

### 2.1 `Jiuwen`：真实 agent 轨迹（推荐）

输入是**目录**，一个请求一个 JSON 文件。

**文件名必须满足**（脚本会按 `_` 切分并直接转 int，`build_agent_dataset.py:396-405`）：

```text
G<任意>_S<session号>_R<request号>[_management...].json
```

示例：

```text
model_io/
├── G0001_S001_R001_chat.json
├── G0001_S001_R002_chat.json
├── G0001_S002_R001_chat.json
└── G0002_S001_R001_chat.json
```

- `S` 决定会话，`R` 决定会话内轮次；脚本按 `_S`、`_R` 排序（`:381-394`）。
- 文件名里的 `management` 不生效，是否为控制面请求只看 `agent_hint.context_management.manage_request`。

**单文件结构**（脚本实际读取的字段，`:407-443`）：

```json
{
  "request": {
    "agent_hint": {
      "session_id": "sess-A",
      "parent_session_id": "parent-1",
      "cache_control": {},
      "context_management": {
        "manage_request": false,
        "edits": []
      }
    },
    "messages": [
      {"role": "system", "content": "You are a coding agent..."},
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."},
      {"role": "tool", "content": "...", "tool_call_id": "..."}
    ],
    "tools": [
      {"type": "function", "function": {"name": "read_pdf", "description": "...", "parameters": {}}}
    ]
  },
  "usage": {
    "prompt_tokens": 900,
    "completion_tokens": 100,
    "total_tokens": 1000
  }
}
```

字段要求：

| 字段 | 必需性 | 说明 |
|---|---|---|
| `request.agent_hint.session_id` | 语义必需 | 会话标识；缺失会归到空字符串组 |
| `request.agent_hint.parent_session_id` | 语义必需 | **运行时真正用于分桶的 key**；缺失会归到 `"default"`，所有会话退化为串行 |
| `request.agent_hint.context_management.manage_request` | 可选 | `true` 为控制面请求；存在但非 `true` 时脚本会清空 `edits`（`:413-417`） |
| `request.agent_hint.context_management.edits` | 可选 | `[{"type": "offload/prefetch/evict", "target": "session"}]` |
| `request.agent_hint.cache_control` | 可选 | 占位字段，原样透传，运行时无逻辑消费 |
| `request.agent_hint.stage` | 不需要 | 脚本会按 `R` 序号覆盖（`:419-420`），后续还会重排 |
| `request.messages` | 非 SWE 必需 | 非 `--swe-format` 时会执行 `m["content"]`，**每条消息必须有 `content`** |
| `request.messages` | SWE 必需 | `--swe-format` 时原样透传，需为 OpenAI chat 格式 |
| `request.tools` | 仅 `--swe-format` 使用 | OpenAI function-calling 格式 |
| `usage.total_tokens` / `usage.prompt_tokens` | 可选但强烈建议 | 用于计算 `max_tokens = total - prompt`；缺失或为 0 时取 `1`（`:422-429`） |

### 2.2 `default`：普通问题 JSONL

输入是**单个 JSONL 文件**，每行至少含 `question`：

```jsonl
{"question": "帮我总结这篇文章：..."}
{"question": "帮我总结这篇文章：..."}
```

- 必须用 `--tokenizer` 指定 HuggingFace `tokenizer.json`（`:999-1008`）。
- 脚本会按 token 前缀自动分组、生成 `session_id`（`sid_00001` 形式）和 `stage`（`:876-962`）。
- 输出**不带** `max_tokens`，实际生成长度走模型配置的 `max_out_len`。

### 2.3 `agent_hint` 字段契约

构造阶段真正认识并会生成的字段（`_make_manage_request`，`:140-172`）：

```json
{
  "session_id": "...",
  "parent_session_id": "...",
  "cache_control": {},
  "context_management": {"manage_request": false, "edits": []},
  "stage": "start"
}
```

| 字段 | 类型 | 取值 | 作用 |
|---|---|---|---|
| `session_id` | string | 任意唯一串 | 会话标识；用于日志与 `output.session_id` |
| `parent_session_id` | string | 任意串，通常同 `session_id` | 调度分桶 key（`icl_base_api_inferencer.py:466-468`）；相同值 = 同会话串行 |
| `stage` | string | `start` / `stage-N` / `end` / `""` | 轮次标记；`end` 触发会话槽位释放 |
| `cache_control` | dict | 当前固定 `{}` | 预留占位，仅透传 |
| `context_management.manage_request` | bool | `true` / `false` | 控制面请求标记；`true` 时从性能统计剔除 |
| `context_management.edits` | list | `{"type": "offload/prefetch/evict", "target": "session"}` | `offload` 触发调度器分裂新 task（`icl_base_api_inferencer.py:233-247, 378-392`）；其余由服务端消费 |

注意：

1. `Jiuwen` 模式会把源 `agent_hint` 整个 dict 原样保留（`:432`），因此额外的自定义字段会一并透传到 vLLM 请求体，但 ais_bench 自身不消费。
2. `default` 模式只生成 `session_id`、`parent_session_id`、`stage`、`context_management`，没有 `cache_control`。
3. `edits` 只在 `manage_request=true` 时保留；普通请求的 `edits` 会被清空。

---

## 3. Step 1：构造数据集

> **路径解析规则（重要）**：脚本的 `-s` 和 `-o` 如果写相对路径，会相对**脚本所在目录**解析（`build_agent_dataset.py:79-88`），而不是当前工作目录。为避免歧义，**推荐一律使用绝对路径**。

### 3.1 Jiuwen 普通格式

```bash
python ais_bench/datasets/agent/build_agent_dataset.py \
    -t Jiuwen \
    -s /abs/path/to/model_io \
    -o /abs/path/to/agent_dataset_out.jsonl \
    --evict --add-pause 0.5
```

### 3.2 Jiuwen SWE / chat 格式

保留 system/assistant/tool 角色，使用 `messages` + `tools` 对接 chat 接口：

```bash
python ais_bench/datasets/agent/build_agent_dataset.py \
    -t Jiuwen \
    -s /abs/path/to/model_io \
    -o /abs/path/to/agent_dataset_out.jsonl \
    --swe-format --evict
```

### 3.3 default 普通问题

```bash
python ais_bench/datasets/agent/build_agent_dataset.py \
    -t default \
    -s /abs/path/to/questions.jsonl \
    -o /abs/path/to/agent_dataset_out.jsonl \
    --tokenizer /abs/path/to/tokenizer.json
```

### 3.4 参数速查

| 参数 | 适用 | 作用 |
|---|---|---|
| `-t/--src-dataset-type` | 通用 | `Jiuwen` 或 `default` |
| `-s/--src` | 通用 | Jiuwen 传目录；default 传 JSONL 文件 |
| `-o/--output` | 通用 | 输出 JSONL 路径 |
| `--tokenizer` | default 必填 / Jiuwen 可选 | 分词器；Jiuwen 仅 `--max-token-len` 需要 |
| `-c/--cut-count` | Jiuwen | 每个会话只保留最后 N 轮 |
| `-n/--session-count` | Jiuwen | 只保留前 N 个大组（按 `parent_session_id`） |
| `-p/--add-pause` | Jiuwen | 给指定比例的会话随机插一对 offload+prefetch |
| `--evict` | Jiuwen | 用真实 evict/offload/end 组合替代默认逻辑 |
| `--copy n` | Jiuwen | 每个大组复制 n 份并加随机后缀 |
| `--max-token-len` | Jiuwen | 丢弃 question 超长记录（需 `--tokenizer`） |
| `--swe-format` | 通用 | 输出 `messages/tools/tool_choice` chat 格式 |
| `-P/--preview` | 通用 | 预览：替换 prompt 文本并打印分组结构 |
| `--show-agent-hint` | 通用 | 配合 `--preview` 打印完整 `agent_hint` |

### 3.5 构造结果验收

```bash
# 行数
wc -l /abs/path/to/agent_dataset_out.jsonl

# 抽查字段（需要 jq）
head -n 1 /abs/path/to/agent_dataset_out.jsonl | jq '{keys: keys, agent_hint: .agent_hint}'

# 统计控制面/数据面请求数
jq -r '.agent_hint.context_management.manage_request' /abs/path/to/agent_dataset_out.jsonl | sort | uniq -c
```

推荐先跑一次预览，确认层级结构无误：

```bash
python ais_bench/datasets/agent/build_agent_dataset.py \
    -t Jiuwen -s /abs/path/to/model_io \
    -o /tmp/preview.jsonl \
    --evict --preview --show-agent-hint
```

预期 stdout 包含：

- `[Jiuwen] Reading N files from ...`
- `[Jiuwen] group_by_parent_sessions_evict: ... prefetch=.. offload=.. evict=.. evict+end=..`
- `========== ... Preview (N records, M ParentGroups) ==========`
- `----- Summary: X data-plane + Y manage-request = Z total -----`

验收标准：

- [ ] 记录数符合预期
- [ ] 每个 ParentGroup 下 SessionGroup 顺序正确
- [ ] 数据面记录 `manage_request=false`
- [ ] 控制面记录 `manage_request=true` 且 `edits` 非空
- [ ] 每个会话首条 `stage=start`
- [ ] 使用 `--evict` 时存在 `stage=end` 的切分标记

---

## 4. Step 2：配置数据集

编辑 `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py`：

```python
agent_reader_cfg = dict(
    input_columns=['question', 'max_out_len'],   # max_out_len 必须在列表里才会生效
    output_column=None,
)

agent_demo_datasets = [
    dict(
        abbr='session-agent',
        type=CustomDataset,
        path='ais_bench/datasets/agent/agent_dataset_out.jsonl',  # 改成你的文件
        reader_cfg=agent_reader_cfg,
        infer_cfg=agent_infer_cfg,
        eval_cfg=agent_eval_cfg,
    )
]
```

说明：

- `path` 若为相对路径，是相对**仓库根目录**解析（`get_data_path`，`datasets/utils/datasets.py:37-64`），也可直接写绝对路径。
- 仓库里默认的 `ais_bench/datasets/agent/example.jsonl` **不存在**，必须替换。
- `agent_hint` 不需要写进 `input_columns`，会自动从数据列透传（`icl_gen_inferencer.py:216-282`）。
- `max_tokens` 会在加载时被改名为 `max_out_len`（`datasets/custom.py:116-119`），只有 `max_out_len` 在 `input_columns` 时才覆盖模型配置的默认长度。

验收：

```bash
python -c "
from ais_bench.benchmark.datasets import CustomDataset
d = CustomDataset.load('ais_bench/datasets/agent/agent_dataset_out.jsonl')
print(len(d), d.column_names)
print(d[0])
"
```

---

## 5. Step 3：配置模型

编辑 `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py`，关键开关：

```python
dict(
    attr="service",
    type=VLLMCustomAPI,
    abbr="vllm-api-general-stream-session",
    path="/mnt/weight/Qwen3-8B",
    model="qwen",                    # 改成服务端实际模型名
    stream=True,
    use_chat_api=True,
    session_aware=True,              # 必开：按会话调度
    session_concurrency=4,           # 同时活跃会话数，按显存/负载调整
    pass_agent_hint=True,            # 要测特性必须开
    batch_size=4,                    # session 模式下是排队缓冲，建议等于 session_concurrency
    request_rate=0,                  # 0 表示不限速
    use_timestamp=False,
    retry=2,
    api_key="",
    host_ip="localhost",
    host_port=31036,                 # 改成实际端口
    max_out_len=256,
    generation_kwargs=dict(temperature=0, ignore_eos=False),
)
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `session_aware` | `False` | 开为 `True` 后按会话调度；强制单进程 |
| `session_concurrency` | `1` | 同时活跃会话数上限 |
| `pass_agent_hint` | `False` | `False` 时只内部用 `session_id/stage`，控制面请求直接跳过、不发；`True` 时把 `agent_hint` 合并进请求体 |
| `batch_size` | `1` | session 模式下退化为 janus 队列容量 |
| `request_rate` | `0` | 全局限速；`0` 不限速 |
| `stream` | `True` | 流式，才能统计 TTFT |
| `use_chat_api` | `True` | 打 `/v1/chat/completions` |
| `max_out_len` | `256` | 数据未提供 `max_out_len` 时的默认生成长度 |

---

## 6. Step 4：运行压测

### 6.1 CLI 方式（推荐给 agent）

```bash
ais_bench \
    --models vllm_api_general_stream_session \
    --datasets custom_agent_session_gen \
    --debug -m perf
```

- `--debug`：单进程、日志详细，建议先跑通。
- `-m perf`：性能模式。
- 默认 `work_dir=outputs/default/<timestamp>`；也可加 `--work-dir outputs/agent-hint`（会再拼时间戳子目录）。

### 6.2 组合配置方式

```bash
ais_bench --config ais_bench/configs/api_examples/infer_vllm_api_stream_session.py -m perf
```

该组合配置的 `work_dir` 是 `outputs/api-vllm-stream-session/`。

### 6.3 先看配置解析是否正确（不发请求）

```bash
ais_bench \
    --models vllm_api_general_stream_session \
    --datasets custom_agent_session_gen \
    --mode perf --search
```

确认两个配置的绝对路径符合预期。

### 6.4 运行中应看到的日志

```text
[session-aware] enabled, session_concurrency=4, pass_agent_hint=True, ...
[session-aware] force single worker process to preserve in-session order
[session-aware] session <psid> acquired exec slot, work queue len is ...
[session-aware] session <psid> START request stage=... data_id=...
[agent_hint][send] data_id=... session_id=... agent_hint={...}
[session-aware] session <psid> END   request stage=... data_id=...
```

如果只看得到 "skip manage_request since pass_agent_hint=False"，说明模型配置没生效或没设 `pass_agent_hint=True`。

---

## 7. Step 5：读取结果

目录结构：

```text
<work_dir>/<timestamp>/
├── configs/                                  # 本次运行的配置快照
└── performances/
    └── <model_abbr>/
        ├── <dataset_abbr>_details.jsonl      # 每请求明细（含 manage_request 标记）
        ├── <dataset_abbr>_failed.jsonl       # 失败请求
        ├── <dataset_abbr>.json               # 性能汇总（common results）
        └── <dataset_abbr>.csv                # 性能汇总表
```

常用查看命令：

```bash
# 汇总 JSON
ls <work_dir>/<timestamp>/performances/*/
cat <work_dir>/<timestamp>/performances/*/<dataset_abbr>.json | head -c 2000

# 汇总 CSV
column -s, -t < <work_dir>/<timestamp>/performances/*/<dataset_abbr>.csv

# 明细里控制面请求是否被标记
jq -r '.extra_perf_data.manage_request' \
    <work_dir>/<timestamp>/performances/*/<dataset_abbr>_details.jsonl | sort | uniq -c
```

主要指标：

| 指标 | 含义 |
|---|---|
| Total / Success / Failed | 请求数；控制面请求已从统计中剔除（`default_perf.py:145-151`） |
| 吞吐（token/s、request/s） | 整体处理能力 |
| TTFT | 首 token 延迟 |
| TPOT | 每输出 token 耗时 |
| Concurrency | 实际并发，不会超过 `session_concurrency` |

验收标准：

- [ ] `Failed` 在可接受范围，且失败原因已排查
- [ ] `Concurrency` 接近 `session_concurrency`
- [ ] `Total` 约等于数据集中 `manage_request=false` 的记录数
- [ ] `_details.jsonl` 中控制面请求被标记并排除
- [ ] 与"`pass_agent_hint=False` 的对照实验"相比，指标变化符合预期（验证特性是否真正生效）

---

## 8. 关键语义（避免误判）

1. **分桶 key 是 `parent_session_id`，不是 `session_id`**（`icl_base_api_inferencer.py:466-468`）。缺失时全部进入 `"default"` 桶，退化为串行。
2. **同一会话严格串行，跨会话并行**；总并发上限由 `session_concurrency` 控制。
3. **`stage="end"` 会结束当前 span 并释放并发槽**（`icl_base_api_inferencer.py:386-392`）。没有 `end` 的会话会一直占槽到数据耗尽。
4. **`edits[].type="offload"` 会分裂出新 task**（`:378-392`）；`prefetch` / `evict` 只是透传给服务端的信号，ais_bench 不据此调度。
5. **`manage_request=true` 的请求**：`pass_agent_hint=False` 时由 `do_request` 直接跳过不发送；`True` 时照常发送但标记 `extra_perf_data.manage_request=True`，统计阶段剔除。
6. **`max_tokens` → `max_out_len`**：`CustomDataset.load` 会把 `max_tokens` 改名为 `max_out_len`（`datasets/custom.py:116-119`），之后按 AISBench 标准机制写入请求体 `max_tokens`。它不会以原始 `max_tokens` 字段再次透传。
7. **`pass_agent_hint=True` 只是把字段发出去**：服务端必须实现 `agent_hint.context_management` 的 offload/prefetch/evict，才能测出特性收益；否则等价于普通压测。

---

## 9. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| `ValueError: Jiuwen expects a directory` | `-s` 给了文件 | Jiuwen 传目录；default 才传文件 |
| `ValueError: default expects a file` | `-s` 给了目录 | 同上 |
| `int()` 转换报错 | 文件名不符合 `G*_S<num>_R<num>` | 重命名输入文件 |
| 找不到输出文件 | 相对 `-o` 被解析到脚本目录 | 用绝对路径 |
| `Dataset path ... is not exist` | dataset 配置的 `path` 不对，或 `example.jsonl` 未替换 | 改成真实文件（相对仓库根目录或绝对路径） |
| `Not found timestamps ... use_timestamp is True` | 配置开了 `use_timestamp` 但数据无 `timestamp` | 关掉 `use_timestamp` |
| 日志出现 `skip manage_request since pass_agent_hint=False` | 模型配置没开 `pass_agent_hint` | 设为 `True` |
| 所有请求都在同一个 session | `parent_session_id` 全为空 | 补 `parent_session_id` |
| 并发始终是 1 | `session_concurrency=1` 或数据只有一个会话 | 提高 `session_concurrency`，确认数据有多个 `parent_session_id` |
| 指标与普通压测几乎一样 | 服务端未实现 `agent_hint` 处理 | 检查服务端实现；做 `pass_agent_hint` 开/关对照 |
| 生成长度异常短 | 输入缺 `usage`，`max_tokens` 回退为 1 | 补 `usage.total_tokens` / `prompt_tokens` |
| 多进程起不来 | session 模式强制单进程 | 把 `runner.max_num_workers` 设为 1，使用 `--debug` |

---

## 10. 代码索引

| 环节 | 位置 |
|---|---|
| 数据集构造脚本 | `ais_bench/datasets/agent/build_agent_dataset.py` |
| 控制面记录生成 | `build_agent_dataset.py:140-172` |
| Jiuwen 解析与 stage 生成 | `build_agent_dataset.py:365-443` |
| evict/offload 分组 | `build_agent_dataset.py:496-590` |
| 随机暂停插入 | `build_agent_dataset.py:178-265` |
| `max_tokens` → `max_out_len` | `ais_bench/benchmark/datasets/custom.py:101-130` |
| dataset 配置 | `ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py` |
| 模型配置 | `ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py` |
| 组合配置 | `ais_bench/configs/api_examples/infer_vllm_api_stream_session.py` |
| session/stage 提取 | `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py:216-282` |
| manage_request 跳过与标记 | `ais_bench/benchmark/openicl/icl_inferencer/icl_gen_inferencer.py:85-113` |
| 请求体合并 | `ais_bench/benchmark/models/api_models/vllm_custom_api.py:116-155` |
| 开关注入与单进程 | `ais_bench/benchmark/tasks/openicl_api_infer.py:106-173` |
| session 调度 | `ais_bench/benchmark/openicl/icl_inferencer/icl_base_api_inferencer.py:233-500` |
| 统计剔除 | `ais_bench/benchmark/summarizers/default_perf.py:145-151` |

---

## 11. 最小可复制模板

```bash
set -euo pipefail

REPO=/abs/path/to/benchmark
SRC=/abs/path/to/model_io
OUT=/abs/path/to/agent_dataset_out.jsonl

cd "$REPO"

# 1) 构造数据集（先用 --preview 自检，再正式生成）
python ais_bench/datasets/agent/build_agent_dataset.py \
    -t Jiuwen -s "$SRC" -o /tmp/agent_dataset_preview.jsonl \
    --evict --add-pause 0.5 -P --show-agent-hint

python ais_bench/datasets/agent/build_agent_dataset.py \
    -t Jiuwen -s "$SRC" -o "$OUT" \
    --evict --add-pause 0.5

# 2) 手动（或由 agent 编辑）把 dataset 配置的 path 改成 "$OUT"，然后确认
DATASET_CFG=ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py
grep -n "path=" "$DATASET_CFG"
MODEL_CFG=ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py
grep -nE "session_aware|session_concurrency|pass_agent_hint|host_port|model=" "$MODEL_CFG"

# 3) 运行
ais_bench --models vllm_api_general_stream_session \
          --datasets custom_agent_session_gen \
          --debug -m perf \
          --work-dir outputs/agent-hint

# 4) 查看结果
find outputs/agent-hint -path "*performances*" -type f | sort
```

> 注意：模板里的 `$OUT` 必须在 dataset 配置中真实生效；如果不想每次手改配置，可把 `path` 直接写成绝对路径。
