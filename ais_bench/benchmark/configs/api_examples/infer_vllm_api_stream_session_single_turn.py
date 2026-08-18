from mmengine.config import read_base
from ais_bench.benchmark.partitioners import NaivePartitioner
from ais_bench.benchmark.runners.local import LocalRunner
from ais_bench.benchmark.tasks import OpenICLApiInferTask

with read_base():
    from ais_bench.benchmark.configs.models.vllm_api.vllm_api_general_stream_session import models
    from ais_bench.benchmark.configs.datasets.custom.session_single_turn_gen import single_turn_datasets
    from ais_bench.benchmark.configs.summarizers.example import summarizer

datasets = [*single_turn_datasets]

infer = dict(
    partitioner=dict(type=NaivePartitioner),
    runner=dict(
        type=LocalRunner,
        max_num_workers=1,
        task=dict(type=OpenICLApiInferTask),
    ),
)

work_dir = 'outputs/api-vllm-stream-session-single-turn/'
