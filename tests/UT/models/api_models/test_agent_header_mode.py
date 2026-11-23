import asyncio
import base64
import json
import os
import tempfile
import unittest
from unittest import mock

from ais_bench.benchmark.models import VLLMCustomAPI
from ais_bench.benchmark.models.output import Output
from ais_bench.benchmark.openicl.icl_inferencer.icl_gen_inferencer import GenInferencer


HINT = {
    "session_id": "workbuddy/session-1",
    "parent_session_id": "workbuddy/parent-1",
    "stage": "stage-1",
}


def decode_header(value):
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return json.loads(raw.decode("utf-8"))


class TestVLLMCustomAPIHeaderMode(unittest.TestCase):
    def make_model(self, *, header_mode=True, stream=False):
        return VLLMCustomAPI(
            path="test-model",
            model="test-model",
            stream=stream,
            retry=1,
            host_ip="localhost",
            host_port=8080,
            generation_kwargs={},
            agent_header_mode=header_mode,
        )

    def test_header_mode_normal_request_uses_per_request_header(self):
        async def run():
            model = self.make_model()
            model.text_infer = mock.AsyncMock()
            await model.generate("prompt", 8, Output(), session=mock.Mock(), agent_hint=HINT.copy())

            body = model.text_infer.await_args.args[0]
            headers = model.text_infer.await_args.kwargs["request_headers"]
            self.assertNotIn("agent_hint", body)
            self.assertNotIn("stage", body)
            self.assertEqual(
                decode_header(headers["X-Agent-Hint"]),
                {
                    "session": {
                        "session_id": "workbuddy/session-1",
                        "parent_session_id": "workbuddy/parent-1",
                    }
                },
            )
            self.assertNotIn("X-Agent-Hint", model.headers)

        asyncio.run(run())

    def test_header_mode_stream_request_uses_same_translation(self):
        async def run():
            model = self.make_model(stream=True)
            model.stream_infer = mock.AsyncMock()
            await model.generate("prompt", 8, Output(), session=mock.Mock(), agent_hint=HINT.copy())

            body = model.stream_infer.await_args.args[0]
            headers = model.stream_infer.await_args.kwargs["request_headers"]
            self.assertNotIn("agent_hint", body)
            self.assertIn("X-Agent-Hint", headers)

        asyncio.run(run())

    def test_legacy_mode_keeps_body_hint_and_has_no_custom_headers(self):
        async def run():
            model = self.make_model(header_mode=False)
            model.text_infer = mock.AsyncMock()
            await model.generate("prompt", 8, Output(), session=mock.Mock(), agent_hint=HINT.copy())

            body = model.text_infer.await_args.args[0]
            self.assertEqual(body["agent_hint"], HINT)
            self.assertNotIn("request_headers", model.text_infer.await_args.kwargs)
            self.assertNotIn("X-Agent-Hint", model.headers)

        asyncio.run(run())

    def test_header_mode_rejects_manage_inference_request(self):
        async def run():
            model = self.make_model()
            manage_hint = HINT.copy()
            manage_hint["context_management"] = {
                "manage_request": True,
                "edits": [{"type": "offload", "target": "session"}],
            }
            with self.assertRaisesRegex(ValueError, "agent-status"):
                await model.generate("prompt", 8, Output(), session=mock.Mock(), agent_hint=manage_hint)

        asyncio.run(run())

    def test_requests_do_not_share_or_leak_headers(self):
        async def run():
            model = self.make_model()
            calls = []

            async def capture(body, output, **kwargs):
                await asyncio.sleep(0)
                calls.append((body, kwargs))

            model.text_infer = capture
            other = HINT.copy()
            other["session_id"] = "workbuddy/session-2"
            await asyncio.gather(
                model.generate("p1", 8, Output(), session=mock.Mock(), agent_hint=HINT.copy()),
                model.generate("p2", 8, Output(), session=mock.Mock(), agent_hint=other),
                model.generate("p3", 8, Output(), session=mock.Mock()),
            )
            first = decode_header(calls[0][1]["request_headers"]["X-Agent-Hint"])
            second = decode_header(calls[1][1]["request_headers"]["X-Agent-Hint"])
            self.assertEqual(first["session"]["session_id"], "workbuddy/session-1")
            self.assertEqual(second["session"]["session_id"], "workbuddy/session-2")
            self.assertNotIn("request_headers", calls[2][1])
            self.assertNotIn("X-Agent-Hint", model.headers)

        asyncio.run(run())


class DummyStatusCounter:
    async def post(self):
        pass

    async def rev(self):
        pass

    async def failed(self):
        pass

    async def finish(self):
        pass

    async def case_finish(self):
        pass


class DummyModel:
    max_out_len = 16
    is_api = True
    base_url = "http://motor:1025/"
    headers = {"Content-Type": "application/json"}

    def parse_template(self, prompt, mode="gen"):
        return prompt


class FakeStatusResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload if payload is not None else {"applied": True, "reason": "applied"}
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return self.payload


class FakeStatusSession:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status
        self.calls = []

    def post(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStatusResponse(self.payload, self.status)


def manage_data(edit_type, index=7):
    return {
        "index": index,
        "prompt": "KV management only",
        "data_abbr": "test",
        "max_out_len": 1,
        "session_id": HINT["session_id"],
        "sched_parent_id": "local-scheduling-only",
        "agent_hint": {
            **HINT,
            "context_management": {
                "manage_request": True,
                "edits": [{"type": edit_type, "target": "session"}],
            },
        },
    }


class TestGenInferencerAgentStatus(unittest.TestCase):
    def make_inferencer(self, model, **kwargs):
        with mock.patch(
            "ais_bench.benchmark.openicl.icl_inferencer.icl_base_inferencer.build_model_from_cfg",
            return_value=model,
        ), mock.patch(
            "ais_bench.benchmark.openicl.icl_inferencer.icl_base_inferencer.model_abbr_from_cfg",
            return_value="mabbr",
        ):
            inferencer = GenInferencer(model_cfg={}, batch_size=1, **kwargs)
        inferencer.status_counter = DummyStatusCounter()
        inferencer.output_handler.report_cache_info = mock.AsyncMock()
        return inferencer

    def test_manage_edits_map_to_out_of_band_events(self):
        async def run():
            for edit_type, state in (("offload", "pause"), ("prefetch", "resume"), ("evict", "finalize")):
                with self.subTest(edit_type=edit_type):
                    model = DummyModel()
                    model.generate = mock.AsyncMock()
                    inferencer = self.make_inferencer(
                        model, agent_header_mode=True, pass_agent_hint=True
                    )
                    session = FakeStatusSession()
                    await inferencer.do_request(manage_data(edit_type), None, session)
                    self.assertEqual(len(session.calls), 1)
                    call = session.calls[0]
                    self.assertEqual(call["url"], "http://motor:1025/agent-status")
                    self.assertEqual(
                        call["json"],
                        {
                            "session": {"session_id": HINT["session_id"]},
                            "agent_status": {"state": state},
                        },
                    )
                    self.assertNotIn("parent_session_id", call["json"]["session"])
                    model.generate.assert_not_awaited()

        asyncio.run(run())

    def test_a_group_sends_no_manage_event(self):
        async def run():
            model = DummyModel()
            model.generate = mock.AsyncMock()
            inferencer = self.make_inferencer(
                model, agent_header_mode=True, pass_agent_hint=False
            )
            session = FakeStatusSession()
            await inferencer.do_request(manage_data("offload"), None, session)
            self.assertEqual(session.calls, [])
            model.generate.assert_not_awaited()

        asyncio.run(run())

    def test_b0_skips_only_resume(self):
        async def run():
            model = DummyModel()
            inferencer = self.make_inferencer(
                model,
                agent_header_mode=True,
                pass_agent_hint=True,
                skip_prefetch=True,
            )
            session = FakeStatusSession()
            await inferencer.do_request(manage_data("prefetch"), None, session)
            self.assertEqual(session.calls, [])
            await inferencer.do_request(manage_data("offload", index=8), None, session)
            await inferencer.do_request(manage_data("evict", index=9), None, session)
            self.assertEqual(
                [call["json"]["agent_status"]["state"] for call in session.calls],
                ["pause", "finalize"],
            )

        asyncio.run(run())

    def test_empty_manage_marker_writes_noop_audit_without_event(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                audit_path = os.path.join(tmpdir, "events.jsonl")
                model = DummyModel()
                model.generate = mock.AsyncMock()
                inferencer = self.make_inferencer(
                    model,
                    agent_header_mode=True,
                    pass_agent_hint=True,
                    agent_status_audit_file=audit_path,
                )
                session = FakeStatusSession()
                data = manage_data("offload")
                data["agent_hint"]["context_management"]["edits"] = []
                await inferencer.do_request(data, None, session)
                self.assertEqual(session.calls, [])
                model.generate.assert_not_awaited()
                with open(audit_path, encoding="utf-8") as audit_file:
                    record = json.loads(audit_file.readline())
                self.assertTrue(record["success"])
                self.assertTrue(record["noop"])
                self.assertEqual(record["reason"], "empty_edits_noop")
                self.assertIsNone(record["http_status"])

        asyncio.run(run())

    def test_unknown_manage_edit_is_an_audited_error(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                audit_path = os.path.join(tmpdir, "events.jsonl")
                model = DummyModel()
                inferencer = self.make_inferencer(
                    model,
                    agent_header_mode=True,
                    pass_agent_hint=True,
                    agent_status_audit_file=audit_path,
                )
                data = manage_data("offload")
                data["agent_hint"]["context_management"]["edits"].append(
                    {"type": "prefetch", "target": "session"}
                )
                with self.assertRaisesRegex(ValueError, "unsupported KVC edits"):
                    await inferencer.do_request(data, None, FakeStatusSession())
                with open(audit_path, encoding="utf-8") as audit_file:
                    record = json.loads(audit_file.readline())
                self.assertFalse(record["success"])
                self.assertEqual(record["reason"], "unsupported_edits")

        asyncio.run(run())

    def test_legacy_manage_request_still_uses_model_generate(self):
        async def run():
            model = DummyModel()

            async def generate(inputs, max_out_len, output=None, session=None, **kwargs):
                output.success = True
                output.content = "managed"

            model.generate = mock.AsyncMock(side_effect=generate)
            inferencer = self.make_inferencer(model, pass_agent_hint=True)
            session = FakeStatusSession()
            await inferencer.do_request(manage_data("offload"), None, session)
            self.assertEqual(session.calls, [])
            kwargs = model.generate.await_args.kwargs
            self.assertIn("agent_hint", kwargs)
            self.assertNotIn("sched_parent_id", kwargs)
            self.assertTrue(kwargs["agent_hint"]["context_management"]["manage_request"])

        asyncio.run(run())

    def test_event_response_and_failures_are_audited(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmpdir:
                audit_path = os.path.join(tmpdir, "events.jsonl")
                model = DummyModel()
                inferencer = self.make_inferencer(
                    model,
                    agent_header_mode=True,
                    pass_agent_hint=True,
                    agent_status_audit_file=audit_path,
                )
                await inferencer.do_request(manage_data("offload"), None, FakeStatusSession())
                with self.assertRaisesRegex(RuntimeError, "agent-status failed"):
                    await inferencer.do_request(
                        manage_data("prefetch", index=8),
                        None,
                        FakeStatusSession({"applied": False, "reason": "terminal"}),
                    )
                with open(audit_path, encoding="utf-8") as audit_file:
                    records = [json.loads(line) for line in audit_file]
                self.assertEqual(len(records), 2)
                self.assertTrue(records[0]["success"])
                self.assertEqual(records[0]["state"], "pause")
                self.assertFalse(records[1]["success"])
                self.assertEqual(records[1]["reason"], "terminal")
                self.assertIn("error", records[1])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
