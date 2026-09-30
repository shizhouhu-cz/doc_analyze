"""第四章处理流水线：特征粗筛 → 编号渲染 → 结构恢复 → 章节树 → 表格 → 落库。"""
from doc_analyze.pipeline.ingest import ingest_file

__all__ = ["ingest_file"]
