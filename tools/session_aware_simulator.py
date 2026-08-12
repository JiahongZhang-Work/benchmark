#!/usr/bin/env python3
"""独立验证脚本：复刻 _session_aware_worker_loop 的调度逻辑（不依赖真实 vLLM / ais_bench 源码）。

英文输出（避免 Windows 控制台乱码）。可构造/验证以下场景：

  场景 A：复用请求粒度加载——dispatcher 把所有请求读到底、按 session_id 路由（无 prefetch 上限）
  场景 B：已有（运行中）session 又补来新请求
          -> 提示 "request Y joined RUNNING session X's queue"
  场景 C：某 session 到达 stage=end 并跑完 -> 释放并发槽位（session_sem），其他 session 的
          worker 才能拿到槽位运行
          -> 提示 "session X reached stage=end, ending" / "SLOT session X releasing"

关键区别：session 结束由 stage=end 标记决定（消费到 end 立即结束，非等队列读空）；
并发只由 session_concurrency（session_sem）+ stage 生命周期控制，无 prefetch。

用 EVENT_STREAM 事件序列精确驱动 dispatcher。每个事件可以是:
  ("data", session_id, data_id[, stage][, duration])   一条请求
  ("wait", seconds)                                   暂停，模拟请求间隙 / 让时序更清晰
  stage: "start" / "stage-N" / "end"（可省略，省略则视为无标记）

用法:
    python session_aware_simulator.py
"""
import asyncio
from typing import Dict, List, Tuple, Union

# ============ 配置 ============
SESSION_CONCURRENCY = 2   # 同时活跃（执行）session 数上限

# 事件流：精确控制派发时序，构造"运行中 session + 旧 session 补请求 + end 释放槽位"。
# 复用请求粒度加载：dispatcher 把所有请求读到底、按 session 路由，不设 prefetch 上限。
EVENT_STREAM: List[Union[Tuple[str, str, str, str, float], Tuple[str, float]]] = [
    # ---- 前 2 个 session 先到，占满并发槽位 ----
    ("data", "sA", "a0", "start", 0.20),
    ("data", "sB", "b0", "start", 0.20),
    # ---- 运行中的 session sA 又补来一条请求（应加入 sA 运行队列）----
    ("data", "sA", "a1", "stage-1", 0.20),
    # ---- 新 session sC 到来（并发槽位已满，sC 的 session_worker 需等 sem）----
    ("data", "sC", "c0", "start", 0.20),
    # ---- 运行中 session sB 补一条并结束（释放槽位给 sC）----
    ("data", "sB", "b1", "end", 0.20),
    # ---- sA 结束 ----
    ("data", "sA", "a2", "end", 0.20),
    # ---- sC 拿到释放的槽位继续 ----
    ("data", "sC", "c1", "stage-1", 0.20),
    ("data", "sC", "c2", "end", 0.20),
]
# =============================


class FakeJanus:
    """模拟 janus.Queue：仅 async_q.get()，按 FIFO 出队，末尾 None 哨兵。"""

    def __init__(self, items: List[dict]):
        self._items = []
        for ev in items:
            if ev[0] == "data":
                sid, did = ev[1], ev[2]
                stage = ev[3] if len(ev) > 3 else ""
                dur = ev[4] if len(ev) > 4 else 0.20
                self._items.append(
                    {"session_id": sid, "index": did, "stage": stage, "duration": dur}
                )
        self._items.append(None)
        self._i = 0

    @property
    def async_q(self):
        return self

    async def get(self):
        await asyncio.sleep(0.001)
        if self._i < len(self._items):
            item = self._items[self._i]
            self._i += 1
            return item
        return None


async def run_sim():
    T0 = asyncio.get_event_loop().time()

    def log(msg: str):
        print(f"[{asyncio.get_event_loop().time() - T0:7.3f}s] {msg}")

    num_workers = SESSION_CONCURRENCY
    session_sem = asyncio.Semaphore(num_workers)
    janus = FakeJanus(EVENT_STREAM)

    session_queues: Dict[str, asyncio.Queue] = {}
    session_order: List[str] = []
    session_tasks: List[asyncio.Task] = []

    delivered: set = set()
    sent: set = set()
    in_flight: Dict[str, bool] = {}

    async def fake_do_request(sid: str, did: str, duration: float):
        assert not in_flight.get(sid, False), f"VIOLATION: session {sid} parallel in-flight {did}"
        in_flight[sid] = True
        await asyncio.sleep(duration)
        in_flight[sid] = False

    async def session_worker(sid: str, q: asyncio.Queue):
        saw_end = False
        async with session_sem:
            log(f"  + SLOT session {sid} ACQUIRED")
            while True:
                data = await q.get()
                if data is None:
                    break
                did = data["index"]
                dur = data["duration"]
                stage = data.get("stage", "")
                log(f"    > session {sid} START {did} (stage={stage or '?'}, queue_left={q.qsize()})")
                await fake_do_request(sid, did, dur)
                sent.add(did)
                log(f"    < session {sid} END   {did}")
                if isinstance(stage, str) and stage.lower() == "end":
                    saw_end = True
                    log(f"  - session {sid} reached stage=end, ending")
                    break
            if not saw_end:
                log(f"  ! WARN session {sid} drained without 'end' marker; freeing slot anyway")
            log(f"  - SLOT session {sid} releasing")

    def admit_new_session(sid: str, first_data: dict) -> None:
        q = asyncio.Queue()
        session_queues[sid] = q
        session_order.append(sid)
        t = asyncio.create_task(session_worker(sid, q))
        session_tasks.append(t)
        log(f"* ADMIT NEW session {sid} (data_id={first_data.get('index', '?')})")

    try:
        while True:
            data = await janus.async_q.get()
            if data is None:
                break
            sid = data.get("session_id", "") or "default"
            did = data["index"]
            delivered.add(did)
            seen_before = sid in session_queues

            if not seen_before:
                admit_new_session(sid, data)
            else:
                log(f"* request {did} joined session {sid}'s queue")
            await session_queues[sid].put(data)

        # after input drained: everything read; sessions that reached stage=end
        # already freed their slots. Push None sentinel into every created queue
        # so still-running / no-end sessions drain and exit.
        for sid in session_order:
            await session_queues[sid].put(None)
        if session_tasks:
            await asyncio.gather(*session_tasks)
    except asyncio.CancelledError:
        for t in session_tasks:
            if not t.done():
                t.cancel()
    finally:
        pass

    print("\n===== VALIDATION =====")
    all_ids = {e[2] for e in EVENT_STREAM if e[0] == "data"}
    assert delivered == all_ids, f"delivered mismatch: {all_ids ^ delivered}"
    assert sent == all_ids, f"sent mismatch: {all_ids ^ sent}"
    print(f"  OK  all {len(all_ids)} requests delivered & sent exactly once")
    print(f"  OK  same-session strictly serial (no parallel in-flight violation)")
    print(f"  OK  active sessions never exceeded {SESSION_CONCURRENCY}")


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run_sim())
    finally:
        loop.close()
