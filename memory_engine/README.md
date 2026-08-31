# memory_engine —— 面向多角色长文本的分层语义检索引擎

> 职责边界：**纯检索器**。不判断"谁该发言"，只做一件事——根据用户输入毫秒级返回最相关的
> 历史记忆片段，并附带该片段内按角色拆解好的 `facts_per_role` 事实立场字典，供上层"接话"
> 流水线直接取用。

## 目录结构

```
memory_engine/
├── __init__.py          # MemoryEngine 进程内单例 + 对外接口（get_engine）
├── config.py            # 全部阈值 / 路径 / 生命周期 / 存储治理参数
├── models.py            # 数据模型与输出契约（MemoryFragment / RoleFact / RetrievedMemory）
├── time_utils.py        # 时间词解析（去年Q3 / 上季度 / 最近30天）+ 季度运算 + 主题标签
├── preprocessing.py     # 分级预处理：切分 → 元数据 → 角色事实拆解 → 双路检索数据
├── embedding.py         # 向量化（默认本地 n-gram 哈希；set_embedder() 预留真实嵌入）
├── ingest.py            # 写入流程：预处理 → L3 落盘 → L2 索引（主表+FTS+向量）
├── retrieval.py         # 检索四步法：L1 短路 → 硬过滤 → 双路并行 → 置信度仲裁
├── governance.py        # 存储治理：空间统计 / 配额淘汰 / 原文保留 / 整理去重 / 分类索引
├── llm_recheck.py       # 模糊场景轻量级 LLM 复核（可插拔，失败自动回退 Top1）
└── storage/
    ├── l1_cache.py      # L1 热缓存：LRU + 30 分钟 TTL（输入哈希 → 完整结果）
    ├── l2_sqlite.py     # L2 SQLite：结构化主表（B-Tree）+ FTS5（BM25/trigram）+ 向量 BLOB 表 + 分类表
    ├── l2_vector.py     # L2 向量检索：numpy 平面索引（lancedb 可用时自动切换，接口预留）
    ├── l3_cold.py       # L3 冷存储：年/季度/主题 目录，pyarrow Parquet（缺失时 JSONL 兜底）
    └── archive.py       # 季度滚动归档：活跃库 → 归档历史库（行+向量+FTS 一并迁移）
```

## 分层物理存储

| 层 | 载体 | 内容 | 生命周期 |
| --- | --- | --- | --- |
| L0 会话缓存 | 内存（进程内） | 最近 20 条对话回合（**会话上下文内容**，喂给大模型） | 上下文调用时优先取用；进程结束销毁 |
| L1 热缓存 | 内存 LRU | 输入哈希 → **检索结果缓存**（相同查询 30 分钟内去重） | 30 分钟自动淘汰 |
| L2 核心索引 | SQLite(+FTS5) + 向量 | 结构化元数据、searchable_text、semantic_context 向量 | 季度滚动归档至归档库 |
| L3 原始冷存 | 磁盘文件 | 年/季度/主题 目录下的原始全文（Parquet/JSONL） | 常驻，检索不触碰 |

> **L0 与 L1 不重复**：L0 存的是对话回合**内容**（上下文），L1 存的是检索**结果**（缓存）。
> L0 不参与记忆检索（检索统一走 L1/L2/L3）；L0 的会话命中由上层"上下文检测"直接判断。

数据默认位于 `runtime/memory_engine/`，可用环境变量 `MEMORY_ENGINE_DATA` 覆盖。

## 上下文统一管理（L0 + 双模式）

**所有上下文需求（单个角色、多人对话）统一接入引擎管理**，放弃旧的“最早上下文管理模式”：

- **生成前（组装上下文）**：`engine.assemble_context()` —— 优先取 L0 最近 20 条对话回合，
  再检索 L1/L2/L3 相关长期记忆（跨年度），最后拼入当前输入；
- **生成后（记录回合）**：`engine.record_turn()` 按当前模式处理：
  - `readwrite`（完整权限）：回合写入 L0 **并归档到 L2**（生成后记录归档，供长期记忆检索）；
  - `readonly`（只读）：回合**仅写入 L0 临时缓存**，不归档、不做任何后续操作，进程结束即销毁。

两种模式可随时切换（插件设置 `上下文使用模式` 或命令 `/memory mode readwrite|readonly`）。
`config.conversation_history` 保留为只读镜像（兼容角色切换 / 用户信息等既有消费者）。

## 启停自动整理

- 每次运行开始（插件启用 / 应用启动）：`on_run_start()` 清理 L0 残留 + 执行存储治理；
- 每次运行结束（插件停用 / 应用退出）：`on_run_end()` 销毁 L0（只读模式不归档） + 执行治理；
- 治理会把“超时的上下文历史”移入下一层（归档 → 季度聚合 → 年度聚合 → 原文压缩）。

## 检索四步法（全程无发言者判断，支持跨年度）

1. **L1 缓存闪电短路**：输入哈希命中 → 直接返回缓存结果（~0ms，跳过全部步骤）。
2. **L2 硬过滤**：解析时间词（无时间词时**不限时间**，活跃库 + 归档库并查，实现跨年度搜索），
   按时间边界 + 最近优先窗口剪枝至 ≤200 候选。
   注：不再用主题标签做硬剪枝——同一事件在存储侧可能归入别的主题，主题过滤会盲目剪掉
   正确候选；主题信号由双路打分承担。
3. **双路并行召回**：路径 A 向量点积（向量索引同时覆盖活跃库与归档库）+ 路径 B FTS5/BM25（跨库合并），
   并集后 `Base_Score = 0.6*S_vec + 0.4*S_keyword`（归一化）。
4. **置信度仲裁**：
   - Top1 - Top2 > 0.15 → 高置信直出（不碰 LLM）；
   - ≤ 0.15 → 模糊冲突，轻量级 LLM 二选一复核（只输出 ID，Token 极短；失败回退 Top1）；
   - Top1 < 0.4 → 低置信，直接返回空（不调用 LLM，由上层按年度宏观摘要兜底）。

## 上层调用顺序（避免上下文冲突）

`pipeline.process_message` 的处理顺序：

1. **插件**（含音乐插件，歌曲关键词最先被拦截）；
2. **歌曲检测**（置于最前）：歌曲关键词触发 → 跳过联网与上下文检测，进入歌曲播放检测；
   LLM 复核（confirm_music_intent）失败 → 回到原流程；
3. **上下文检测**（`_context_hit`）：长期记忆命中 → 直接用上下文回答，**跳过联网**；
   省略式追问（如“那周六呢”）且 L0 有实质相关内容 → 延续对话，也跳过联网；
   仅与 L0 泛用词重合（如“昨天有什么新闻吗”碰上“今天有什么安排吗”）不再算命中；
4. **联网检测**：上下文无命中（或显式"上网查"）→ `judge_need_online`（通用原则裁决，
   含“延续之前的实时查询→需要联网”“延续对话/问记忆安排→无需联网”）→ 联网搜索注入；
   联网时 `call_ollama` 不再注入记忆块（单一来源）。

## 输出契约（RetrievedMemory）

```python
{
  "id": "...", "full_summary": "200字以内事件核心",
  "participants": ["张三", "李四"],
  "facts_per_role": {"张三": {"action": "...", "result": "...", "stance": "..."}, "李四": {...}},
  "base_score": 0.0~1.0, "confidence": 0.0~1.0, "route": "l1|direct|llm_recheck|none",
  "candidates": [...], "raw_text": "原始全文（仅在 include_raw=True 时从 L3 读取）"
}
```

上层拿到结果后：`full_summary` 分配给第一位发言人，`facts_per_role["李四"]` 分配给接话的李四。

## 对外接口（预留）

```python
import memory_engine
engine = memory_engine.get_engine()          # 单例
engine.init() / engine.close()               # 启停（数据保留在磁盘）

# 存储（预留）
engine.ingest(raw_text, participants=..., facts_per_role=..., year=..., quarter=..., ...)
engine.store_record({"main_topic": ..., "participants": [...], "facts_per_role": {...}}, raw_text=...)

# 检索
engine.search(user_input, include_raw=False) # → RetrievedMemory（无时间词时跨年度全层检索）

# 上下文统一管理（L0 + 双模式）
engine.set_mode("readwrite") / engine.get_mode()   # readwrite=完整权限 | readonly=只读
engine.assemble_context(user_input)                # 生成前：L0 最近回合 + 长期记忆
engine.record_turn(user_text, reply, meta={...})   # 生成后：按模式记录（读写→归档 / 只读→仅L0）
engine.clear_context()                             # 清空 L0（切换角色时调用）
engine.on_run_start() / engine.on_run_end()        # 启停自动整理（清理 L0 + 治理）

# 存储治理（防止无限消耗）
engine.usage()                               # 各层空间统计（条数 / 磁盘占用）
engine.govern(max_active=..., max_archive=..., raw_retention_quarters=..., archive_retention_quarters=...)
                                             # 一键治理：季度归档 + 配额淘汰 + 原文保留 + FTS 重建

# 整理 / 分类（预留接口）
engine.tidy_records(threshold=0.92)          # 整理：近似重复检测并合并
engine.classify_records()                    # 分类：重建 主题/季度/参与者 索引
engine.list_categories(cat_type="topic")     # 分类清单
engine.records_in_category("participant", "李四")  # 按分类取记录

engine.archive() / engine.rebuild_fts() / engine.maybe_rebuild_fts()
engine.status() / engine.clear_all()
```

## 存储治理机制（数据只降级、不删除）

面向以年为单位的超长跨度上下文：任何记录都不会被直接删除，而是按时间逐级“降级保存”，
要点（参与者 / 事实立场 / 摘要）长期留存，存储空间有界。

| 机制 | 说明 | 参数（config.py / 插件设置可覆盖） |
| --- | --- | --- |
| 活跃库配额 | 超过上限时，按“最近访问时间”把最旧记录迁入归档库（无损） | `ACTIVE_MAX_ENTRIES`（默认 20000） |
| 归档库配额 | 超过上限时，对最旧季度执行聚合降级（跨主题/跨季度合并）直至达标 | `ARCHIVE_MAX_ENTRIES`（默认 100000） |
| 季度聚合 | 超过保留期的 同 年/季度/主题 记录合并为一条（参与者并集、facts_per_role 按角色拼接） | `ARCHIVE_MAX_QUARTERS`（默认 12） |
| 年度聚合 | 超过更长保留期的 同 年/主题 记录合并为年度摘要 | `AGGREGATE_YEAR_QUARTERS`（默认 24） |
| 原文压缩 | 超过保留期的 L3 原文压缩为要点版（前 35% + 事实列表，索引与摘要保留） | `RAW_RETENTION_QUARTERS`（默认 8）/ `RAW_COMPACT_KEEP` |
| 整理去重 | 组内向量余弦 ≥ 阈值判定近似重复，合并 facts_per_role 与参与者 | `TIDY_SIMILARITY`（默认 0.92） |
| L1 缓存淘汰 | LRU + 30 分钟 TTL | `CACHE_TTL` / `CACHE_MAX_ITEMS` |
| 自动治理 | 插件启用期间后台每 30 分钟执行一次 govern | `AUTO_MAINTAIN_INTERVAL` |

检索命中会更新记录的 `last_access`，作为“迁归档 / 优先聚合”的次序依据（常用记忆保持高保真，
冷门记忆先降级）。聚合片段带 `【季度聚合】/【年度聚合】` 标记，跨年检索（如“去年Q3”）仍可按
时间 + 主题命中聚合结果。

## 可插拔点

- `embedding.set_embedder(fn)`：替换向量化（如接入 Ollama /api/embeddings 或外部 API）。
- `l2_vector`：lancedb 安装后自动切换后端（当前为 numpy 平面索引，接口不变）。
- `l3_cold`：pyarrow 可用时用 Parquet，否则 JSONL 兜底（接口不变）。
- `llm_recheck.recheck_binary`：模糊复核默认调用应用内判断模型，可整体替换。

## 调试输出与运行日志

- **运行日志**：始终写入 `runtime/memory_engine/memory_engine.log`（文件），记录关键节点运行结果；
- **调试模式**：控制台额外打印检索全过程，便于开发者使用。开启方式任选：
  - 应用「设置 → 调试模式」开启后**自动联动**（引擎检测 `config.DEBUG_MODE`）；
  - 插件设置「引擎调试模式」开关，或命令 `/memory debug on|off`。

调试输出包含的关键内容：

```
[检索] 输入: '星期三晚上去哪玩' | include_raw=False | 角色=洛天依
[检索] 时间解析: 年度=None 季度=None 模式=active 缓存键=…（星期X 已解析为本周三窗口）
[检索] 硬过滤: 来源库=L2-活跃库 条件(时间=[本周三0点, 本周四0点], topic=不限) → 候选 1 条
[检索] 双路召回: 向量Top5=[…] / BM25 Top5=[…] / 综合Top5=[…]
[检索] 角色加成: 角色=洛天依 +0.2 → …（参与者以 {char} 占位符存储，改名预设仍可命中）
[检索] 仲裁: Top1=0.62 - Top2=0.20 = 0.42 > 差距阈值 0.15 → 情况A 高置信直出（不碰LLM）
[检索] 结果: id=… 摘要=… 置信度=0.62 route=direct 来源=L2-活跃库   ← 来源 + 置信度
[上下文] L0查询（来源L0）: 取最近 N 条回合 / 长期记忆（来源L1/L2/L3）…
[回合记录] 模式=readwrite L0=2/20 归档=True 挤出=0 条
[治理] govern 完成: 归档=… 迁入, 季度聚合=…, 年度聚合=…, 原文压缩=…, FTS=… 行
```

## 聊天场景与角色名约定

- **主题体系**：内置聊天场景主题（美食/旅行/娱乐/爱好/学习/社交/回忆/健康/日程/日常），
  入库时按词表猜测主题，查询侧只认明确主题词做硬过滤剪枝，避免泛词误剪。
- **角色名占位符**：写入侧把当前角色名替换为 `{char}` 占位符存储（参与者/事实/摘要/原文），
  读取侧渲染回当前角色名——用户自由修改角色预设（改名）不会丢失历史记忆，检索加成与展示均自动跟随。
- **时间词**：支持 今天/明天/后天/昨天/前天、星期X/周X/礼拜X（可带上/下/这/本前缀）、
  本周/上周/下周、本月/上/下个月、最近N天、今年/去年/前年（可带季度）等，全部按当前时间动态翻译；
  省略式追问（如“星期三呢”）自动带上上一轮用户话术做语义扩展，时间窗口仍以本句为准。

## 与主程序的关系

- 引擎完全独立，不修改主程序任何现有逻辑；
- 由插件 `plugins/memory.py`（上下文记忆库）负责启用 / 停止引擎生命周期；
- 插件不挂接消息钩子，不参与发言者判断，不影响其他功能。
