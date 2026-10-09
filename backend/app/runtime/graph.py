"""app/runtime/graph.py — 阶段 1 图定义（开发计划 §5.1）

parse → [lineart ∥ retrieve] → gate → END
两条支路无依赖并行执行，gate 等两支都到达后收口。阶段 2（prompt/generate）在 M5
以独立子图接入，confirm 时从持久化状态恢复后续跑。
"""

from langgraph.graph import END, START, StateGraph

from app.runtime.nodes import (
    assemble_node,
    gate_node,
    generate_node,
    lineart_node,
    parse_node,
    prompt_node,
    retrieve_node,
)
from app.runtime.state import STAGE_FAILED, DesignState


def _route_after_parse(state: DesignState) -> list[str]:
    """parse 失败（如实拍图缺失）直接收口到 gate，不再起并行支路。"""
    return ["lineart", "retrieve"] if state.get("stage") != STAGE_FAILED else ["gate"]


def build_stage1_graph():
    g = StateGraph(DesignState)
    g.add_node("parse", parse_node)
    g.add_node("lineart", lineart_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("gate", gate_node)

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", _route_after_parse, ["lineart", "retrieve", "gate"])
    g.add_edge("lineart", "gate")
    g.add_edge("retrieve", "gate")
    g.add_edge("gate", END)

    return g.compile()


def build_stage2_graph():
    """阶段 2：prompt → generate → assemble（confirm 时从持久化状态恢复后续跑）。"""
    g = StateGraph(DesignState)
    g.add_node("prompt", prompt_node)
    g.add_node("generate", generate_node)
    g.add_node("assemble", assemble_node)

    g.add_edge(START, "prompt")
    # prompt 失败直接到 assemble 收口，不再进入生图
    g.add_conditional_edges(
        "prompt",
        lambda s: ["generate"] if s.get("stage") != STAGE_FAILED else ["assemble"],
        ["generate", "assemble"],
    )
    g.add_edge("generate", "assemble")
    g.add_edge("assemble", END)

    return g.compile()


_stage1_graph = None
_stage2_graph = None


def get_stage1_graph():
    """进程级单例（编译结果无状态，可复用）。"""
    global _stage1_graph
    if _stage1_graph is None:
        _stage1_graph = build_stage1_graph()
    return _stage1_graph


def get_stage2_graph():
    global _stage2_graph
    if _stage2_graph is None:
        _stage2_graph = build_stage2_graph()
    return _stage2_graph
