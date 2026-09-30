# DOCX标书结构化提取与查询系统技术方案（第一阶段·裁剪版）

## 一、方案总览

### 1.1 目标与边界

- **输入**：原生 DOCX 格式招投标文件
- **输出**：SQLite 结构化数据库，包含章节层级树（正文挂载于章节）、表格结构化数据、FTS5 全文索引
- **核心目标**：解决长文档上下文爆炸问题，通过「标题树导航 + 渐进式下钻 + 工具调用型 Agent」实现精准、低 token 的标书信息检索
- **阶段定位**：只做「解析 → 结构化落库 → Agent 查询」核心链路；零向量依赖、无摘要体系、无分块机制

### 1.2 核心设计原则

1. **OOXML 直读，不引入外部解析器**：python-docx 解包 + lxml 直读 OOXML，特征读取、编号渲染、表格还原、噪声清洗自研（决策依据见 2.2 的 Docling 能力缺口）；解析层无外部重量级依赖
2. **模型只做一件事**：解析侧唯一模型节点是「LLM 全局结构恢复」，文档级一次调用；禁止逐段判断、全文解析、全量切分
3. **保真大于通顺**：所有结论可溯源至章节路径；核心数据透传，不做二次改写
4. **渐进式下钻，token 有界**：每次工具调用只返回「当前章节正文 + 子章节目录」，下钻节奏由 Agent 控制，杜绝一次性返回大子树
5. **结构与语义分离**：后续扩展 PDF/OCR 时只换解析器，不动存储与查询层

### 1.3 第一阶段范围清单

| 保留（本阶段实现） | 砍掉（移入第九章扩展路线） |
| --- | --- |
| OOXML 直读解析（特征 + 编号 + 表格 + 清洗） | Docling 等外部解析器接入 |
| 瘦版编号渲染（编号文本拼回标题，不建树） | 完整编号还原建树（模式映射/一致性校验） |
| 轻量格式特征读取 + 候选标题粗筛 | 有效格式级联解析、格式规范度评分 |
| LLM 全局结构恢复（唯一模型节点） | 逐段小模型判断 |
| 表格结构化落库 + Markdown 渲染 | 语义块/规则分块、两级摘要、关键词抽取 |
| FTS5 全文索引（章节正文 + 章节标题） | 向量索引、Embedding、jieba/NLP 依赖 |
| LangGraph 工具调用 Agent（4 个工具） | 业务字段提取、语义混合召回 |
| 金标评测集 | 人工校验界面、任务队列、多文档并发 |

---

## 二、技术选型与开源库

### 2.1 选型总表

| 层 | 技术点 | 选型 | 说明 |
| --- | --- | --- | --- |
| 运行环境 | 语言/版本 | **Python 3.11+** | venv + pip 管理依赖 |
| 解析层 | DOCX → 结构化文档 | **python-docx + lxml**（OOXML 直读） | python-docx 解包文档流，lxml 直读 `w:document` / `styles.xml` / `numbering.xml` XML 树：特征读取、编号渲染、表格还原、噪声清洗全部自研；不引入 Docling（能力缺口见 2.2） |
| 存储 | 结构化存储 | **SQLite**（标准库 `sqlite3`，WAL 模式） | 零运维，不上 ORM |
| 存储 | 中文全文检索 | **SQLite FTS5 + trigram 分词器**（SQLite ≥ 3.34） | 原生支持中文子串匹配，无需分词插件 |
| Agent | 编排 | **LangGraph** | 工具调用循环 + SqliteSaver checkpoint + 多轮状态 |
| Agent | LLM 接入 | **OpenAI 兼容接口**（`langchain-openai`） | 本地 Ollama 与云 API 同一套代码切换 |
| 模型 | Agent / 结构恢复 | **Qwen3-4B-Instruct**（Agent）/ 强模型或 **Qwen3-8B-Instruct**（结构恢复） | 结构恢复为文档级 1 次调用，可用 API 级强模型（成本可控） |
| 校验 | Schema 校验 | **Pydantic v2** | 工具参数与 LLM 输出共用 |
| 服务入口 | CLI / API | **Typer**（CLI）+ **FastAPI**（服务与调试页） | `doc-analyze serve` 启动服务，`/` 内置本地调试台 |
| 质量 | 测试/评测 | **pytest** + 金标评测脚本 | 金标集与流水线同步建设 |

> 相比上一版移除：Docling（特征读取/outlineLvl/编号渲染三处能力缺口，见 2.2）、docx2python、jieba。

### 2.2 解析层职责边界（全部自研，及不引入 Docling 的决策依据）

| 职责 | 归属 | 说明 |
| --- | --- | --- |
| 文档解包、body 遍历（段落/表格阅读顺序） | 自研 | python-docx 解包，`body` 子节点顺序即阅读顺序 |
| 噪声清洗（TOC 域/空段/模板噪声） | 自研 | 跨段落域嵌套深度状态机 + 首尾关键词位置规则（见 4.1） |
| **轻量格式特征读取**（字号/加粗/缩进/outlineLvl/numPr） | 自研 | lxml 直读 pPr/rPr/样式继承链，粗筛五规则的全部输入（见 4.2） |
| **编号文本渲染**（不建树） | 自研 | numbering.xml 状态机（见 4.3 难点①） |
| **表格合并单元格还原** | 自研 | gridSpan/vMerge 占用矩阵还原逻辑网格，映射 Schema（见 4.5） |
| 章节树落库、索引、Agent | 自研 | 业务层 |

**不引入 Docling 的决策依据**（真实招标文件实测，2026-09）：

1. **层级信号失配**：Docling 的 DOCX 层级来自 Word 样式名（Heading/标题N）；实测招标文档样式名匹配 0 命中，`w:outlineLvl` 命中 148 处——唯一有效的标题信号 Docling 拿不到，其输出层级在该类文档上为空
2. **格式特征不暴露**：候选粗筛依赖字号半点、加粗三态、`w:ind` 首行缩进、`w:numPr` 等原始 OOXML 特征；Docling 的 `DoclingDocument` 只给语义 label，五条粗筛规则中四条无法运行
3. **编号不渲染**：numbering.xml 自动编号文本本就需要自研状态机（4.3）

三项相加，Docling 仅剩「遍历与阅读顺序」可用，而 python-docx 的 body 遍历已覆盖；为保留它需并行维护两套解析映射（Docling 元素 ↔ 源段落 id），成本大于收益，故整体不引入。

---

## 三、前置验证：解析器能力 Spike（已完成）

对真实招标文件《定稿-中国电信长沙分公司2026-2027年维护工程…采购项目.docx》实测（6.6 万字量级）：

1. **锚点覆盖率**：清洗后 1534 段落，outlineLvl 锚点 119（全部来自 outlineLvl，**样式名匹配 0 命中**）；锚点占比 7.8%，覆盖率 100%（119/119 锚点均给出建议层级）
2. **样式名与 outlineLvl 覆盖差值**：119 - 0 = 119，全部为「自定义样式/直接 pPr 标题」，验证 outlineLvl 信号是命脉（也是 2.2 不引入 Docling 的直接依据）
3. **待定项占比**：粗筛候选 864，其中待定 745（86%）——真实模型接入后 LLM 单趟调用需裁决约 745 行；mock 验证下锚点直接建树（119 章、24 表、0.65s）

后续接入新批次文档时复跑该统计（脚本化：特征读取 + 候选计数），锚点占比骤降即触发编号渲染与分段调用的参数复核。

---

## 四、处理流水线

```text
OOXML 直读解析 + 噪声清洗 → 轻量特征粗筛 → 瘦版编号渲染
    → 章节树构建（建议层级 ∪ LLM 全局恢复） → 表格落库 → FTS5 索引
```

### 4.1 遍历清洗（OOXML 直读）

- python-docx 解包，按 `body` 子节点顺序产出 `('p' | 'tbl')` 序列（阅读顺序）
- **TOC 域剔除**：`instrText`/`fldSimple` 含 TOC 的域段落，及其跨段落域结果区（域嵌套深度状态机一并剔除）
- 空段/全空白段丢弃；文档首尾各 3 段内的模板噪声（共 N 页/第 N 页/特此声明）按关键词+位置规则过滤
- 落库：`documents.doc_hash`（SHA-256 去重）、`status`、`total_chars`

### 4.2 轻量格式特征读取与候选标题粗筛

**不做完整样式级联解析**（原难点①裁撤）。粗筛是召回导向的，精度由 LLM 全局恢复兜底。

**读取特征**（python-docx + 有界 XML 查找）：

- run/段落直接格式：`w:sz`（半点）、`w:b`、`w:ind`
- 样式名：`w:pStyle` → 样式名含 Heading/标题
- **大纲级别**：`w:outlineLvl`（**0 起始**，0 = 一级），按「段落直接 pPr → 样式定义 → basedOn 链」有界查找（通常两三层）。该字段是 Word 导航窗格/TOC 的依据，可覆盖**自定义样式标题**（样式名不含「标题」但定义了大纲级别）

**候选入选**（满足任一，宁可多选不可漏选，目标过滤 90% 以上正文段落）：

- 有 outlineLvl
- 样式名匹配 Heading/标题类
- 有效加粗且段长 ≤ 50 字，**且字号/缩进至少一项与正文差异化**（字号 ≠ 众数基准，或缩进 ≠ 基准/顶格未设置）
- 字号 > 全文众数字号
- **编号 + 短行组合**：段首有编号特征 且 段长 ≤ 50 字，且**首行缩进 ≠ 基准缩进**（== 基准即排除——正文列表条目恰是「自动编号 + 2 字符首行缩进」形态，如宝兴文档「(五) 禁止参加本次采购活动的供应商」；排除后仅剩顶格/更小缩进的编号行，仍需满足结尾无句读标点 或 缩进 < 基准）。实测（宝兴）：该收紧再排 23 个候选（180→157），被质疑段落全部清出

**键值行负规则**（仅约束上述无属性路径，锚点不受影响）：冒号后跟 ≥4 字非空白内容（`[:：]\s*\S{4}`，如「项目编号：XCSSCG2026082001」「采购代理机构：xxx」）直接排除。实测（宝兴文档）：加粗规则命中的 89 个候选中 54 个是键值信息行，负规则清零且零误伤（「附件一：xxx」类真标题因带 outlineLvl 锚点保留）；结尾冒号的引导行（「采购项目简介：」）不受影响，交给 4.4 裁决。

**run 格式不一负规则**（同上，锚点免疫）：段内含文本 run 的**有效** (字号, 加粗) 组合数 >1 直接排除（run 未显式设置的属性回落段落/样式值，避免「显式 vs 继承同值」假阳性）。依据：标题视觉上单一格式，run 层多 run 常来自中英文字体拆分/rsid，**多 run 但格式统一的真标题不受影响**（长沙「第一章 招标公告」等 61 个多 run 锚点全部保留）；格式混杂是「标签加粗+值不加粗」键值行等复合信息行的特征。实测：真标题格式不一率 宝兴 0/119、长沙 1/119（该 1 个为锚点，免疫）。

> 编号**单独**不构成入选条件：正文列表项同样带编号且数量远超标题，仅凭编号会使候选集膨胀数倍、LLM 输入失控。组合中的缩进与结尾特征正是「标题编号 vs 列表编号」的经典判别维度（列表项悬挂缩进大、结尾常带冒号/分号），`w:ind` 本就在读取清单中，零额外成本；残余误入项由 4.4 的 `is_title` 裁决兜底。
> 加粗**单独**同样不构成入选条件（2026-09 收紧）：招标文件封面/前言的加粗信息行（键值行）大量混入候选会放大模型幻觉（宝兴文档实测「项目编号」被判为标题）；差异化要求保证「同字号只加粗」的三四级标题（缩进顶格）不漏召。

**建议层级判定（默认值，非硬约束，模型可纠偏，见 4.4）**：

| 优先级 | 依据 | 建议层级 |
| --- | --- | --- |
| 1 | outlineLvl 存在 | outlineLvl + 1 |
| 2 | 样式名 Heading/标题N | 样式映射层级 |
| 3 | 两者冲突，或均无但已入选 | 标待定，交模型裁决 |

### 4.3 瘦版编号渲染（难点①）

**只渲染编号文本，不做任何树构建/层级映射/一致性校验**——约为原编号模块 1/3 的工作量。

1. **状态机**：按文档顺序遍历，`numId → abstractNumId → lvl(ilvl 0~8)` 解析 `numFmt`/`lvlText`/`start`/`lvlRestart`/`lvlOverride`；维护 9 级计数器，高级别递增时低级别重置
2. **numFmt 转换器**（纯函数，逐一单元测试）：`decimal`、`chineseCountingThousand`（一…九百九十九）、`chineseLegalSimplified`（壹贰叁）、`ideographDigital`（一二三）、`ideographTraditional`（甲乙丙）、`lowerLetter/upperLetter`、`lowerRoman/upperRoman`、`decimalEnclosedCircle`（①②③）、`bullet`（无文本）
3. **渲染**：`lvlText` 模板占位符（`%1..%9`）替换为格式化计数值，得到编号文本（如「第二章」「2.1」），**拼回标题/条款文本**；段落无 `numPr` 时查样式链，`numId=0` 视为无编号
4. 价值：①区分同名标题（三个「项目概况」）②补全「第3.2条」类条款身份与交叉引用 ③为 LLM 的 is_title 裁决与层级判断提供最强信号

### 4.4 单趟全局结构恢复与章节树构建（难点②，本阶段核心）

**设计决策**：不做「规则树 ∪ 模型树」双支线合并（合并点是乱序与父子错挂的高发地），而是把粗筛产物压成**单一文档顺序序列**，一次模型调用统一定级，再走同一份确定性建树代码。

```text
输入（文档顺序单一序列，粗筛产出）：
  idx 1  [建议 level=1]  第一章 招标公告
  idx 2  [建议 level=2]  1.1 招标范围
  idx 3  [待定]          加粗、无编号、字号=正文
  ...
    ↓ 1 次调用/文档（temperature=0；候选行压缩：编号文本+标题截断+特征摘要，约 5k tokens）
LLM 对全部候选项输出：[{idx, is_title, level}]  ← is_title 剔除误入候选的正文段；层级均允许纠偏
    ↓ Pydantic 校验 + 规则复检
  ① 与建议层级偏离的项：保留模型结果，标 needs_review（可审计、不静默）
  ② 跳级修复：层级骤降（如 L2→L4）挂回最近合法祖先
  ③ 同编号模式同层级校验（软约束，违反仅标记不回滚）
    ↓ 文档顺序栈式拼树（与无模型降级路径同一份代码）
章节树（source: rule / llm，confidence、needs_review 随节点落库）
```

**建议层级 ≠ 硬约束**：排版与逻辑层级可能脱节——如「第一章」与「1.1」共用同一样式、排版同级，但编号语义上是父子。模型应依据**编号语义 + 文档上下文**纠正建议层级，锚点仅作默认值；偏离项落库时标记，不回滚。

**设计要点**：

- 模型任务是「整序列定级」（全局模式识别），非逐段局部判断；1 次/文档，模型档位可上浮（API 强模型成本几分钱）
- 编号文本（4.3）是模型裁决 is_title 与纠层级的最强依据；候选超多（>500 行）时按一级锚点分段送，段间靠锚点路径衔接
- **降级路径**：不配置模型时直接按建议层级建树，待定项并入就近锚点并标 `needs_review`，流水线照常完成
- **乱序在结构上不可能发生**：序列即文档顺序，模型只输出层级不输出顺序，建树为确定性栈操作

### 4.5 表格结构化

- lxml 直读 `w:tbl`：gridSpan/vMerge 占用矩阵还原逻辑网格（row_span/col_span），映射 `tables` / `table_cells`；嵌套表格拍平为独立表
- 表头识别：首行加粗/重复表头属性规则判断，无把握标 `is_header=0`
- **表格以 Markdown 渲染进所属章节正文**（保持阅读位置），`tables.order_num` 记录章节内顺序；字段映射留待扩展阶段

### 4.6 索引构建：FTS5 中文全文检索（难点③）

```sql
-- trigram 分词器原生支持中文子串匹配，无需中文分词插件
CREATE VIRTUAL TABLE chapters_fts USING fts5(content, tokenize = 'trigram');
CREATE VIRTUAL TABLE chapter_titles_fts USING fts5(title, tokenize = 'trigram');
```

- 建在**章节正文**上（非语义块——分块已裁撤）；流水线一次性写入，无增量同步负担
- 查询：`bm25()` 排序 + `snippet()` 截取命中片段，join 回 `chapters` 取路径与 `content_chars`
- 短查询兜底：trigram 要求 ≥3 字符，1~2 字关键词回退 `LIKE '%x%'`（章节级 LIKE 毫秒级）

---

## 五、SQLite Schema（第一阶段版）

### 5.1 文档主表

```sql
CREATE TABLE documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_name TEXT NOT NULL,
    doc_type TEXT DEFAULT 'docx',
    doc_hash TEXT UNIQUE,              -- SHA-256，去重
    status TEXT DEFAULT 'pending',     -- pending/processing/done/failed
    error_msg TEXT,
    total_chars INTEGER,
    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 5.2 章节结构表（核心表，正文唯一挂载点）

```sql
CREATE TABLE chapters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    parent_id INTEGER DEFAULT 0,
    level INTEGER NOT NULL,
    number_prefix TEXT,                -- 瘦版渲染的编号文本，如「第二章」「2.1」
    title TEXT NOT NULL,
    content TEXT,                      -- 本章正文（不含子章节），表格已渲染为 Markdown
    content_chars INTEGER,             -- 正文长度，下钻决策依据
    order_num INTEGER NOT NULL,
    chapter_path TEXT,                 -- 完整层级路径
    source TEXT DEFAULT 'rule',        -- rule / llm（模型纠偏或待定并入的节点标 llm）
    confidence REAL DEFAULT 1.0,
    needs_review INTEGER DEFAULT 0,
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);
CREATE INDEX idx_chapters_doc ON chapters(doc_id, order_num);
```

> 已删除：`semantic_blocks`（分块裁撤）、`chapter_summary`/`doc_summary`（摘要裁撤）、`format_score`（评分裁撤）、`total_pages`（DOCX 无页概念）。

### 5.3 表格相关表

```sql
CREATE TABLE tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    table_caption TEXT,
    total_rows INTEGER NOT NULL,
    total_cols INTEGER NOT NULL,
    order_num INTEGER,
    chapter_path TEXT,
    FOREIGN KEY (doc_id) REFERENCES documents(id)
);

CREATE TABLE table_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL,
    row_idx INTEGER NOT NULL,
    col_idx INTEGER NOT NULL,
    row_span INTEGER DEFAULT 1,
    col_span INTEGER DEFAULT 1,
    content TEXT NOT NULL,
    is_header INTEGER DEFAULT 0,
    FOREIGN KEY (table_id) REFERENCES tables(id)
);
CREATE INDEX idx_cells_table ON table_cells(table_id);
```

### 5.4 FTS 虚表

见 4.6：`chapters_fts`（章节正文）+ `chapter_titles_fts`（章节标题）。

---

## 六、查询 Agent（LangGraph）

### 6.1 编排设计

```text
StateGraph: agent(bind_tools) ⇄ tools 循环 → 汇总回答
    ├─ Checkpointer: SqliteSaver（多轮对话按 thread_id 持久化）
    ├─ System Prompt: 注入紧凑文档地图（每章一行：chapter_id|level|路径|标题）
    └─ 递归上限: 6 轮工具调用，防失控
```

文档地图为**纯标题树**（无摘要）：≤300 节点全量注入；超出则只注入一二级 + 子树计数，靠下钻获取明细。

### 6.2 工具集与渐进式下钻协议（难点④）

| 工具 | 功能 | 参数（Pydantic） |
| --- | --- | --- |
| `get_document_map` | 紧凑标题树 | `doc_id` |
| `get_chapter` | **当前章节正文（不含子章节）+ 子章节目录列表** | `doc_id`、`chapter_id`、`max_chars=4000`、`offset=0` |
| `search_content` | FTS5 关键词检索章节正文 | `doc_id`、`keywords`、`chapter_id?`、`top_k=5` |
| `find_chapter` | 章节标题模糊定位 | `doc_id`、`title_keywords` |

**下钻协议（写入 `get_chapter` 工具描述，为 Agent 行为约定）**：

```text
返回值结构：
    content:    当前章节自身正文（表格为 Markdown），不含子章节正文
    children:   [{chapter_id, level, chapter_path, title, content_chars}]
    truncated:  正文被截断时为 true，附 offset 续读提示

Agent 约定：
    1. children 为空 = 叶子章节，无需再下钻
    2. content_chars 大的子章节可优先取；仍太大则继续下钻更深层
    3. 禁止假设 content 包含子章节内容；需要细节必须按 chapter_id 再取
```

### 6.3 结果组装与 token 预算

- 核心数据透传不做改写；每条结果附 `chapter_path` 支持溯源到章节
- 单次工具返回上限约 3000 tokens：`get_chapter` 按 `max_chars` 截断 + `offset` 分页；`search_content` 返回 snippet 而非全文
- 回答引用 `chapter_path`，保证一键溯源
- 退化保护：若文档层级很平（单章正文即全文），`max_chars+offset` 分页兜底，配合 `search_content` 定位

---

## 七、性能指标（15 万字标准 DOCX）

| 环节 | 耗时 | 说明 |
| --- | --- | --- |
| OOXML 直读解析 + 清洗 | < 0.1 秒 | lxml 直读，无外部解析器 |
| 轻量特征读取 + 粗筛 | ~0.1 秒 | python-docx + lxml 单遍 |
| 瘦版编号渲染 | 1~2 秒 | 状态机 |
| 章节树构建 | ＜1 秒 | 栈式拼树 + 约束复检 |
| 表格落库 + Markdown 渲染 | 1~2 秒 | 视表格数量 |
| FTS5 索引 | ＜1 秒 | |
| **合计（零模型链路）** | **约 8~12 秒** | 无 GPU 依赖 |
| （可选）LLM 全局结构恢复 | +5~15 秒 | 1 次 LLM 调用 |

---

## 八、评测与验收

1. **金标集**：10~20 份真实标书，人工标注章节树（标题 + 层级 + 编号）；Spike 语料并入
2. **解析质量**：章节树 F1 ≥ 0.85（标题识别 + 父节点正确率）；模型偏离建议层级的占比与 `needs_review` 占比可观测
3. **检索质量**：`search_content` 人工抽评 Precision@5 ≥ 0.7
4. **端到端**：问答结果 100% 可溯源至 `chapter_path`；单次问答平均工具调用轮数 ≤ 4（下钻效率）
5. 全部指标脚本化（pytest 集成），规则或解析层改动跑回归

---

## 九、后续扩展路线（本阶段不实现）

1. **完整编号还原升级**：若 Spike 显示锚点覆盖率低，将瘦版渲染器升级为「编号→层级映射」（模式归一化 + 同模式同层级约束），降低 LLM 恢复负担
2. **核心业务字段提取**：资质/评分/废标三类字段表 + 表格语义映射 + 模型校验；`tables`/`table_cells` 已落库，直接消费
3. **向量检索上线**：章节内容 + 标题路径向量化（sqlite-vec + bge 系列），与 FTS5 混合召回；届时再评估是否引入细粒度分块
4. **摘要体系**：章节摘要（供文档地图压缩与预览），向量阶段配套
5. **原生 PDF 支持**：Docling PDF 管线或 MinerU，统一中间结构接入
6. **扫描件支持**：PaddleOCR 等 OCR 引擎
7. **工程化**：人工校验界面（消费 `needs_review`）、任务队列、多文档并发、增量更新
