"""
用固定并发直接发送 aisbench 生成的数据集（session_single_turn.jsonl），
用于对比 aisbench 框架的额外消耗。

数据集结构：每个 session 2 条记录
  1. 推理请求（question 为长文本 prompt，agent_hint.manage_request=False）
  2. 驱逐请求（question 为空，agent_hint.manage_request=True，只 evict 不推理）

脚本行为：
  - 按 session_id 分组，维持数据集内顺序
  - 固定并发 = session 数上限；一个 session 持有 slot 直到推理 + 驱逐都完成
    （复刻 aisbench session_aware 语义：session_concurrency=5，session 内串行）
  - 驱逐请求发出但不计入 TTFT/统计

用法：
    # 默认（带 agent_hint，发驱逐请求）
    python tools/bench_dataset_direct.py

    # 不带 agent_hint（模拟 off：只发 20 条推理请求，不发驱逐）
    python tools/bench_dataset_direct.py --no-agent-hint

    # 指定并发 / 数据集路径
    python tools/bench_dataset_direct.py --concurrency 5 --dataset ais_bench/datasets/session_demo/session_single_turn.jsonl
"""

import asyncio
import aiohttp
import json
import argparse
import time
import sys
from collections import OrderedDict
from typing import Dict, List, Optional

VLLM_URL = "http://0.0.0.0:31025/v1/chat/completions"
TIMEOUT = 300

GEN_PARAMS = {
    "model": "qwen",
    "temperature": 0,
    "max_tokens": 256,
    "stream": True,
}


def load_dataset(path: str):
    """读取 jsonl，按 session_id 分组，保持数据集中首次出现顺序。"""
    sessions: "OrderedDict[str, List[dict]]" = OrderedDict()
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ah = rec.get("agent_hint") or {}
            sid = ah.get("session_id") or ""
            sessions.setdefault(sid, []).append(rec)
    return sessions


def split_session_records(recs: List[dict]):
    """拆出推理请求和控制面（evict）请求。"""
    infer_rec = None
    evict_recs = []
    for rec in recs:
        ah = rec.get("agent_hint") or {}
        cm = ah.get("context_management") or {}
        if cm.get("manage_request") is True:
            evict_recs.append(rec)
        else:
            infer_rec = rec
    return infer_rec, evict_recs


async def send_infer(
    session: aiohttp.ClientSession,
    prompt: str,
    agent_hint: Optional[dict],
    session_id: str,
) -> Optional[dict]:
    """发送一条推理请求，返回 TTFT 结果（流式，测首个 content chunk）。"""
    payload = {
        **GEN_PARAMS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if agent_hint is not None:
        payload["agent_hint"] = agent_hint

    start_time = time.perf_counter()
    ttft = None
    token_times = []
    output_token_count = 0

    try:
        async with session.post(
            VLLM_URL, json=payload, timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"[{session_id}] HTTP失败 status={resp.status}, {text[:200]}")
                return None

            async for line in resp.content:
                line = line.strip()
                if not line or not line.startswith(b"data: "):
                    continue
                data_str = line[6:].decode("utf-8")
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                if not delta.get("content"):
                    continue

                now = time.perf_counter()
                if ttft is None:
                    ttft = now - start_time
                token_times.append(now)
                output_token_count += 1

        if ttft is None or output_token_count <= 1:
            print(f"[{session_id}] 有效输出token不足，跳过统计")
            return None

        total_after_first = token_times[-1] - token_times[0]
        tpot = total_after_first / (output_token_count - 1)
        e2el = time.perf_counter() - start_time

        return {
            "session_id": session_id,
            "prompt_len": len(prompt),
            "ttft": round(ttft, 4),
            "tpot": round(tpot, 4),
            "e2el": round(e2el, 4),
            "output_tokens": output_token_count,
        }
    except Exception as e:
        print(f"[{session_id}] 请求异常: {str(e)}")
        return None


async def send_evict(
    session: aiohttp.ClientSession,
    agent_hint: dict,
    session_id: str,
):
    """发送驱逐请求（不推理），不计入统计。"""
    payload = {
        **GEN_PARAMS,
        "messages": [{"role": "user", "content": ""}],
        "stream": False,
        "agent_hint": agent_hint,
    }
    try:
        async with session.post(
            VLLM_URL, json=payload, timeout=aiohttp.ClientTimeout(total=TIMEOUT)
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                print(f"[{session_id}] Evict失败 status={resp.status}, {text[:200]}")
    except Exception as e:
        print(f"[{session_id}] Evict异常: {str(e)}")


async def run_direct(
    sessions: "OrderedDict[str, List[dict]]",
    concurrency: int,
    use_agent_hint: bool,
) -> List[dict]:
    """固定并发发送。一个 session 占一个 slot：推理成功后紧跟驱逐。"""
    session_ids = list(sessions.keys())
    results = []

    connector = aiohttp.TCPConnector(limit=concurrency + 4)
    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(connector=connector) as session:

        async def run_one(sid: str):
            recs = sessions[sid]
            infer_rec, evict_recs = split_session_records(recs)
            agent_hint_infer = infer_rec.get("agent_hint") if infer_rec else None
            prompt = infer_rec.get("question", "") if infer_rec else ""

            if not use_agent_hint:
                agent_hint_infer = None

            async with sem:
                # 推理请求（测量）
                res = await send_infer(session, prompt, agent_hint_infer, sid)
                if use_agent_hint:
                    # 驱逐请求（不计统计），紧跟推理之后
                    for evict_rec in evict_recs:
                        await send_evict(session, evict_rec["agent_hint"], sid)

                if res:
                    results.append(res)
                    print(
                        f"[{sid}] prompt_len:{res['prompt_len']} | "
                        f"TTFT:{res['ttft']:.4f}s | TPOT:{res['tpot']:.4f}s | "
                        f"E2EL:{res['e2el']:.4f}s"
                    )

        tasks = [asyncio.create_task(run_one(sid)) for sid in session_ids]
        await asyncio.gather(*tasks)

    return results


def print_stats(label: str, results: List[dict], duration: float):
    if not results:
        print(f"\n{label}: 无有效数据")
        return

    def pct(arr, p):
        s = sorted(arr)
        if not s:
            return 0.0
        idx = min(int(len(s) * p), len(s) - 1)
        return s[idx]

    ttfts = [r["ttft"] for r in results]
    tpots = [r["tpot"] for r in results]
    e2els = [r["e2el"] for r in results]
    plens = [r["prompt_len"] for r in results]
    n = len(results)

    print(f"\n{'=' * 62}")
    print(f"{label}（{n} 条推理请求，duration={duration:.1f}s）")
    print(f"{'=' * 62}")
    print(f"  TTFT  均值 {sum(ttfts)/n:.1f}ms | 中位 {pct(ttfts,0.5)*1000:.1f}ms | "
          f"P75 {pct(ttfts,0.75)*1000:.1f}ms | P90 {pct(ttfts,0.9)*1000:.1f}ms | "
          f"min {min(ttfts)*1000:.1f}ms | max {max(ttfts)*1000:.1f}ms")
    print(f"  TPOT  均值 {sum(tpots)/n:.1f}ms | 中位 {pct(tpots,0.5)*1000:.1f}ms")
    print(f"  E2EL  均值 {sum(e2els)/n:.1f}ms | 中位 {pct(e2els,0.5)*1000:.1f}ms")
    print(f"  prompt_len 均值 {sum(plens)/n:.0f} | 范围 {min(plens)}~{max(plens)}")
    print(f"  并发: {n / duration:.2f} req/s | 输出tokens/请求: upto {max(r['output_tokens'] for r in results)}")


async def main():
    global VLLM_URL

    parser = argparse.ArgumentParser(
        description="用固定并发直接发送 aisbench session 数据集，对比框架消耗"
    )
    parser.add_argument("--dataset", default="ais_bench/datasets/session_demo/session_single_turn.jsonl")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--no-agent-hint", action="store_true", default=False,
                        help="不带 agent_hint（只发推理请求，不发驱逐，模拟 off）")
    parser.add_argument("--url", default=VLLM_URL)
    args = parser.parse_args()

    VLLM_URL = args.url

    print(f"数据集: {args.dataset}")
    sessions = load_dataset(args.dataset)
    n_infer = sum(1 for recs in sessions.values() if split_session_records(recs)[0] is not None)
    n_evict = sum(len(split_session_records(recs)[1]) for recs in sessions.values())
    print(f"  sessions: {len(sessions)} | 推理请求: {n_infer} | 驱逐请求: {n_evict}")
    print(f"  并发: {args.concurrency} | agent_hint: {'不带' if args.no_agent_hint else '带'}")

    start = time.perf_counter()
    results = await run_direct(
        sessions, args.concurrency, use_agent_hint=not args.no_agent_hint
    )
    duration = time.perf_counter() - start

    label = "直接发送-带agent_hint" if not args.no_agent_hint else "直接发送-不带agent_hint"
    print_stats(label, results, duration)


if __name__ == "__main__":
    asyncio.run(main())
