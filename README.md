# doc-analyze

DOCX 招标/标书文件 → 结构化章节知识库（SQLite + FTS5）→ LangGraph Agent 智能问答。

一条流水线完成「解析 → 结构恢复 → 入库 → 检索问答」，专为中文招标文件设计，全程离线可跑（LLM 可切真实模型）。

## 核心功能

### 1. DOCX 结构化提取流水线

| 步骤 | 说明 |
| --- | --- |
| 遍历清洗 | OOXML 直读（python-docx + lxml），TOC 域剔除（跨段落域状态机）、空段与模板噪声过滤 |
| 特征粗筛 | 逐段读取字号/加粗/首行缩进/`outlineLvl`/`numPr`，五条召回导向规则筛出候选标题（宁多勿漏） |
| 编号渲染 | numbering.xml 状态机还原自动编号文本（`2.1` / `3.4.2`），只渲染不建树 |
| LLM 结构恢复 | 候选序列单趟送模型裁决 `[{idx, is_title, level}]`，Pydantic 严格校验；超 450 行按一级锚点分段调用；失败自动降级为规则建树 |
| 建树与复检 | 确定性栈式建树，偏离标 `needs_review`、跳级钳制 |
| 表格还原 | `gridSpan`/`vMerge` 占用矩阵还原合并单元格，Markdown 化并入章节正文 |
| 索引 | FTS5 + trigram 中文子串检索（正文/标题双虚表），bm25 排序 + snippet 截片 |

### 2. LangGraph 检索问答 Agent

4 个工具由模型自主编排调用，渐进式下钻控制 token：

- `get_document_map` — 文档章节地图（超 300 章节自动折叠低层级）
- `get_chapter` — 章节正文 + 子章节目录，模型判断是否继续下钻
- `search_content` — FTS5 全文检索（<3 字符自动回退 LIKE）
- `find_chapter` — 章节标题定位

### 3. Web 调试台

内置单文件调试页（无外部依赖）：文档上传/切换、章节树（缩进 + `needs_review` 标记）、章节正文查看、Agent 问答与工具调用轨迹展示。

## 快速开始（本地）

环境要求：Python 3.11+（使用内置 `tomllib`）。

```powershell
# 1. 创建虚拟环境并安装
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\pip install pytest          # 跑测试需要

# 2. 解析文档（默认 mock 模型，无需任何 API Key）
.venv\Scripts\doc-analyze ingest 定稿-xxx.docx
# → {"doc_id": 1, "chapters": 119, "tables": 24, "elapsed_seconds": 0.65, ...}

# 3. 提问 / 查看结构
.venv\Scripts\doc-analyze query 1 "履约保证金的比例是多少"
.venv\Scripts\doc-analyze tree 1
.venv\Scripts\doc-analyze stats 1

# 4. 启动服务 + 调试台（浏览器打开 http://127.0.0.1:8000/）
.venv\Scripts\doc-analyze serve
```

重复 `ingest` 同一文件会复用文档行、清旧数据强制重跑（不产生重复文档）。

## 配置

三级优先级：**环境变量 > config.toml > 代码默认值**。配置文件在项目根 `config.toml`：

```toml
[app]
db_path = "doc_analyze.db"   # 相对路径基于项目根
max_agent_rounds = 6         # Agent 最大工具循环轮数
max_chars = 4000             # get_chapter 单次正文上限
top_k = 5                    # search_content 返回条数

[llm]
provider = "mock"            # mock | openai
# --- ChatGPT / DashScope（Bearer 鉴权）---
# provider = "openai"
# api_key = "sk-..."
# base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
# --- 内网网关（裸 token 鉴权）---
# provider = "openai"
# base_url = "http://192.168.x.x:port/gateway/xxx/v1"   # 须以 /v1 结尾
# auth_header = "anvil_xxx"                             # 完整 Authorization 头值
# recovery_model = "qwen"
# agent_model = "qwen"
```

对应环境变量（临时覆盖用）：`DOC_ANALYZE_DB`、`DOC_ANALYZE_LLM_PROVIDER`、`OPENAI_API_KEY`、`OPENAI_BASE_URL`、`DOC_ANALYZE_OPENAI_AUTH_HEADER`、`DOC_ANALYZE_RECOVERY_MODEL`、`DOC_ANALYZE_AGENT_MODEL`。

LLM 未配置或调用失败时自动降级：结构恢复用建议层级建树，问答用 mock 模型。

## 远程部署

### 方式一：直接 uvicorn

```bash
pip install . && uvicorn doc_analyze.server:app --host 0.0.0.0 --port 8000
```

### 方式二：systemd（Linux 常驻）

```ini
# /etc/systemd/system/doc-analyze.service
[Unit]
Description=doc-analyze service
After=network.target

[Service]
WorkingDirectory=/opt/doc_analyze
ExecStart=/opt/doc_analyze/.venv/bin/uvicorn doc_analyze.server:app --host 0.0.0.0 --port 8000
Restart=on-failure
Environment=DOC_ANALYZE_LLM_PROVIDER=openai

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now doc-analyze
```

### 方式三：Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
EXPOSE 8000
CMD ["uvicorn", "doc_analyze.server:app", "--host", "0.0.0.0", "--port", "8000"]
```

```bash
docker build -t doc-analyze .
docker run -d -p 8000:8000 \
  -e DOC_ANALYZE_LLM_PROVIDER=openai \
  -e DOC_ANALYZE_OPENAI_AUTH_HEADER=anvil_xxx \
  -e OPENAI_BASE_URL=http://192.168.x.x:port/gateway/xxx/v1 \
  -v doc_data:/data -e DOC_ANALYZE_DB=/data/doc_analyze.db \
  doc-analyze
```

部署注意事项：

- 数据库默认落项目根 `doc_analyze.db`（WAL 模式），容器部署务必用 `-v` 挂载持久化（或 `DOC_ANALYZE_DB` 指到挂载卷）
- config.toml 与环境变量二选一即可；容器内建议全走环境变量
- 内网网关（192.168.x.x）要求部署机与网关同网段可达
- SQLite 单文件库适合单实例部署；多实例/高并发场景需换 PostgreSQL（当前阶段不在范围内）

## HTTP API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 调试台页面 |
| GET | `/api/documents` | 文档列表（含章节/表格计数） |
| POST | `/api/ingest` | 上传 DOCX 入库（multipart，字段 `file`） |
| GET | `/api/tree/{doc_id}` | 平铺章节树 |
| GET | `/api/chapters/{id}` | 章节详情（含正文） |
| GET | `/api/stats/{doc_id}` | 文档统计 |
| POST | `/api/query` | Agent 问答 `{"doc_id": 1, "question": "..."}`，返回 answer + 工具调用轨迹 |

## 测试

```bash
pytest -q                # 全量（含真实文件 e2e，需项目根下的测试 DOCX）
pytest -m "not local"    # 跳过依赖本地文件的用例
```

## 项目结构

```text
src/doc_analyze/
├── config.py            # 配置（config.toml + env，三级优先级）
├── db.py                # SQLite Schema + FTS5 虚表
├── cli.py               # Typer CLI：ingest / query / tree / stats / serve
├── server.py            # FastAPI 服务 + 调试台
├── static/debug.html    # 单文件调试页
├── llm/
│   ├── provider.py      # Mock / OpenAI 兼容（Bearer 与裸 token 网关）
│   └── recovery.py      # 单趟全局结构恢复（Pydantic 校验 + 重试）
├── pipeline/
│   ├── features.py      # 特征读取与候选粗筛（五规则）
│   ├── numbering.py     # numbering.xml 瘦版编号渲染
│   ├── treebuild.py     # 复检 + 栈式建树
│   ├── tables.py        # gridSpan/vMerge 表格还原
│   └── ingest.py        # 流水线编排（含 >450 行分段调用）
└── agent/
    ├── tools.py         # get_document_map / get_chapter / search_content / find_chapter
    └── graph.py         # LangGraph 图（SqliteSaver checkpoint）
tests/                   # pytest（numbering/features/tables/treebuild/e2e/recovery/agent）
```

详细设计见 [design.md](design.md)。
