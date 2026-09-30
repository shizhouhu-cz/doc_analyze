"""全局配置：config.toml < 环境变量 < 代码默认值（优先级从低到高）。

config.toml 位于项目根（可选，不存在时全部走默认值/环境变量）。
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Settings:
    db_path: Path
    llm_provider: str = "mock"  # mock | openai
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    # 完整 Authorization 头值（内网网关等非 Bearer 鉴权时使用，覆盖默认 "Bearer <key>"）
    openai_auth_header: str | None = None
    recovery_model: str = "qwen-plus"
    agent_model: str = "qwen-plus"
    max_agent_rounds: int = 6
    max_chars: int = 4000  # get_chapter 单次正文上限
    top_k: int = 5


def _read_toml() -> dict:
    cfg = _ROOT / "config.toml"
    if not cfg.exists():
        return {}
    with cfg.open("rb") as f:
        return tomllib.load(f)


def _pick(section: dict, key: str, env: str) -> str | None:
    """环境变量优先于配置文件项；空串/空白视为未配置。"""
    v = os.environ.get(env)
    if v is not None:
        return v.strip() or None
    v = section.get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def load_settings() -> Settings:
    toml = _read_toml()
    app: dict = toml.get("app", {})
    llm: dict = toml.get("llm", {})

    db = _pick(app, "db_path", "DOC_ANALYZE_DB") or "doc_analyze.db"
    db_path = Path(db)
    if not db_path.is_absolute():
        db_path = _ROOT / db_path

    return Settings(
        db_path=db_path,
        llm_provider=_pick(llm, "provider", "DOC_ANALYZE_LLM_PROVIDER") or "mock",
        openai_api_key=_pick(llm, "api_key", "OPENAI_API_KEY"),
        openai_base_url=_pick(llm, "base_url", "OPENAI_BASE_URL"),
        openai_auth_header=_pick(llm, "auth_header", "DOC_ANALYZE_OPENAI_AUTH_HEADER"),
        recovery_model=_pick(llm, "recovery_model", "DOC_ANALYZE_RECOVERY_MODEL") or "qwen-plus",
        agent_model=_pick(llm, "agent_model", "DOC_ANALYZE_AGENT_MODEL") or "qwen-plus",
        max_agent_rounds=int(app.get("max_agent_rounds", 6)),
        max_chars=int(app.get("max_chars", 4000)),
        top_k=int(app.get("top_k", 5)),
    )
