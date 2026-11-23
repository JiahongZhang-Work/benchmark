# Agent Hint 性能压测标准执行流程（Agent SOP）

本文是**给自动化 agent 执行**的标准操作流程：输入一个 agent 轨迹数据集目录，按步骤完成
`agent_hint` 关闭 / 开启两组性能压测，并产出可对比、可复现的结果。

与 [Agent Hint 性能压测 Runbook](./agent_hint_perf_runbook.md) 的分工：

- Runbook：解释“参数是什么、代码在哪里、为什么这样”。
- 本文：只回答“先做什么、执行什么命令、看到什么算成功、失败怎么办”。

**服务端由其他人启动和运维**，本文只做客户端侧的校验、压测和结果分析，不包含服务端启动步骤。

---

## 0. 使用说明与全局变量

### 0.1 执行原则

1. 按 Step 顺序执行，**不得跳过门禁（Gate）**。
2. 每个 Step 都要记录命令、开始时间、预期输出、实际结果。
3. 任一 Gate 不通过时，停止后续压测，先按“故障排查”处理。
4. 正式测量不要使用 `--num-prompts`，它会截断会话，导致 `stage=end` 缺失和结果失真。
5. 同一组实验必须使用同一份数据；不同组之间必须做**缓存隔离**（见第 7 节）。

### 0.2 全局变量

执行前先填好下表，后续命令只引用变量。

| 变量 | 含义 | 示例 |
|---|---|---|
| `REPO` | AISBench 仓库根目录 | `/abs/path/to/benchmark` |
| `DATASET_DIR` | **原始轨迹目录**（一个请求一个 JSON） | `/abs/path/to/model_io` |
| `DATASET_JSONL` | 已构造好的 JSONL（可选，若已有） | `/abs/path/to/agent_dataset.jsonl` |
| `WARMUP_DATASET_DIR` | 预热用原始目录（可选，内容需与测量数据不同） | `/abs/path/to/model_io_warmup` |
| `OUT_ROOT` | 本次实验根目录 | `/abs/path/to/agent_hint_perf` |
| `RUN_ID` | 本次实验唯一标识 | `20261120_143000` |
| `RUN_TAG` | 单次 run 标识（组 + 并发 + 重复） | `${RUN_ID}_A_c8_r1` |
| `BASE_HOST` / `BASE_PORT` | **agent_hint 关闭**的服务地址 | `127.0.0.1` / `8000` |
| `HINT_HOST` / `HINT_PORT` | **agent_hint 开启**的服务地址 | `127.0.0.1` / `8001` |
| `SERVER_BASE` | 由 `BASE_HOST`/`BASE_PORT` 拼出的 URL | `http://127.0.0.1:8000` |
| `SERVER_HINT` | 由 `HINT_HOST`/`HINT_PORT` 拼出的 URL | `http://127.0.0.1:8001` |
| `MODEL` | 服务端实际模型名 | `my-model` |
| `TOKENIZER` | HuggingFace `tokenizer.json`（`default` 模式必填） | `/abs/path/to/tokenizer.json` |
| `CONC_LIST` | 待扫描的 `session_concurrency` | `4 8 16` |

初始化：

```bash
set -euo pipefail
export REPO=/abs/path/to/benchmark
export OUT_ROOT=/abs/path/to/agent_hint_perf
export RUN_ID=$(date +%Y%m%d_%H%M%S)
export BASE_HOST=127.0.0.1 BASE_PORT=8000
export HINT_HOST=127.0.0.1 HINT_PORT=8001
export SERVER_BASE="http://${BASE_HOST}:${BASE_PORT}"
export SERVER_HINT="http://${HINT_HOST}:${HINT_PORT}"
mkdir -p "$OUT_ROOT"/{data,logs,runs,metrics,configs}
```

### 0.3 客户端执行位置

所有命令默认在 `$REPO` 根目录执行：

```bash
cd "$REPO"
```

如果客户端在容器内，先进入容器，并确保 `$REPO` 已挂载、依赖已安装。**关键是让 Python 加载本仓库的
`ais_bench`，而不是镜像里安装的旧副本**（否则新特性代码不生效）：

```bash
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
python -c "import ais_bench; print(ais_bench.__file__)"
# 预期输出以 $REPO/ais_bench/__init__.py 开头
```

---

## 1. 端到端流程

```text
Step 0  预检（仓库 / 客户端 / 服务端 / 数据 / 设备）
   |
Step 1  校验原始数据集目录
   |
Step 2  构造 AISBench JSONL 并验收
   |
Step 3  配置 dataset / model（支持环境变量注入）
   |
Step 4  小样本冒烟（Agent Hint 关闭）
   |
Step 5  正式跑 A 组（hint 关闭）
   |
Step 6  正式跑 B 组（hint 开启，服务端特性关闭）
   |
Step 7  正式跑 C 组（hint 开启，服务端特性开启）
   |
Step 8  汇总指标、做合理性检查、写报告
```

如果服务端**没有**实现 `agent_hint` 特性，只能完成 A 组和 B 组，不能得出特性收益结论。
此时必须在报告中明确写“服务端未实现 agent_hint，本次仅为 session 调度基线”。

---

## 2. Step 0：预检

### 2.1 仓库与关键文件

```bash
test -f "$REPO/ais_bench/datasets/agent/build_agent_dataset.py" && echo OK_BUILD
test -f "$REPO/ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py" && echo OK_MODEL_CFG
test -f "$REPO/ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py" && echo OK_DATA_CFG
```

**Gate G0-a**：三个文件都存在。

### 2.2 客户端可用

```bash
PYTHONPATH="$REPO" python -c "import ais_bench; print(ais_bench.__file__)"
PYTHONPATH="$REPO" ais_bench --help >/dev/null && echo OK_CLI
```

**Gate G0-b**：`ais_bench.__file__` 位于 `$REPO`；`OK_CLI` 输出成功。

### 2.3 服务端可用

```bash
curl -sf "$SERVER_BASE/v1/models" | head -c 300; echo
# 若 C 组使用独立服务：
curl -sf "$SERVER_HINT/v1/models" | head -c 300; echo
```

**Gate G0-c**：至少 `SERVER_BASE` 返回 200，且模型名与 `$MODEL` 一致。

### 2.4 记录服务端能力（必须写入报告）

| 项目 | 值 |
|---|---|
| 服务端地址 | `$SERVER_BASE` / `$SERVER_HINT` |
| 服务端版本 | 由服务端提供方给出 |
| 启动参数/镜像 | 由服务端提供方给出 |
| 是否实现 `agent_hint` | 是 / 否 |
| 开启方式 | 例如环境变量/配置项 |
| 是否有 `/metrics` | 是 / 否 |

如果无法确认服务端是否实现 `agent_hint`，按“未实现”处理，不能把 B 组当作特性收益。

### 2.5 数据与设备

```bash
test -d "$DATASET_DIR" && echo OK_DATASET_DIR
# 或已有 JSONL：
test -f "$DATASET_JSONL" && echo OK_DATASET_JSONL
```

**Gate G0-d**：`DATASET_DIR` 或 `DATASET_JSONL` 至少有一个存在；客户端侧 CPU/内存充足；
若客户端和服务端共用设备，确认没有其他压测任务在跑。

---

## 3. Step 1：校验原始数据集目录

### 3.1 目录契约

`DATASET_DIR` 下每个请求一个 JSON 文件，文件名必须匹配：

```text
G<任意>_S<会话号>_R<轮次号>[_任意后缀].json
```

示例：

```text
model_io/
├── G0001_S001_R001_chat.json
├── G0001_S001_R002_chat.json
├── G0001_S002_R001_chat.json
└── G0002_S001_R001_chat.json
```

- `S` 决定 session，`R` 决定会话内轮次。
- 脚本按 `_S`、`_R` 排序；`G` 不参与排序。
- 文件名中的 `management` 不决定控制面，是否控制面只看 `agent_hint.context_management.manage_request`。

### 3.2 必需字段

| 字段 | 必需性 | 说明 |
|---|---|---|
| `request.agent_hint.session_id` | 必需 | 会话标识 |
| `request.agent_hint.parent_session_id` | **必需** | 真正的调度分桶 key；为空会全部退化到 `"default"`，所有会话串行 |
| `request.messages[].content` | 非 SWE 必需 | 普通模式会拼接每条消息的 `content` |
| `request.messages` / `request.tools` | `--swe-format` 使用 | OpenAI chat 格式 |
| `usage.total_tokens` / `usage.prompt_tokens` | 强烈建议 | 用于计算 `max_tokens = total - prompt`；缺失会回退为 1 |
| `request.agent_hint.context_management.manage_request` | 可选 | `true` 为控制面请求 |
| `request.agent_hint.context_management.edits` | 可选 | `offload` / `prefetch` / `evict` |

### 3.3 目录统计

```bash
python - "$DATASET_DIR" <<'EOF'
import json, os, sys, collections
d = sys.argv[1]
files = sorted(f for f in os.listdir(d) if f.startswith("G") and f.endswith(".json"))
parents, sessions, stages = set(), set(), collections.Counter()
for f in files:
    r = json.load(open(os.path.join(d, f)))
    ah = (r.get("request") or {}).get("agent_hint") or {}
    parents.add(ah.get("parent_session_id"))
    sessions.add(ah.get("session_id"))
    stages[ah.get("stage")] += 1
print("files", len(files))
print("parents", len(parents), "sessions", len(sessions))
print("empty_parent", sum(1 for p in parents if not p))
print("stages", stages)
EOF
```

**Gate G1**：`files > 0`、`parents > 1`、`empty_parent == 0`。

> 如果 `parents == 1`，本次压测实际上只能串行，不能用来评估并发能力。

---

## 4. Step 2：构造数据集并验收

### 4.1 先预览

```bash
python "$REPO/ais_bench/datasets/agent/build_agent_dataset.py" \
  -t Jiuwen \
  -s "$DATASET_DIR" \
  -o "$OUT_ROOT/data/${RUN_ID}_preview.jsonl" \
  --evict --add-pause 0.5 \
  --preview --show-agent-hint
```

预期 stdout 包含：

- `[Jiuwen] Reading N files from ...`
- `[Jiuwen] group_by_parent_sessions_evict: ... prefetch=.. offload=.. evict=.. evict+end=..`
- `========== ... Preview (...) ==========`
- `----- Summary: X data-plane + Y manage-request = Z total -----`

**Gate G2-a**：记录数、ParentGroup/SessionGroup 结构符合预期；每个会话首条是 `stage=start`；
使用 `--evict` 时存在 `stage=end`。

### 4.2 正式生成

```bash
python "$REPO/ais_bench/datasets/agent/build_agent_dataset.py" \
  -t Jiuwen \
  -s "$DATASET_DIR" \
  -o "$OUT_ROOT/data/${RUN_ID}.jsonl" \
  --evict --add-pause 0.5
```

常用可选参数：

| 参数 | 作用 |
|---|---|
| `-c N` | 每个 session 只保留最后 N 条 |
| `-n N` | 只保留前 N 个 parent group |
| `--copy N` | 每个 parent group 复制 N 份（新随机后缀） |
| `--add-pause P` | 给比例 P 的会话插入 offload+prefetch |
| `--evict` | 生成 evict/offload/end 组合 |
| `--max-token-len N` | 丢弃超长 question（需 `--tokenizer`） |
| `--swe-format` | 输出 `messages/tools/tool_choice` 格式 |

如果输入已经是 JSONL：

```bash
python "$REPO/ais_bench/datasets/agent/build_agent_dataset.py" \
  -t default \
  -s "$DATASET_JSONL" \
  -o "$OUT_ROOT/data/${RUN_ID}.jsonl" \
  --tokenizer "$TOKENIZER"
```

### 4.3 验收构造结果

```bash
python - "$OUT_ROOT/data/${RUN_ID}.jsonl" <<'EOF'
import json, sys, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
manage = collections.Counter(
    bool(((r.get("agent_hint") or {}).get("context_management") or {}).get("manage_request"))
    for r in rows
)
print("rows", len(rows))
print("columns", sorted(rows[0].keys()))
print("data_plane", manage[False], "manage", manage[True])
print("parents", len({r["agent_hint"]["parent_session_id"] for r in rows}))
print("sessions", len({r["agent_hint"]["session_id"] for r in rows}))
print("start", sum(1 for r in rows if r["agent_hint"].get("stage") == "start"))
print("end", sum(1 for r in rows if r["agent_hint"].get("stage") == "end"))
print("empty_parent", sum(1 for r in rows if not r["agent_hint"].get("parent_session_id")))
print("max_tokens_min", min((r.get("max_tokens") or 0) for r in rows))
EOF
```

**Gate G2-b**：

- `rows > 0`，`data_plane > 0`；
- `parents > 1`，`empty_parent == 0`；
- 每个会话有 `start`；
- 使用 `--evict` 时 `end > 0`；
- 数据面记录有非 0 的 `max_tokens`（否则会按 1 生成）。

### 4.4 缓存隔离（正式实验必做）

前缀缓存会让“同一份数据跑第二次”显著变快，从而污染对比。正式测量必须满足以下任一条件：

1. **每次 run 使用内容不同但分布一致的数据**（推荐）：合成数据时在 system prompt 前缀加唯一 nonce；
   真实数据无法改内容时，使用方案 2 或 3。
2. **每次 run 前重启服务端**（由服务端同学配合）。
3. **每次 run 使用独立服务实例**。

如果以上都做不到，必须在报告中写“缓存未隔离，结果仅能横向参考”。

推荐的文件命名（每个正式 run 一份）：

```text
$OUT_ROOT/data/
├── ${RUN_ID}_A_c4_r1.jsonl
├── ${RUN_ID}_A_c8_r1.jsonl
├── ${RUN_ID}_B_c8_r1.jsonl
├── ${RUN_ID}_C_c8_r1.jsonl
└── ${RUN_ID}_warmup.jsonl
```

采用方案 1 时，Step 2 的 `-o` 要改成对应的 `${RUN_TAG}.jsonl`，**每个正式 run 各构造一次**。

---

## 5. Step 3：配置 dataset 与 model

### 5.1 dataset 配置

文件：`$REPO/ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py`

```python
agent_reader_cfg = dict(
    input_columns=['question', 'max_out_len'],
    output_column=None,
)
```

`path` 指向本次构造的 JSONL。**推荐改成环境变量注入**，避免每次手改：

```python
import os

agent_demo_datasets = [
    dict(
        abbr='session-agent',
        type=CustomDataset,
        path=os.environ["AGENT_DATASET"],
        reader_cfg=agent_reader_cfg,
        infer_cfg=agent_infer_cfg,
        eval_cfg=agent_eval_cfg,
    )
]
```

说明：

- `path` 写绝对路径最稳妥；相对路径相对仓库根目录解析。
- `agent_hint` 不需要写进 `input_columns`，会自动透传。
- 数据里的 `max_tokens` 会被改名成 `max_out_len`；只有 `max_out_len` 在 `input_columns`
  时才覆盖模型配置的默认长度。

### 5.2 model 配置

文件：`$REPO/ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py`

推荐改成环境变量注入：

```python
import os

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-api-general-stream-session",
        path=os.environ.get("AGENT_WEIGHT_PATH", "/abs/path/to/model"),
        model=os.environ.get("AGENT_MODEL", "my-model"),
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=int(os.environ.get("AGENT_CONCURRENCY", "4")),
        pass_agent_hint=os.environ.get("AGENT_PASS_HINT", "0") == "1",
        batch_size=int(os.environ.get("AGENT_CONCURRENCY", "4")),
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="",
        host_ip=os.environ.get("AGENT_HOST", "127.0.0.1"),
        host_port=int(os.environ.get("AGENT_PORT", "8000")),
        url="",
        max_out_len=256,
        trust_remote_code=False,
        generation_kwargs=dict(temperature=0, ignore_eos=False),
    )
]
```

关键参数：

| 参数 | 必须 | 说明 |
|---|---|---|
| `session_aware` | 是 | 开启会话感知调度 |
| `session_concurrency` | 是 | 同时活跃会话数上限 |
| `pass_agent_hint` | 是 | `False` 不发 `agent_hint`；`True` 合并进请求体 |
| `batch_size` | 建议 | session 模式下是队列容量，建议等于 `session_concurrency` |
| `stream` | 是 | 否则没有 TTFT |
| `use_chat_api` | 是 | 打 `/v1/chat/completions` |
| `host_ip` / `host_port` | 是 | 指向服务端 |
| `model` | 是 | 服务端实际模型名 |
| `max_out_len` | 建议 | 数据未提供时的兜底长度 |

### 5.3 配置校验（不发请求）

```bash
cd "$REPO"
PYTHONPATH="$REPO" ais_bench \
  --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen \
  --mode perf --search
```

**Gate G3-a**：输出两个配置文件的绝对路径都在 `$REPO` 下。

再做一次配置自检，确认环境变量真的被写进了配置对象：

```bash
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_A.jsonl" \
AGENT_PASS_HINT=0 AGENT_CONCURRENCY=8 \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" AGENT_MODEL="$MODEL" \
PYTHONPATH="$REPO" python - <<'EOF'
from mmengine.config import Config
m = Config.fromfile(
    "ais_bench/benchmark/configs/models/vllm_api/vllm_api_general_stream_session.py"
).models[0]
d = Config.fromfile(
    "ais_bench/benchmark/configs/datasets/custom/custom_agent_session_gen.py"
).agent_demo_datasets[0]
print("dataset", d["path"])
print("model", m["model"], "addr", m["host_ip"], m["host_port"])
print("session_aware", m["session_aware"], "concurrency", m["session_concurrency"])
print("pass_agent_hint", m["pass_agent_hint"], "batch_size", m["batch_size"])
EOF
```

预期：`session_aware=True`、`concurrency=8`、`pass_agent_hint=False`、地址和 `dataset` 都是本次实际值。

**Gate G3-b**：自检输出与预期一致。若不一致，说明配置没有改成环境变量注入，或环境变量没传进去。

### 5.4 三组实验配置

| 组 | 服务端 | `AGENT_HOST/AGENT_PORT` | `AGENT_PASS_HINT` | 服务端特性 | 目的 |
|---|---|---|---|---|---|
| A | 基线 | `SERVER_BASE` | `0` | 关闭 | 无 hint 基线 |
| B | 基线 | `SERVER_BASE` | `1` | 关闭 | 隔离字段透传开销 |
| C | 特性 | `SERVER_HINT` | `1` | 开启 | 特性收益 |

如果只有一个服务端，B 和 C 之间需要服务端重启切换；切换前后都要重新做健康检查。

---

## 6. Step 4：小样本冒烟

冒烟只验证链路，不作为性能结论。

```bash
cd "$REPO"
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" \
AGENT_MODEL="$MODEL" AGENT_CONCURRENCY=4 AGENT_PASS_HINT=0 \
PYTHONPATH="$REPO" \
ais_bench \
  --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen \
  --debug -m perf \
  --num-prompts 20 \
  --work-dir "$OUT_ROOT/runs/${RUN_ID}_smoke"
```

预期日志：

```text
[session-aware] enabled, session_concurrency=4, pass_agent_hint=False, ...
[session-aware] create a task for parent session ...
[session-aware] session ... acquired exec slot ...
[session-aware] session ... START request stage=... data_id=...
[session-aware] session ... END   request stage=... data_id=...
```

当 `AGENT_PASS_HINT=1` 时，还应看到：

```text
[agent_hint][send] data_id=... session_id=... agent_hint={...}
```

**Gate G4**：

- `Failed Requests = 0`；
- 出现 `session-aware` 创建任务和 START/END 日志；
- `Max Concurrency > 1`（数据里确实有多个 parent session）；
- 冒烟结果中的 `Total Requests` 等于被选中的数据面请求数。

> 冒烟使用 `--num-prompts` 会截断部分会话，出现
> `last meaningful stage is 'stage-N', expected 'end'` 是正常的；正式测量不要用它。

---

## 7. Step 5-7：正式压测

### 7.1 预热

正式测量前先跑一份**与测量数据不重叠**的数据做预热（不同 nonce / 不同 session）：

```bash
# 前提：WARMUP_DATASET_DIR 与 DATASET_DIR 内容不同（不同 nonce / 不同批次）
python "$REPO/ais_bench/datasets/agent/build_agent_dataset.py" \
  -t Jiuwen -s "$WARMUP_DATASET_DIR" \
  -o "$OUT_ROOT/data/${RUN_ID}_warmup.jsonl" \
  -n 2 --evict --add-pause 0.5

AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_warmup.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" \
AGENT_MODEL="$MODEL" AGENT_CONCURRENCY=4 AGENT_PASS_HINT=0 \
PYTHONPATH="$REPO" \
ais_bench --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen -m perf \
  --num-prompts 16 --work-dir "$OUT_ROOT/runs/${RUN_ID}_warmup"
```

预热不计入结果，只用于触发内核编译、图捕获、服务端缓存初始化。
如果没有第二份内容不同的数据，**不要用测量数据预热**，改用“每次 run 重启服务端”。

### 7.2 采集服务端指标（可选但推荐）

```bash
RUN_TAG="${RUN_ID}_A_c8_r1"
curl -s "$SERVER_BASE/metrics" > "$OUT_ROOT/metrics/${RUN_TAG}_before.prom" || true
# 跑完后
curl -s "$SERVER_BASE/metrics" > "$OUT_ROOT/metrics/${RUN_TAG}_after.prom" || true
```

至少记录：prefix cache 命中、KV 使用率、offload/evict 次数、preempt、运行/等待请求数。
指标名以服务端实际暴露为准。

### 7.3 单组运行命令

以 A 组、`session_concurrency=8`、第 1 次重复为例：

```bash
cd "$REPO"
RUN_TAG="${RUN_ID}_A_c8_r1"
AGENT_DATASET="$OUT_ROOT/data/${RUN_TAG}.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" \
AGENT_MODEL="$MODEL" AGENT_CONCURRENCY=8 AGENT_PASS_HINT=0 \
PYTHONPATH="$REPO" \
ais_bench \
  --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen \
  -m perf \
  --work-dir "$OUT_ROOT/runs/${RUN_TAG}"
```

B 组只改 `RUN_TAG`、数据文件和 `AGENT_PASS_HINT=1`；C 组在此基础上把
`AGENT_HOST="$HINT_HOST"`、`AGENT_PORT="$HINT_PORT"`。

> 如果没有为每个 run 单独构造数据，就把 `AGENT_DATASET` 改成
> `"$OUT_ROOT/data/${RUN_ID}.jsonl"`，并在报告中标注“缓存未隔离”。

### 7.4 重复与顺序

- 每个（组 × 并发）至少跑 **3 次**，报告中取中位数，同时保留 min/max。
- 不同组之间交替执行（A→B→C→A→B→C），减少服务端状态漂移。
- 每次 run 使用唯一 `RUN_ID`，并记录该 run 的数据文件、`AGENT_PASS_HINT`、并发、服务端地址。

### 7.5 建议矩阵

| 组 | `session_concurrency` | 重复 | 说明 |
|---|---|---|---|
| A | 4 / 8 / 16 | 3 | hint 关闭 |
| B | 4 / 8 / 16 | 3 | hint 开启、服务端关闭 |
| C | 4 / 8 / 16 | 3 | hint 开启、服务端开启 |

并发上限不要超过数据中的 parent session 数，否则不会真正打满。

**Gate G5**（每组每次）：

- `Failed Requests = 0`；
- `Total Requests` 等于该数据集数据面记录数；
- `Concurrency` 接近 `session_concurrency`；
- `_details.jsonl` 行数与 `Total Requests` 一致；
- 日志中没有持续重试、超时、OOM。

---

## 8. Step 8：结果读取与汇总

### 8.1 目录结构

```text
<work_dir>/<timestamp>/
├── configs/                                  # 本次配置快照
└── performances/<model_abbr>/
    ├── session-agent_details.jsonl           # 每请求明细
    ├── session-agent_failed.jsonl            # 失败请求
    ├── session-agent.json                    # 性能汇总
    └── session-agent.csv                     # 性能表
```

### 8.2 指标

| 指标 | 含义 |
|---|---|
| Total / Success / Failed | 请求数；控制面请求按规则剔除 |
| Request Throughput | req/s |
| Total Token Throughput | 输入+输出 token/s |
| TTFT | 首 token 延迟 |
| TPOT | 每输出 token 耗时 |
| E2EL | 端到端延迟 |
| Concurrency / Max Concurrency | 实际并发 / 峰值并发 |

### 8.3 合理性检查

```bash
RUN_TAG="${RUN_ID}_A_c8_r1"
D=$(ls -d "$OUT_ROOT/runs/${RUN_TAG}"/*/ | tail -1)
f=$(find "$D/performances" -name '*_details.jsonl')
wc -l "$f"
python - "$f" <<'EOF'
import json, sys, collections
rows = [json.loads(l) for l in open(sys.argv[1])]
print("details", len(rows))
print("success", collections.Counter(r.get("success") for r in rows))
print("has_extra_perf_data", sum(1 for r in rows if r.get("extra_perf_data")))
EOF
```

**Gate G6**：

- `details` 行数 = `Total Requests`；
- `success` 全为 `True`；
- 控制面请求在 `AGENT_PASS_HINT=0` 时通常不出现在 details（被跳过），
  用构造阶段的 `data_plane` / `manage` 计数核对即可；
- `Total Requests` 等于数据集的 `data_plane` 数。

### 8.4 生成汇总表

使用附录 D 的脚本：

```bash
python "$OUT_ROOT/tools/summarize.py" "$OUT_ROOT/runs" > "$OUT_ROOT/summary.csv"
column -s, -t < "$OUT_ROOT/summary.csv"
```

汇总表至少包含：组别、并发、重复序号、请求吞吐、Token 吞吐、TTFT avg/P90、TPOT avg/P90、
E2EL、Failed、缓存状态。

---

## 9. 对照实验结论怎么读

| 对比 | 能说明什么 | 不能说明什么 |
|---|---|---|
| A vs B | `agent_hint` 字段透传本身的客户端/网络开销 | 服务端特性收益 |
| B vs C | 服务端 `agent_hint` 实现带来的收益 | 客户端字段开销 |
| A vs C | 端到端收益（特性 + 字段 + 调度） | 收益来源拆解 |

预期现象（如果特性真正生效）：

- C 组相对 B 组，TTFT 或 prefill 吞吐改善，KV 复用/offload 指标有变化；
- 上下文越长、会话轮次越多，收益越明显；
- 并发越高，收益可能被调度开销稀释。

如果 A/B/C 几乎一样，优先怀疑：

1. 服务端没有真正开启特性；
2. 数据里没有可复用的前缀/没有 offload/evict；
3. 跨 run 前缀缓存污染；
4. 并发远低于 `session_concurrency`。

---

## 10. 验收清单（一页纸）

- [ ] G0：仓库文件齐全、CLI 加载本仓库、服务端 `/v1/models` 可用
- [ ] G0：服务端是否实现 `agent_hint` 已确认并记录
- [ ] G1：原始目录文件数、parent/session 数、`parent_session_id` 非空
- [ ] G2：数据集构造成功，data-plane/manage 计数符合预期
- [ ] G2：每会话 `stage=start`，使用 `--evict` 时存在 `stage=end`
- [ ] G2：正式 run 已做缓存隔离
- [ ] G3：`--search` 指向 `$REPO` 下的配置
- [ ] G4：冒烟 `Failed=0`，日志出现 session START/END
- [ ] G5：正式 run `Failed=0`，`Total=数据面数`，并发接近配置值
- [ ] G6：`details` 行数匹配，汇总表生成
- [ ] 报告记录环境、数据、矩阵、缓存状态、结论边界

---

## 11. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 代码改动不生效 / 找不到新参数 | CLI 加载了镜像内安装的副本 | 设置 `PYTHONPATH="$REPO"`，确认 `ais_bench.__file__` |
| `Dataset path ... is not exist` | 配置 `path` 没改，或相对路径解析错误 | 改成绝对路径，或设置 `AGENT_DATASET` |
| `ValueError: Jiuwen expects a directory` | `-s` 给了文件 | Jiuwen 传目录；default 才传文件 |
| `default expects a file` | `-s` 给了目录 | 同上 |
| `int()` 转换报错 | 文件名不符合 `G*_S<num>_R<num>` | 重命名输入文件 |
| 所有请求都在同一个 session | `parent_session_id` 全为空 | 补 `parent_session_id` |
| 并发始终是 1 | `session_concurrency=1` 或数据只有一个 parent | 提高并发，确认数据有多个 parent |
| 冒烟出现 `expected 'end'` 警告 | `--num-prompts` 截断了会话 | 正常；正式测量不要用 `--num-prompts` |
| `Not found timestamps ... use_timestamp is True` | 数据无 `timestamp` | 关掉 `use_timestamp` |
| 日志出现 `skip manage_request since pass_agent_hint=False` | 模型配置没开 `pass_agent_hint` | 按实验组设置 `AGENT_PASS_HINT` |
| 指标与普通压测几乎一样 | 服务端未实现/未开启 `agent_hint` | 检查服务端；做 A/B/C 对照并记录能力 |
| 生成长度异常短 | 输入缺 `usage`，`max_tokens` 回退为 1 | 补 `usage.total_tokens` / `prompt_tokens` |
| 多进程起不来 | session 模式强制单进程 | 不要提高 worker 数，使用 `--debug` 冒烟 |
| 服务启动 OOM / KV 不足 | `max-model-len` 过大或显存/内存不足 | 由服务端调小 `max-model-len` 或提高显存利用率 |
| 引擎初始化报算子/版本错误 | 模型与镜像不匹配 | 换匹配的模型或服务端镜像，记录版本 |
| 驱动/CANN 包签名校验失败 | 容器 CANN 与宿主驱动不匹配 | 使用匹配的镜像，不要改宿主安全配置 |
| 同一份数据第二次跑明显变快 | 前缀缓存跨 run 命中 | 用唯一 nonce 数据 / 重启服务 / 独立实例 |
| `use_timestamp`、`timestamp` 相关报错 | 配置与数据不一致 | 确认 `use_timestamp=False` |

---

## 12. 运行记录模板（每个 run 必填）

```yaml
run_id:
start_time:
end_time:
git_commit:
repo:
dataset:
dataset_sha256:
dataset_records:
data_plane_records:
manage_records:
server_url:
server_version:
server_flags:
agent_hint_enabled: true/false
client_pass_agent_hint: true/false
session_concurrency:
model:
max_out_len:
cache_state: cold/warm/isolated
result_dir:
notes:
```

---

## 附录 A：最小可复制模板

```bash
set -euo pipefail

REPO=/abs/path/to/benchmark
DATASET_DIR=/abs/path/to/model_io
OUT_ROOT=/abs/path/to/agent_hint_perf
RUN_ID=$(date +%Y%m%d_%H%M%S)
MODEL=my-model
BASE_HOST=127.0.0.1 BASE_PORT=8000
HINT_HOST=127.0.0.1 HINT_PORT=8001
SERVER_BASE=http://${BASE_HOST}:${BASE_PORT}
SERVER_HINT=http://${HINT_HOST}:${HINT_PORT}

mkdir -p "$OUT_ROOT"/{data,logs,runs,metrics,configs}
cd "$REPO"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"

# 1) 构造（A 组；B/C 用同样命令生成 _B/_C，内容 nonce 要不同）
python ais_bench/datasets/agent/build_agent_dataset.py \
  -t Jiuwen -s "$DATASET_DIR" \
  -o "$OUT_ROOT/data/${RUN_ID}_A.jsonl" \
  --evict --add-pause 0.5

# 2) 冒烟
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_A.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" AGENT_MODEL="$MODEL" \
AGENT_CONCURRENCY=4 AGENT_PASS_HINT=0 \
ais_bench --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen --debug -m perf \
  --num-prompts 20 --work-dir "$OUT_ROOT/runs/${RUN_ID}_smoke"

# 3) A 组
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_A.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" AGENT_MODEL="$MODEL" \
AGENT_CONCURRENCY=8 AGENT_PASS_HINT=0 \
ais_bench --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen -m perf \
  --work-dir "$OUT_ROOT/runs/${RUN_ID}_A_c8_r1"

# 4) B 组（hint 开启，服务端特性关闭）
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_B.jsonl" \
AGENT_HOST="$BASE_HOST" AGENT_PORT="$BASE_PORT" AGENT_MODEL="$MODEL" \
AGENT_CONCURRENCY=8 AGENT_PASS_HINT=1 \
ais_bench --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen -m perf \
  --work-dir "$OUT_ROOT/runs/${RUN_ID}_B_c8_r1"

# 5) C 组（hint 开启，服务端特性开启）
AGENT_DATASET="$OUT_ROOT/data/${RUN_ID}_C.jsonl" \
AGENT_HOST="$HINT_HOST" AGENT_PORT="$HINT_PORT" AGENT_MODEL="$MODEL" \
AGENT_CONCURRENCY=8 AGENT_PASS_HINT=1 \
ais_bench --models vllm_api_general_stream_session \
  --datasets custom_agent_session_gen -m perf \
  --work-dir "$OUT_ROOT/runs/${RUN_ID}_C_c8_r1"
```

---

## 附录 B：配置片段

### B.1 dataset

```python
import os
from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset

agent_reader_cfg = dict(input_columns=['question', 'max_out_len'], output_column=None)
agent_infer_cfg = dict(
    prompt_template=dict(type=PromptTemplate, template='{question}'),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)
agent_eval_cfg = dict(evaluator=None, pred_role='BOT')

agent_demo_datasets = [
    dict(
        abbr='session-agent',
        type=CustomDataset,
        path=os.environ["AGENT_DATASET"],
        reader_cfg=agent_reader_cfg,
        infer_cfg=agent_infer_cfg,
        eval_cfg=agent_eval_cfg,
    )
]
```

### B.2 model

```python
import os
from ais_bench.benchmark.models import VLLMCustomAPI

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-api-general-stream-session",
        path=os.environ.get("AGENT_WEIGHT_PATH", "/abs/path/to/model"),
        model=os.environ.get("AGENT_MODEL", "my-model"),
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=int(os.environ.get("AGENT_CONCURRENCY", "4")),
        pass_agent_hint=os.environ.get("AGENT_PASS_HINT", "0") == "1",
        batch_size=int(os.environ.get("AGENT_CONCURRENCY", "4")),
        request_rate=0,
        use_timestamp=False,
        retry=2,
        api_key="",
        host_ip=os.environ.get("AGENT_HOST", "127.0.0.1"),
        host_port=int(os.environ.get("AGENT_PORT", "8000")),
        url="",
        max_out_len=256,
        trust_remote_code=False,
        generation_kwargs=dict(temperature=0, ignore_eos=False),
    )
]
```

---

## 附录 C：数据集单条示例

```json
{
  "question": "",
  "answer": "",
  "tool_choice": "auto",
  "messages": [
    {"role": "system", "content": "You are a coding agent..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."},
    {"role": "tool", "content": "...", "tool_call_id": "..."}
  ],
  "tools": [
    {"type": "function", "function": {"name": "read_pdf", "description": "...", "parameters": {}}}
  ],
  "max_tokens": 256,
  "agent_hint": {
    "session_id": "sess-A",
    "parent_session_id": "parent-1",
    "cache_control": {},
    "context_management": {"manage_request": false, "edits": []},
    "stage": "stage-3"
  }
}
```

---

## 附录 D：结果汇总脚本

保存为 `$OUT_ROOT/tools/summarize.py`：

```python
#!/usr/bin/env python3
"""汇总 AISBench perf 结果目录，输出 CSV 到 stdout。"""
import csv
import glob
import json
import os
import sys

root = sys.argv[1] if len(sys.argv) > 1 else "."

rows = []
pattern = os.path.join(root, "**", "performances", "*", "session-agent.json")
for j in glob.glob(pattern, recursive=True):
    with open(j) as f:
        d = json.load(f)
    # 路径为 <root>/<run_tag>/<timestamp>/performances/<model>/session-agent.json
    run = os.path.relpath(
        os.path.dirname(os.path.dirname(os.path.dirname(j))), root
    )
    row = {"run": run}
    for k in [
        "Benchmark Duration", "Total Requests", "Failed Requests", "Success Requests",
        "Concurrency", "Max Concurrency", "Request Throughput",
        "Total Input Tokens", "Total Generated Tokens", "Total Token Throughput",
    ]:
        row[k] = (d.get(k) or {}).get("total")

    c = os.path.join(os.path.dirname(j), "session-agent.csv")
    if os.path.exists(c):
        with open(c) as f:
            m = {r["Performance Parameters"]: r for r in csv.DictReader(f)}
        for k in ["TTFT", "TPOT", "E2EL"]:
            if k in m:
                row[k + "_avg"] = m[k]["Average"]
                row[k + "_median"] = m[k]["Median"]
                row[k + "_p90"] = m[k]["P90"]
                row[k + "_p99"] = m[k]["P99"]
    rows.append(row)

if not rows:
    sys.exit("no session-agent.json found")

fields = list(rows[0].keys())
for r in rows[1:]:
    for k in r:
        if k not in fields:
            fields.append(k)
w = csv.DictWriter(sys.stdout, fieldnames=fields)
w.writeheader()
w.writerows(rows)
```

---

## 附录 E：术语

| 术语 | 含义 |
|---|---|
| 数据面请求 | `manage_request=false`，真正产生输出 token 的请求 |
| 控制面请求 | `manage_request=true`，offload/prefetch/evict/end 等信号 |
| parent session | 调度分桶 key，相同值串行，不同值可并行 |
| session | 会话标识，用于 stage 顺序与日志 |
| A/B/C 组 | 基线 / 字段透传 / 特性开启 |
| 冷缓存 | 服务端没有本次测量数据的前缀缓存 |
| 热缓存 | 服务端已缓存相同前缀（跨 run 污染） |
