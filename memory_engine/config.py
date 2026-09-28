# memory_engine/config.py
# 分层语义检索引擎的配置（所有阈值 / 路径 / 生命周期参数集中于此）。
import os


# 数据根目录：默认放在应用 runtime 下；可用环境变量 MEMORY_ENGINE_DATA 覆盖
def _default_data_dir():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, "runtime", "memory_engine")


DATA_DIR = os.environ.get("MEMORY_ENGINE_DATA") or _default_data_dir()


class MemoryPaths:
    """一个数据目录下的各层存储路径（实例级，避免存储层依赖全局配置）。

    引擎按实例持有 MemoryPaths：active / archive / cold / log / FTS 标记全部由 data_dir 推导。
    此前 data_dir 只被保存、未被使用（各层仍读全局路径），会导致「传入临时目录的实例」实际
    写进真实 runtime，测试数据污染生产数据。这里把路径收口到实例上。
    """

    __slots__ = ("data_dir", "active_db", "archive_db", "cold_dir", "log_file", "fts_marker")

    def __init__(self, data_dir: str = None):
        self.data_dir = os.path.abspath(data_dir or DATA_DIR)
        self.active_db = os.path.join(self.data_dir, "active", "memory.db")
        self.archive_db = os.path.join(self.data_dir, "archive", "memory.db")
        self.cold_dir = os.path.join(self.data_dir, "cold")
        self.log_file = os.path.join(self.data_dir, "memory_engine.log")
        self.fts_marker = os.path.join(self.data_dir, "last_fts_rebuild.txt")

    def ensure_dirs(self) -> None:
        for path in (self.active_db, self.archive_db):
            os.makedirs(os.path.dirname(path), exist_ok=True)
        os.makedirs(self.cold_dir, exist_ok=True)

    def same_as(self, other) -> bool:
        return bool(other) and os.path.normcase(self.data_dir) == os.path.normcase(other.data_dir)

    def as_dict(self) -> dict:
        return {"data_dir": self.data_dir, "active_db": self.active_db, "archive_db": self.archive_db,
                "cold_dir": self.cold_dir, "log_file": self.log_file, "fts_marker": self.fts_marker}


def resolve_paths(data_dir: str = None) -> MemoryPaths:
    """返回指定数据目录下的各层路径（不传则用默认数据目录）。"""
    return MemoryPaths(data_dir)


# ---- 数据文件布局（L2 索引层）：默认目录下的路径，兼容既有引用 ----
ACTIVE_DB = os.path.join(DATA_DIR, "active", "memory.db")        # 活跃主库（SQLite + FTS5 + 向量表）
ARCHIVE_DB = os.path.join(DATA_DIR, "archive", "memory.db")      # 归档历史库（季度滚动后落这里）
COLD_DIR = os.path.join(DATA_DIR, "cold")                        # L3 冷存储根目录

# ---- 语义切分 ----
SEMANTIC_CORE_MAX = 200        # 单个记忆片段语义核心上限（字）
RAW_CHUNK_MAX = 2000           # 原始长文本逻辑切分的参考块大小（超出按块切分）

# ---- L1 热缓存 ----
CACHE_TTL = 30 * 60            # 生命周期 30 分钟
CACHE_MAX_ITEMS = 512          # LRU 容量上限

# ---- L0 会话缓存（最近对话上下文，进程内临时缓存） ----
L0_MAX_ENTRIES = 20            # 最近 20 条对话回合作为 L0 上下文窗口
CONTEXT_MODE = "readwrite"     # 插件侧的默认上下文模式（引擎实例自身默认 readonly，需显式放开）
L0_HIT_MIN_SCORE = 2.0         # L0 命中视为“实质相关”的最低加权重合分（省略式追问时用于跳过联网）
L0_CONTEXT_MIN_SCORE = 1.5     # 注入 LLM 上下文时保留回合的最低加权重合分（话题切换时自动丢弃无关旧回合）

# ---- 检索 ----
TOP_K = 10                     # 每条路径返回 Top10
CANDIDATE_CAP = 200            # 硬过滤后候选集上限
DEFAULT_TIME_WINDOW_DAYS = 0   # 无显式时间词时的默认时间边界；0=不限时间（跨年度搜索，活跃+归档并查）
VEC_WEIGHT = 0.6               # 向量分权重
KEYWORD_WEIGHT = 0.4           # BM25 关键词分权重
ROLE_BOOST = 0.2               # 当前角色相关事件加分（判断层优先输出与当前角色相关的事件；支持角色列表）
VEC_CONFIRM_MIN = 0.06         # 弱匹配共识校验：仅靠 1 个 2-gram 命中（无 FTS 支持）时，向量分需 ≥ 该值才算有效命中
TIME_CONFIRM_BONUS = 0.12      # 时间-内容确认加成：候选文本含查询时间词（如 星期三→周三）时，弱匹配获得的结构性加分

# ---- 调试与日志 ----
DEBUG = False                  # 引擎调试模式（控制台输出检索过程）；运行日志始终写入文件
LOG_FILE = os.path.join(DATA_DIR, "memory_engine.log")

# ---- 置信度仲裁 ----
CONFIDENCE_GAP = 0.15          # Top1 - Top2 > 该值 → 直出
LOW_CONFIDENCE = 0.4           # Top1 < 该值 → 判定无相关记忆（返回空）
RECHECK_MIN_SCORE = 0.45       # 模糊场景 LLM 复核的最低 Top1 分：低于该值直接取 Top1，
                               # 避免低分噪声对也消耗 LLM（控制延迟与成本）

# ---- 向量 ----
VECTOR_DIM = 512               # 本地 n-gram 哈希向量维度（可替换为外部嵌入模型）
VECTOR_MEMORY_MAX = 20000      # 内存中驻留的向量条数上限（LRU，≈40MB@512维）；超出部分按需从库中惰性加载

# ---- 生命周期 ----
FTS_REBUILD_MARKER = os.path.join(DATA_DIR, "last_fts_rebuild.txt")  # FTS5 每日重建标记
LLM_RECHECK_ENABLED = True     # 模糊场景是否允许调用轻量级 LLM 复核
INJECT_REVIEW = True           # 注入复核：检索/联网内容注入上下文前用 LLM 判断相关性（200=注入 / 404=阻止）
REVIEW_CACHE_TTL = 60          # 注入复核结果同回合缓存秒数（避免同一内容被重复复核）
STALE_CONTEXT_TIMEOUT = 30 * 60  # 纠错机制：距上次活动超过该秒数视为新会话，自动清理 L0/L1 与镜像历史（0=关闭）
ARCHIVE_VALUE_CHECK = True     # 归档价值判断：只保留有长期记忆价值的回合（问候/寒暄等低信息量不归档）
LLM_VALUE_CHECK = False        # 归档价值判断是否启用 LLM 深度复核（每回合多一次判断模型调用，默认关闭）
ARCHIVE_DRAIN_TIMEOUT = 15.0   # 清空 / 关闭时等待后台归档队列的秒数上限

# ---- 存储治理（防止无限制消耗；数据只降级、不删除） ----
# 数量配额：超过上限时按“最近访问时间”把最旧记录先归档、再聚合
ACTIVE_MAX_ENTRIES = 20000      # 活跃库片段上限（超出部分迁入归档库）
ARCHIVE_MAX_ENTRIES = 100000    # 归档库片段上限（超出部分对最旧组执行聚合降级）
# 保留期（按季度计）：到期数据执行“降级保存”（聚合 / 压缩），绝不直接删除
ARCHIVE_MAX_QUARTERS = 12       # 超过该季度数 → 季度聚合（同 年/季度/主题 合并为一条）
AGGREGATE_YEAR_QUARTERS = 24    # 超过该季度数 → 年度聚合（同 年/主题 合并为一条）
RAW_RETENTION_QUARTERS = 8      # 超过该季度数 → L3 原文压缩为要点版（保留语义，不再存全文）
RAW_COMPACT_KEEP = 0.35         # 原文压缩保留比例（前 35% 要点 + 事实列表）
# 整理 / 分类
TIDY_SIMILARITY = 0.92          # 整理时判定“近似重复”的向量余弦阈值（越高越严格）
TIDY_MAX_GROUP = 60             # 整理时单组最大两两比较量（防止大组 O(n²)）
AUTO_MAINTAIN_INTERVAL = 1800   # 插件后台自动治理间隔（秒）
