from ais_bench.benchmark.openicl.icl_prompt_template import PromptTemplate
from ais_bench.benchmark.openicl.icl_retriever import ZeroRetriever
from ais_bench.benchmark.openicl.icl_inferencer import GenInferencer
from ais_bench.benchmark.datasets import CustomDataset


single_turn_reader_cfg = dict(
    input_columns=['question'],
    output_column=None,
)


single_turn_infer_cfg = dict(
    prompt_template=dict(
        type=PromptTemplate,
        template='{question}',
    ),
    retriever=dict(type=ZeroRetriever),
    inferencer=dict(type=GenInferencer),
)


single_turn_eval_cfg = dict(
    evaluator=None,
    pred_role='BOT',
)


single_turn_datasets = [
    dict(
        abbr='session-single-turn',
        type=CustomDataset,
        path='ais_bench/datasets/session_demo/session_single_turn.jsonl',
        reader_cfg=single_turn_reader_cfg,
        infer_cfg=single_turn_infer_cfg,
        eval_cfg=single_turn_eval_cfg,
    )
]
