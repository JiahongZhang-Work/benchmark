import asyncio
import time
import unittest
from unittest import mock

import janus

from ais_bench.benchmark.openicl.icl_inferencer.icl_base_api_inferencer import (
    BaseApiInferencer,
)


class DummyModel:
    max_out_len = 16
    is_api = False

    def parse_template(self, prompt, mode="gen"):
        return prompt


class ConcreteApiInferencer(BaseApiInferencer):
    async def do_request(self, data, token_bucket, session):
        return None


def make_record(session_id, stage, edits=None, manage_request=False):
    return {
        "index": 0,
        "prompt": "",
        "data_abbr": "test",
        "max_out_len": 1,
        "session_id": session_id,
        "stage": stage,
        "agent_hint": {
            "session_id": session_id,
            "parent_session_id": session_id,
            "context_management": {
                "manage_request": manage_request,
                "edits": edits or [],
            },
            "stage": stage,
        },
    }


def edit_type(record):
    edits = (
        ((record.get("agent_hint") or {}).get("context_management") or {}).get(
            "edits"
        )
        or []
    )
    return edits[0].get("type") if edits else None


class TestSessionAwareOffloadOrder(unittest.TestCase):
    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.model_abbr_from_cfg",
        return_value="mabbr",
    )
    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.build_model_from_cfg"
    )
    def test_prefetch_waits_for_offload_completion(self, mock_build, mock_abbr):
        mock_build.return_value = DummyModel()
        inferencer = ConcreteApiInferencer(
            model_cfg={},
            batch_size=4,
            mode="perf",
            session_aware=True,
            session_concurrency=1,
            pass_agent_hint=True,
        )
        events = []

        async def fake_do_request(data, token_bucket, session):
            kind = edit_type(data) or data.get("stage", "data")
            events.append((kind, "start", time.monotonic()))
            if kind == "offload":
                await asyncio.sleep(0.15)
            events.append((kind, "end", time.monotonic()))

        inferencer.do_request = fake_do_request
        records = [
            make_record("session-A", "start"),
            make_record(
                "session-A",
                "",
                edits=[{"type": "offload", "target": "session"}],
                manage_request=True,
            ),
            make_record(
                "session-A",
                "",
                edits=[{"type": "prefetch", "target": "session"}],
                manage_request=True,
            ),
            make_record("session-A", "stage-1"),
        ]

        async def run():
            queue = janus.Queue()
            try:
                for record in records:
                    queue.sync_q.put(record)
                queue.sync_q.put(None)
                await inferencer._session_aware_worker_loop(None, queue.async_q)
            finally:
                queue.close()
                await queue.wait_closed()

        asyncio.run(run())

        offload_end = next(
            timestamp
            for kind, phase, timestamp in events
            if kind == "offload" and phase == "end"
        )
        prefetch_start = next(
            timestamp
            for kind, phase, timestamp in events
            if kind == "prefetch" and phase == "start"
        )
        self.assertLess(offload_end, prefetch_start)
        self.assertEqual(
            [kind for kind, phase, _ in events if phase == "start"],
            ["start", "offload", "prefetch", "stage-1"],
        )

    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.model_abbr_from_cfg",
        return_value="mabbr",
    )
    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.build_model_from_cfg"
    )
    def test_end_marker_is_not_sent(self, mock_build, mock_abbr):
        mock_build.return_value = DummyModel()
        inferencer = ConcreteApiInferencer(
            model_cfg={},
            batch_size=4,
            mode="perf",
            session_aware=True,
            session_concurrency=1,
            pass_agent_hint=True,
        )
        sent = []

        async def fake_do_request(data, token_bucket, session):
            sent.append(data.get("stage", ""))

        inferencer.do_request = fake_do_request
        records = [
            make_record("session-A", "start"),
            make_record("session-A", "end", manage_request=True),
            make_record("session-A", "stage-1"),
        ]

        async def run():
            queue = janus.Queue()
            try:
                for record in records:
                    queue.sync_q.put(record)
                queue.sync_q.put(None)
                await inferencer._session_aware_worker_loop(None, queue.async_q)
            finally:
                queue.close()
                await queue.wait_closed()

        asyncio.run(run())
        self.assertEqual(sent, ["start", "stage-1"])
    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.model_abbr_from_cfg",
        return_value="mabbr",
    )
    @mock.patch(
        "ais_bench.benchmark.openicl.icl_inferencer."
        "icl_base_inferencer.build_model_from_cfg"
    )
    def test_header_end_marker_reaches_noop_handler(self, mock_build, mock_abbr):
        mock_build.return_value = DummyModel()
        inferencer = ConcreteApiInferencer(
            model_cfg={},
            batch_size=4,
            mode="perf",
            session_aware=True,
            session_concurrency=1,
            pass_agent_hint=True,
            agent_header_mode=True,
        )
        sent = []

        async def fake_do_request(data, token_bucket, session):
            sent.append(data.get("stage", ""))

        inferencer.do_request = fake_do_request
        records = [
            make_record("session-A", "start"),
            make_record("session-A", "end", manage_request=True),
            make_record("session-A", "stage-1"),
        ]

        async def run():
            queue = janus.Queue()
            try:
                for record in records:
                    queue.sync_q.put(record)
                queue.sync_q.put(None)
                await inferencer._session_aware_worker_loop(None, queue.async_q)
            finally:
                queue.close()
                await queue.wait_closed()

        asyncio.run(run())
        self.assertEqual(sent, ["start", "end", "stage-1"])


if __name__ == "__main__":
    unittest.main()
