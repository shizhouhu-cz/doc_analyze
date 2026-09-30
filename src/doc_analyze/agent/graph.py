"""LangGraph 查询 Agent（design.md 第六章）：agent(bind_tools) ⇄ tools 循环。"""
from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.prebuilt import ToolNode

from doc_analyze import db
from doc_analyze.agent.tools import (
    find_chapter,
    get_chapter,
    get_document_map,
    render_document_map,
    search_content,
)
from doc_analyze.config import Settings
from doc_analyze.llm.provider import LLMError, get_provider

TOOLS = [get_document_map, get_chapter, search_content, find_chapter]


def _build_system_prompt(settings: Settings, doc_id: int) -> str:
    conn = db.connect(settings.db_path)
    try:
        doc = conn.execute(
            "SELECT doc_name FROM documents WHERE id=?", (doc_id,)
        ).fetchone()
        map_text = render_document_map(conn, doc_id)
    finally:
        conn.close()
    doc_name = doc["doc_name"] if doc is not None else str(doc_id)
    return (
        f"你是标书文档查询助手，当前文档：《{doc_name}》（doc_id={doc_id}）。\n"
        "职责：理解用户意图、选择合适工具、组装参数；所有数据均来自结构化查询，"
        "工具返回的核心数据透传给用户，不做改写。\n"
        "工作路径：先用文档地图或 find_chapter 定位章节，再用 get_chapter 下钻，"
        "或用 search_content 直接检索关键词。\n"
        "【下钻协议】get_chapter 返回的 content 不含子章节正文；children 为子章节目录"
        "（含 chapter_id 与 content_chars），需要细节必须按 chapter_id 继续调用 get_chapter；"
        "content_chars 大的子章节可优先取；禁止假设 content 包含全部内容。\n"
        "回答时必须引用 chapter_path 溯源到具体章节。\n"
        "【文档地图】（每行：id|level|chapter_path|编号+标题）：\n"
        + map_text
    )


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def build_graph(settings: Settings, doc_id: int):
    """构建查询 Agent：文档地图预注入 + 工具循环 + SqliteSaver checkpoint。"""
    provider = get_provider(settings)
    if provider is None:
        raise LLMError("LLM provider 未配置（llm_provider 需为 mock/openai），无法构建查询 Agent")
    model = provider.as_chat_model(settings, doc_id)
    system_prompt = _build_system_prompt(settings, doc_id)
    tool_node = ToolNode(TOOLS)

    def agent_node(state: AgentState) -> dict:
        messages = [SystemMessage(content=system_prompt)] + list(state["messages"])
        ai = model.bind_tools(TOOLS).invoke(messages)
        return {"messages": [ai]}

    def route(state: AgentState):
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
            return "tools"
        return END

    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", END: END})
    builder.add_edge("tools", "agent")

    checkpoint_path = settings.db_path.parent / "checkpoints.sqlite"
    ckpt_conn = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    checkpointer = SqliteSaver(ckpt_conn)
    checkpointer.setup()
    return builder.compile(checkpointer=checkpointer)


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content)


def run_query_trace(
    settings: Settings, doc_id: int, question: str, thread_id: str = "cli"
) -> dict:
    """单次问答并返回工具调用轨迹：{"answer": str, "steps": [{tool, args, result}]}。

    服务端调试页使用；result 截断到 2000 字符避免响应过大。
    """
    graph = build_graph(settings, doc_id)
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.max_agent_rounds + 2,
    }
    result = graph.invoke({"messages": [HumanMessage(content=question)]}, config=config)
    steps: list[dict] = []
    by_call_id: dict[str, dict] = {}
    for m in result["messages"]:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                step = {
                    "tool": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "result": None,
                }
                steps.append(step)
                by_call_id[tc.get("id", "")] = step
        elif isinstance(m, ToolMessage) and m.tool_call_id in by_call_id:
            by_call_id[m.tool_call_id]["result"] = _message_text(m.content)[:2000]
    answer = ""
    for m in reversed(result["messages"]):
        if isinstance(m, AIMessage):
            answer = _message_text(m.content)
            break
    return {"answer": answer, "steps": steps}


def run_query(settings: Settings, doc_id: int, question: str, thread_id: str = "cli") -> str:
    """便捷入口：单次问答，返回最后一条 AIMessage 的 content。"""
    return run_query_trace(settings, doc_id, question, thread_id=thread_id)["answer"]
