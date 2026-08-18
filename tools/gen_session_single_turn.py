"""
为 aisbench session 模式生成单轮（single-turn）数据集。

每个 session 生成 2 条记录：
  1. 正常推理请求（stage=start→end），agent_hint 带 defer evict（推理后执行）
  2. 驱逐请求（stage=end），manage_request=True，只执行 evict 不推理
第 2 条不计入性能统计（do_request 会跳过 report_cache_info）。

共 20 个 session = 40 条记录。
数据使用多领域语料池生成长文本 prompt，避免文本复用。

用法：
    python tools/gen_session_single_turn.py

输出：
    ais_bench/datasets/session_demo/session_single_turn.jsonl
"""

import json
import random
import os
from typing import List

# ===================== 配置 =====================
TOTAL_SESSIONS = 20
MIN_PROMPT_LEN = 10 * 1024
MAX_PROMPT_LEN = 30 * 1024
GLOBAL_SEED = 42
OUTPUT_DIR = "ais_bench/datasets/session_demo"
OUTPUT_FILE = "session_single_turn.jsonl"
# 是否一并生成 evict 记录（manage_request=True，不计入统计）
GEN_EVICT_RECORDS = True
# ===============================================

# 多领域语料池
CORPUS_POOL = [
    "深度学习神经网络通过多层非线性变换实现特征提取与语义建模，卷积神经网络擅长局部特征捕捉，循环神经网络适配时序数据处理，Transformer依托自注意力机制突破长序列建模瓶颈，成为大语言模型核心底座。",
    "分布式云计算依靠负载均衡、节点容错、资源调度实现算力集约化，容器化基于Docker与K8s完成服务编排，微服务拆分单体系统，提升弹性扩容与故障自愈能力。",
    "大数据链路包含采集、清洗、脱敏、存储、计算、分析，Spark批处理、Flink实时流引擎支撑海量数据低延迟处理，数据湖与数仓共同构建企业数据资产体系。",
    "敏捷开发依靠迭代、CI/CD缩短交付周期，代码评审、单元测试、压测、安全测试保障质量，Git版本控制支撑多人协同开发与历史版本快速回滚。",
    "TCP/IP协议栈构成网络通信基础，路由转发、防火墙策略、CDN加速优化传输效率，专线、VPN、内网穿透保障跨地域数据传输安全可靠。",
    "机器学习分为监督、无监督、半监督、强化学习，梯度下降、反向传播、正则化、归一化用来缓解过拟合、梯度消失、收敛速度慢等训练难题。",
    "服务器系统调优包含CPU调度、内存管理、磁盘IO、内核网络参数，合理缓存策略、进程优先级、资源限制提升高并发下吞吐量与稳定性。",
    "大模型推理优化手段包含量化、剪枝、知识蒸馏、算子融合，vLLM采用PagedAttention管理KV缓存，动态批处理降低显存占用、提升吞吐。",
    "NLP典型任务有文本分类、命名实体识别、语义相似度、机器翻译、摘要、对话生成，利用预训练模型基座配合微调适配各类垂直业务。",
    "CUDA并行计算加速矩阵运算，张量并行、流水线并行支撑分布式推理，显存预分配、碎片回收策略持续提高GPU硬件利用率。",
    "高并发系统需要处理限流、熔断、降级、重试、幂等性，消息队列削峰，分布式锁解决竞争冲突，多级缓存防御缓存穿透、击穿、雪崩。",
    "模型对齐技术包含SFT监督微调、RLHF、DPO直接偏好优化，引导模型输出符合人类偏好，减少幻觉、提升安全性与回答实用性。",
    "数据库优化方案涵盖索引设计、SQL调优、读写分离、分库分表，合理选择事务隔离级别，优化锁机制保障业务数据稳定。",
    "边缘计算将算力下沉至终端，降低网络时延，适配物联网、实时视频分析场景，形成本地处理+云端协同的混合计算架构。",
    "上下文扩展技术采用位置编码插值、滑动窗口、稀疏注意力、上下文蒸馏，有效扩展模型原生上下文窗口，支持超长文档问答。",
    "异步编程模型依靠事件循环避免线程开销，协程IO多路复用适合网络密集型服务，合理控制并发队列防止瞬间打爆后端服务。",
    "向量数据库将文本Embedding持久化存储，支持近似最近邻检索，用于知识库问答、RAG检索增强生成系统，实现外部知识接入大模型。",
    "RAG检索增强生成通过文档切片、向量化、相似度检索，把外部相关片段送入prompt，缓解大模型知识截止、幻觉问题。",
    "模型服务网关负责鉴权、限流、路由、负载均衡，将多模型实例统一对外暴露接口，隔离业务流量与底层推理服务。",
    "性能压测关注吞吐量、首包延迟、token生成速度、显存波动、排队时延，长文本场景重点观测KV缓存分配、碎片增长、OOM风险。"
]


random.seed(GLOBAL_SEED)


def generate_long_prompt() -> str:
    """用语料池拼接生成长文本 prompt。"""
    target_len = random.randint(MIN_PROMPT_LEN, MAX_PROMPT_LEN)
    buf: List[str] = []
    while len("".join(buf)) < target_len:
        seg = random.choice(CORPUS_POOL)
        buf.append(seg)
    full = "".join(buf)
    return full[:target_len]


def answer_for(prompt: str) -> str:
    """构造占位 answer。"""
    return "好的，以上概念涵盖" + "、".join(
        random.sample(["机器学习", "深度学习", "分布式系统", "NLP", "CV", "推理优化", "安全", "网络"], 3)
    ) + "等方面。"


def main():
    records = []
    for i in range(TOTAL_SESSIONS):
        question = generate_long_prompt()
        answer = answer_for(question)
        session_id = f"single-{i:03d}"

        # 记录 1：正常推理请求
        # edits 为空：evict 由独立的驱逐请求统一执行，避免推理完成后
        # deferred evict 先释放了 block，导致驱逐请求找不到 session。
        infer_record = {
            "question": question,
            "answer": answer,
            "agent_hint": {
                "session_id": session_id,
                "parent_session_id": "",
                "cache_control": {},
                "context_management": {
                    "manage_request": False,
                    "edits": [],
                },
                "stage": "start",
            },
        }
        records.append(infer_record)

        if GEN_EVICT_RECORDS:
            # 记录 2：驱逐请求（不推理，只 evict，不计入统计）
            evict_record = {
                "question": "",
                "answer": "",
                "agent_hint": {
                    "session_id": session_id,
                    "parent_session_id": "",
                    "cache_control": {},
                    "context_management": {
                        "manage_request": True,
                        "edits": [{"type": "evict", "target": "session"}],
                    },
                    "stage": "end",
                },
            }
            records.append(evict_record)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    total_records = len(records)
    infer_count = TOTAL_SESSIONS
    evict_count = total_records - infer_count
    print(f"生成完成：{out_path}")
    print(f"  session 数：{TOTAL_SESSIONS}")
    print(f"  总请求数：{total_records}（推理 {infer_count} + 驱逐 {evict_count}）")
    print(f"  prompt 长度范围：{MIN_PROMPT_LEN} ~ {MAX_PROMPT_LEN} chars")


if __name__ == "__main__":
    main()
