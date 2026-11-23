import json
import unittest

from ais_bench.datasets.agent.build_agent_dataset import build_kvc_interleaved_dataset


class FakeTokenizer:
    def encode(self, text):
        return type("Encoded", (), {"ids": list(range(len(text)))})()


def make_session(session_id, lengths, parent_id=None):
    parent_id = parent_id or session_id
    records = []
    for idx, length in enumerate(lengths):
        records.append(
            {
                "question": "x" * length,
                "answer": "",
                "max_tokens": 1000,
                "agent_hint": {
                    "session_id": session_id,
                    "parent_session_id": parent_id,
                    "cache_control": {"type": "ephemeral", "ttl": 300},
                    "context_management": {"manage_request": False, "edits": []},
                    "stage": "start" if idx == 0 else f"stage-{idx}",
                },
            }
        )
    return records


def is_manage(record):
    return bool(
        ((record.get("agent_hint") or {}).get("context_management") or {}).get(
            "manage_request"
        )
    )


def edit_types(record):
    context_management = (record.get("agent_hint") or {}).get(
        "context_management"
    ) or {}
    return [
        edit.get("type")
        for edit in (context_management.get("edits") or [])
        if isinstance(edit, dict)
    ]


class TestKvcInterleavedDataset(unittest.TestCase):
    def setUp(self):
        self.dataset = make_session("session-A", [1, 2, 3, 4, 5, 6, 7, 8])
        self.dataset += make_session("session-B", [1, 2, 3, 4, 5, 6, 7, 8])

    def build(self, **kwargs):
        params = {
            "pause_min_prompt_tokens": 5,
            "interleave_turns": 2,
            "prefetch_lead_requests": 1,
            "output_token_cap": 256,
            "max_model_len": None,
        }
        params.update(kwargs)
        return build_kvc_interleaved_dataset(
            self.dataset,
            FakeTokenizer(),
            **params,
        )

    def test_lifecycle_and_lead(self):
        output, manifest = self.build()
        self.assertEqual(manifest["counts"]["data_plane"], 16)
        self.assertEqual(manifest["counts"]["offload"], 4)
        self.assertEqual(manifest["counts"]["prefetch"], 4)
        self.assertEqual(manifest["counts"]["evict"], 2)
        self.assertEqual(manifest["counts"]["end"], 1)
        self.assertTrue(all(event["lead_data_requests"] == 1 for event in manifest["prefetch_events"]))
        self.assertEqual(len(manifest["prefetch_events"]), 4)

        for session_id in ("session-A", "session-B"):
            stages = [
                record["agent_hint"]["stage"]
                for record in output
                if not is_manage(record)
                and record["agent_hint"]["session_id"] == session_id
            ]
            self.assertEqual(
                stages,
                ["start", "stage-1", "stage-2", "stage-3", "stage-4", "stage-5", "stage-6", "stage-7"],
            )

        for idx, record in enumerate(output):
            if "prefetch" not in edit_types(record):
                continue
            session_id = record["agent_hint"]["session_id"]
            later_data = [
                later
                for later in output[idx + 1:]
                if not is_manage(later)
                and later["agent_hint"]["session_id"] == session_id
            ]
            self.assertTrue(later_data)
            between = 0
            for later in output[idx + 1:]:
                if is_manage(later):
                    continue
                if later["agent_hint"]["session_id"] == session_id:
                    break
                between += 1
            self.assertEqual(between, 1)

        evicted = {
            record["agent_hint"]["session_id"]
            for record in output
            if "evict" in edit_types(record)
        }
        self.assertEqual(evicted, {"session-A", "session-B"})
        for idx, record in enumerate(output):
            if "evict" not in edit_types(record):
                continue
            session_id = record["agent_hint"]["session_id"]
            self.assertFalse(
                any(
                    not is_manage(later)
                    and later["agent_hint"]["session_id"] == session_id
                    for later in output[idx + 1:]
                )
            )

    def test_parent_and_cache_control(self):
        output, manifest = self.build()
        parent_ids = {
            record["agent_hint"]["parent_session_id"]
            for record in output
        }
        self.assertEqual(parent_ids, {"session-A"})
        self.assertEqual(manifest["parents"][0]["secondary_session_id"], "session-B")
        self.assertTrue(all("cache_control" not in record["agent_hint"] for record in output))

    def test_output_cap(self):
        output, _ = self.build(output_token_cap=128)
        data_records = [record for record in output if not is_manage(record)]
        self.assertTrue(all(record["max_tokens"] == 128 for record in data_records))

    def test_max_model_len_gate(self):
        with self.assertRaisesRegex(ValueError, "max_model_len gate"):
            self.build(max_model_len=600)

    def test_deterministic(self):
        first, first_manifest = self.build()
        second, second_manifest = self.build()
        self.assertEqual(
            [json.dumps(record, sort_keys=True) for record in first],
            [json.dumps(record, sort_keys=True) for record in second],
        )
        self.assertEqual(first_manifest, second_manifest)


if __name__ == "__main__":
    unittest.main()
