"""LLM Provider 层：统一 mock / OpenAI 兼容两种实现。

- 结构恢复走 complete_json（JSON 输出、temperature=0）
- Agent 走 as_chat_model（langchain BaseChatModel，bind_tools 由 graph 使用）
- 未配置 API Key 或调用失败时上层必须走降级路径（design.md 4.4）
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Sequence

from doc_analyze.config import Settings

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class LLMError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍 markdown 代码栅栏）。"""
    m = _JSON_FENCE.search(text)
    raw = m.group(1) if m else text
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise LLMError(f"模型输出不含 JSON：{text[:200]}")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"JSON 解析失败：{e}") from e


class MockChatModel:
    """脚本化双轮对话模型：第 1 轮按关键词选工具，第 2 轮汇总工具结果。

    用于无 API Key 时端到端验证 LangGraph 工具循环，不依赖网络。
    """

    def __init__(self, doc_id: int = 1):
        self.doc_id = doc_id

    # ---- langchain BaseChatModel 兼容的最小面 ----
    @property
    def _llm_type(self) -> str:  # pragma: no cover
        return "mock"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "MockChatModel":
        return self

    def invoke(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        human = next(
            (m.content for m in reversed(messages) if isinstance(m, HumanMessage)), ""
        )
        tool_texts = [
            f"{getattr(m, 'name', 'tool')}: {str(m.content)[:400]}"
            for m in messages
            if isinstance(m, ToolMessage)
        ]
        if not tool_texts:
            pairs = [
                ("废标", "search_content"),
                ("保证金", "search_content"),
                ("评分", "search_content"),
                ("在哪", "find_chapter"),
                ("哪个章节", "find_chapter"),
            ]
            name, args = "get_document_map", {"doc_id": self.doc_id}
            for kw, tool in pairs:
                if kw in str(human):
                    name = tool
                    break
            if name == "search_content":
                args = {"doc_id": self.doc_id, "keywords": "保证金" if "保证金" in str(human) else "评分"}
            elif name == "find_chapter":
                args = {"doc_id": self.doc_id, "title_keywords": "评标"}
            return AIMessage(
                content="", tool_calls=[{"name": name, "args": args, "id": "mock_call_1", "type": "tool_call"}]
            )
        body = "\n".join(tool_texts)
        return AIMessage(content=f"[MOCK 回答] 已通过工具查询到以下结构化信息，请溯源章节路径：\n{body}")

    # 让它同时能以 BaseChatModel 子类形态被 langgraph 使用
    def with_config(self, **kwargs: Any) -> "MockChatModel":
        return self


class MockProvider:
    """确定性 mock：结构恢复回显建议层级；Agent 用脚本化双轮模型。"""

    def complete_json(self, system: str, user: str) -> dict:
        payload = json.loads(user)
        items: List[dict] = payload["items"]
        out = []
        for it in items:
            sug = it.get("suggested_level")
            if isinstance(sug, int) and sug >= 1:
                # 锚点：回显建议层级
                out.append({"idx": it["idx"], "is_title": True, "level": sug})
            else:
                # 待定项：mock 保守策略——不升格为标题（留给真实模型裁决）
                out.append({"idx": it["idx"], "is_title": False, "level": None})
        return {"items": out}

    def as_chat_model(self, settings: Settings, doc_id: int) -> Any:
        return MockChatModel(doc_id=doc_id)


class OpenAICompatProvider:
    """OpenAI 兼容接口（ChatGPT、DashScope 兼容模式、内网网关均可用）。

    complete_json: response_format=json_object + 温度 0 + 客户端 JSON 抽取与重试。
    鉴权二选一：OPENAI_API_KEY（默认 Bearer 头）或 openai_auth_header
    （完整 Authorization 值，用于 anvil 等要求裸 token 的网关）。
    """

    def __init__(self, settings: Settings):
        if not settings.openai_api_key and not settings.openai_auth_header:
            raise LLMError("OPENAI_API_KEY 或 DOC_ANALYZE_OPENAI_AUTH_HEADER 至少配置一个")
        self._settings = settings

    def _model(self, model_name: str) -> Any:
        from langchain_openai import ChatOpenAI

        kwargs: dict[str, Any] = {}
        if self._settings.openai_auth_header:
            # 覆盖 openai SDK 默认的 "Authorization: Bearer <key>"
            kwargs["default_headers"] = {"Authorization": self._settings.openai_auth_header}
        return ChatOpenAI(
            model=model_name,
            api_key=self._settings.openai_api_key or "unused",
            base_url=self._settings.openai_base_url,
            temperature=0,
            **kwargs,
        )

    def complete_json(self, system: str, user: str) -> dict:
        from langchain_core.messages import HumanMessage, SystemMessage

        llm = self._model(self._settings.recovery_model)
        last_err: Exception | None = None
        for _ in range(2):  # 1 次重试（design.md 4.4）
            try:
                resp = llm.invoke(
                    [SystemMessage(content=system), HumanMessage(content=user)]
                )
                return _extract_json(resp.content)
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise LLMError(f"complete_json 调用失败：{last_err}")

    def as_chat_model(self, settings: Settings, doc_id: int) -> Any:
        return self._model(settings.agent_model)


def get_provider(settings: Settings) -> Any | None:
    """按配置返回 Provider；provider 未配置/未知返回 None（上层走降级）。"""
    if settings.llm_provider == "mock":
        return MockProvider()
    if settings.llm_provider == "openai":
        return OpenAICompatProvider(settings)
    return None
