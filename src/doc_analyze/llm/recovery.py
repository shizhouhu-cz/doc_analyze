"""LLM 全局结构恢复调用端（design.md 4.4）。

单趟全局定级：文档顺序候选序列 -> 1 次模型调用 -> Pydantic 校验 -> list[dict]。
任何失败（provider 未配置 / 调用异常 / 输出非法）返回 None，由调用方走建议层级降级建树。
"""
from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError, field_validator, model_validator

from doc_analyze.config import Settings
from doc_analyze.llm.provider import LLMError, get_provider

SYSTEM_PROMPT = (
    "你是标书文档结构分析专家。输入是按文档顺序排列的候选标题序列，"
    "每项包含编号文本 number_prefix、标题 title、是否加粗 bold、字号 font_size、建议层级 suggested_level。\n"
    "任务：对整条序列统一定级。建议层级只是默认值，可依据编号语义"
    "（如「第一章」与「1.1」是父子关系，层级应相差 1）与文档上下文纠偏；"
    "明显是正文误入的候选项判 is_title=false。\n"
    '只输出严格 JSON，格式：{"items":[{"idx":int,"is_title":bool,"level":int|null}]}\n'
    "约束：level 取 1..9；is_title=false 时 level 必须为 null；不得增删 idx，不得改变顺序。"
)


class RecoveryItem(BaseModel):
    idx: int
    is_title: bool
    level: int | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _clamp_level(cls, v: object) -> object:
        if isinstance(v, int) and not isinstance(v, bool):
            return max(1, min(9, v))
        return v

    @model_validator(mode="after")
    def _null_level_when_not_title(self) -> "RecoveryItem":
        if not self.is_title:
            self.level = None
        return self


class RecoveryResult(BaseModel):
    items: list[RecoveryItem]


def _user_payload(items: list[dict]) -> str:
    slim = []
    for it in items:
        slim.append(
            {
                "idx": int(it["idx"]),
                "number_prefix": it.get("number_prefix") or "",
                "title": str(it.get("title") or "")[:40],
                "bold": bool(it.get("bold")),
                "font_size": it.get("font_size"),
                "suggested_level": it.get("suggested_level"),
            }
        )
    return json.dumps({"items": slim}, ensure_ascii=False)


def recover_structure(items: list[dict], settings: Settings) -> list[dict] | None:
    """单趟全局结构恢复（文档级 1 次调用 + 1 次重试）。

    返回 [{"idx", "is_title", "level"}]（按输入顺序）；失败返回 None（调用方降级）。
    """
    provider = get_provider(settings)
    if provider is None:
        return None
    payload = _user_payload(items)
    expected_idx = {int(it["idx"]) for it in items}
    for _ in range(2):  # 首次 + 1 次重试（design.md 4.4）
        try:
            data = provider.complete_json(SYSTEM_PROMPT, payload)
            result = RecoveryResult.model_validate(data)
            got_idx = {it.idx for it in result.items}
            if got_idx != expected_idx:
                raise LLMError(
                    f"模型输出 idx 集合不符：期望 {len(expected_idx)} 项，实际 {len(got_idx)} 项"
                )
            by_idx: dict[int, dict] = {}
            for it in result.items:
                by_idx.setdefault(
                    it.idx, {"idx": it.idx, "is_title": it.is_title, "level": it.level}
                )
            return [by_idx[int(it["idx"])] for it in items]
        except (LLMError, ValidationError, ValueError, TypeError, KeyError):
            continue
    return None
