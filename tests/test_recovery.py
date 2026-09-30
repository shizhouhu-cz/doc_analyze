"""recover_structure 单元测试（mock，无网络）。"""
from __future__ import annotations

from doc_analyze.config import Settings
from doc_analyze.llm import provider as provider_mod
from doc_analyze.llm.provider import LLMError
from doc_analyze.llm.recovery import recover_structure


def _settings(tmp_path, llm_provider: str = "mock") -> Settings:
    return Settings(db_path=tmp_path / "t.db", llm_provider=llm_provider)


def test_mock_echoes_suggested_levels(tmp_path):
    items = [
        {"idx": 1, "number_prefix": "第一章", "title": "招标公告", "bold": True,
         "font_size": 16.0, "suggested_level": 1},
        {"idx": 2, "number_prefix": "1.1", "title": "招标范围", "bold": False,
         "font_size": 14.0, "suggested_level": 2},
        {"idx": 3, "number_prefix": "2", "title": "投标人须知", "bold": True,
         "font_size": 16.0, "suggested_level": 1},
    ]
    out = recover_structure(items, _settings(tmp_path))
    assert out is not None
    assert [o["idx"] for o in out] == [1, 2, 3]
    assert all(o["is_title"] for o in out)
    assert [o["level"] for o in out] == [1, 2, 1]


def test_pending_items_not_promoted(tmp_path):
    # Mock 对待定项不做升格：is_title=False 交真实模型裁决（design 4.4）
    items = [
        {"idx": 1, "number_prefix": "第一章", "title": "总则", "bold": True,
         "font_size": 16.0, "suggested_level": 1},
        {"idx": 2, "number_prefix": "", "title": "加粗未知行", "bold": True,
         "font_size": 12.0, "suggested_level": None},
        {"idx": 3, "number_prefix": "", "title": "另一待定行", "bold": False,
         "font_size": 12.0, "suggested_level": None},
    ]
    out = recover_structure(items, _settings(tmp_path))
    assert out is not None
    assert [o["level"] for o in out] == [1, None, None]
    assert [o["is_title"] for o in out] == [True, False, False]


def test_unknown_provider_returns_none(tmp_path):
    items = [{"idx": 1, "number_prefix": "第一章", "title": "总则", "bold": True,
              "font_size": 16.0, "suggested_level": 1}]
    assert recover_structure(items, _settings(tmp_path, llm_provider="unknown")) is None


def test_llm_error_returns_none_after_retry(tmp_path, monkeypatch):
    calls = {"n": 0}

    def boom(self, system, user):
        calls["n"] += 1
        raise LLMError("mock 故障")

    monkeypatch.setattr(provider_mod.MockProvider, "complete_json", boom)
    items = [{"idx": 1, "number_prefix": "第一章", "title": "总则", "bold": True,
              "font_size": 16.0, "suggested_level": 1}]
    assert recover_structure(items, _settings(tmp_path)) is None
    assert calls["n"] == 2  # 首次 + 1 次重试


def test_invalid_output_retries_then_succeeds(tmp_path, monkeypatch):
    responses = [
        {"items": "不是列表"},  # 非法输出 -> 触发重试
        {"items": [
            {"idx": 1, "is_title": True, "level": 2},
            {"idx": 2, "is_title": False, "level": 3},
        ]},
    ]
    calls = {"n": 0}

    def flaky(self, system, user):
        r = responses[calls["n"]]
        calls["n"] += 1
        return r

    monkeypatch.setattr(provider_mod.MockProvider, "complete_json", flaky)
    items = [
        {"idx": 1, "number_prefix": "1.1", "title": "范围", "bold": False,
         "font_size": 14.0, "suggested_level": 2},
        {"idx": 2, "number_prefix": "", "title": "正文误入", "bold": False,
         "font_size": 12.0, "suggested_level": 3},
    ]
    out = recover_structure(items, _settings(tmp_path))
    assert calls["n"] == 2
    assert out is not None
    assert out[0] == {"idx": 1, "is_title": True, "level": 2}
    assert out[1] == {"idx": 2, "is_title": False, "level": None}  # is_title=False 置 None


def test_level_clamped_to_1_9(tmp_path, monkeypatch):
    def fake(self, system, user):
        return {"items": [
            {"idx": 1, "is_title": True, "level": 15},
            {"idx": 2, "is_title": True, "level": 0},
        ]}

    monkeypatch.setattr(provider_mod.MockProvider, "complete_json", fake)
    items = [
        {"idx": 1, "number_prefix": "a", "title": "t", "bold": True,
         "font_size": 12.0, "suggested_level": 1},
        {"idx": 2, "number_prefix": "b", "title": "u", "bold": True,
         "font_size": 12.0, "suggested_level": 1},
    ]
    out = recover_structure(items, _settings(tmp_path))
    assert out is not None
    assert [o["level"] for o in out] == [9, 1]
