def _env(key, default=None):
    return __import__("os").environ.get(key, default)


from ais_bench.benchmark.models import VLLMCustomAPI

models = [
    dict(
        attr="service",
        type=VLLMCustomAPI,
        abbr="vllm-api-general-stream-session",
        path=_env("AGENT_WEIGHT_PATH", "/mnt/weight/DeepSeek-V4-Flash"),
        model=_env("AGENT_MODEL", "dsv4flash"),
        stream=True,
        use_chat_api=True,
        session_aware=True,
        session_concurrency=int(_env("AGENT_CONCURRENCY", "4")),
        pass_agent_hint=_env("AGENT_PASS_HINT", "0") == "1",
        skip_prefetch=_env("AGENT_SKIP_PREFETCH", "0") == "1",
        agent_header_mode=_env("AGENT_HEADER_MODE", "0") == "1",
        agent_offload_wait_s=float(_env("AGENT_OFFLOAD_WAIT_S", "0")),
        agent_status_audit_file=_env("AGENT_STATUS_AUDIT_FILE", ""),
        batch_size=int(_env("AGENT_BATCH_SIZE", _env("AGENT_CONCURRENCY", "4"))),
        request_rate=0,
        use_timestamp=False,
        retry=int(_env("AGENT_RETRY", "2")),
        api_key="",
        host_ip=_env("AGENT_HOST", "127.0.0.1"),
        host_port=int(_env("AGENT_PORT", "8000")),
        url="",
        max_out_len=int(_env("AGENT_MAX_OUT_LEN", "256")),
        trust_remote_code=False,
        generation_kwargs=dict(
            temperature=0,
            ignore_eos=_env("AGENT_IGNORE_EOS", "0") == "1",
        ),
    )
]
