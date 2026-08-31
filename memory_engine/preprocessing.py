# memory_engine/preprocessing.py
# 分级预处理：逻辑切分 → 硬过滤元数据提取 → 多角色事实拆解 → 双路检索数据生成。
from __future__ import annotations

import re
import time
import uuid

import memory_engine.config as cfg
from memory_engine.models import MemoryFragment, RoleFact
from memory_engine import time_utils
from memory_engine.text_utils import normalize_query_text

# 「角色名：内容」行格式（入库文本中常见），用于自动拆解角色与事实
_LINE_RE = re.compile(r"^\s*([^：:]{1,16})[：:]\s*(.+)$")
_PUNCT_RE = re.compile(r"[，。！？；、,.!?;:\s“”\"'()（）\[\]【】<>《》\-—_~·]+")
_CLAUSE_RE = re.compile(r"[，。；！？]")

# ---- 聊天场景主题词表（存储侧：入库时猜主题，可宽松；具体词在前，兜底词在后） ----
_TOPIC_LEXICON = [
    (("奶茶", "咖啡", "小笼包", "火锅", "烧烤", "甜品", "蛋糕", "好吃", "美食",
      "餐厅", "零食", "早餐", "午饭", "晚饭", "吃饭", "点外卖", "厨艺", "做饭", "食谱"), "美食"),
    (("爬山", "露营", "旅行", "旅游", "度假", "海边", "游乐园", "景点", "看雪", "雪人",
      "沙滩", "酒店", "民宿"), "旅行"),
    (("电影", "电视剧", "游戏", "打游戏", "唱歌", "KTV", "演唱会", "综艺", "动画", "追剧", "耳机"), "娱乐"),
    (("吉他", "画画", "摄影", "健身", "跑步", "日语", "学琴", "书法", "手工", "爱好"), "爱好"),
    (("学习", "上课", "课程", "考试", "作业", "备考", "考研", "复习", "培训班"), "学习"),
    (("生日", "庆祝", "惊喜", "聚会", "跨年", "烟花", "礼物", "玩偶", "朋友"), "社交"),
    (("体重", "减肥", "锻炼", "运动", "生病", "感冒", "医院", "睡眠", "健康"), "健康"),
    (("上班", "工作", "开会", "项目", "任务", "团建", "安排", "计划", "日程"), "日程"),
    (("回忆", "怀念", "想起", "以前", "曾经", "那时候"), "回忆"),
    (("散步", "逛街", "购物", "天气", "下雨", "公园", "快递", "日常", "约定", "约好", "一起"), "日常"),
]

# ---- 查询侧主题词（检索硬过滤剪枝用，务必保守：只收明确、具体、不易误伤的名词/动词） ----
# 泛词（如「一起/约/安排/以前」）不进入该表，避免把相关候选误剪掉。
_QUERY_TOPIC_LEXICON = [
    (("奶茶", "咖啡", "火锅", "烧烤", "甜品", "蛋糕", "小笼包", "美食", "好吃", "餐厅", "零食"), "美食"),
    (("爬山", "露营", "旅行", "旅游", "海边", "游乐园", "看雪", "沙滩", "度假"), "旅行"),
    (("游戏", "打游戏", "电影", "演唱会", "KTV", "唱歌", "综艺"), "娱乐"),
    (("吉他", "画画", "摄影", "日语", "健身", "跑步"), "爱好"),
    (("考试", "作业", "备考", "考研", "复习", "上课"), "学习"),
    (("生日", "跨年", "烟花", "聚会", "庆祝"), "社交"),
    (("回忆", "怀念"), "回忆"),
    (("体重", "减肥", "感冒", "生病", "医院"), "健康"),
    (("团建", "上班", "开会", "项目"), "日程"),
]

# 合法主题清单（记忆管理页面手工录入时校验用）
TOPIC_NAMES = sorted({m for _, m in _TOPIC_LEXICON} | {"综合"})


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def guess_topic(text: str) -> tuple:
    """按聊天场景主题词表猜测 (主主题, 副主题)。未命中返回 ("综合", "")。"""
    for words, m in _TOPIC_LEXICON:
        if any(w in text for w in words):
            return m, ""
    return "综合", ""


def query_topics(text: str) -> tuple:
    """查询侧主题匹配（保守词表）：返回 (main_topic, None)；未命中 (None, None) 不限制。"""
    for words, m in _QUERY_TOPIC_LEXICON:
        if any(w in text for w in words):
            return m, None
    return None, None


def extract_roles_and_facts(text: str, participants=None, facts_per_role=None) -> tuple:
    """多角色事实拆解。

    优先使用调用方显式给出的 participants / facts_per_role；
    否则从「角色名：内容」行格式中自动拆解，每行内容按 动作/结果/立场 做轻量归类。
    """
    if facts_per_role:
        roles = list(facts_per_role.keys())
        if participants:
            roles = list(dict.fromkeys(list(participants) + roles))
        facts = {}
        # 只为“有事实”的角色建条目（参与者里的其他角色只进 participants，
        # 不再产生空事实噪音，如归档回合的“用户 + 角色”组合）
        for r in facts_per_role.keys():
            fd = facts_per_role.get(r)
            if isinstance(fd, RoleFact):
                facts[r] = fd
            elif isinstance(fd, dict):
                facts[r] = RoleFact.from_dict(fd)
            elif isinstance(fd, str):
                facts[r] = RoleFact(raw=fd)
            else:
                facts[r] = RoleFact()
        return roles, facts

    # 自动拆解：按行解析「名字：内容」
    role_lines = {}
    for ln in text.splitlines():
        m = _LINE_RE.match(ln)
        if m:
            role = m.group(1).strip()
            content = m.group(2).strip()
            if content and len(role) <= 12:
                role_lines.setdefault(role, []).append(content)

    roles = list(role_lines.keys())
    if participants:
        # 参与者显式给出时，把未在文本中出现的角色也纳入
        for p in participants:
            if p not in roles:
                roles.append(p)

    facts = {}
    for role in roles:
        contents = role_lines.get(role, [])
        if not contents:
            facts[role] = RoleFact()
            continue
        joined = "；".join(contents)
        # 轻量归类（聊天场景）：
        #   立场：表态 / 偏好（想、喜欢、希望、拒绝、不想…）
        #   结果：已完成的事实（达成、学会、拍了、瘦了、买了、到了…）
        #   其余 → 动作
        fact = RoleFact(raw=joined[:120])
        if any(w in joined for w in (
                "否决", "收回", "反对", "拒绝", "不支持", "保守", "激进",
                "不想", "不要", "讨厌", "喜欢", "希望", "期待", "打算", "决定",
                "好呀", "好啊", "没问题", "可以", "超开心", "怀念")):
            fact.stance = joined[:80]
        if any(w in joined for w in (
                "导致", "造成", "下跌", "失败", "上升", "增长", "下滑", "翻倍",
                "达成", "完成", "成功", "学会了", "瘦了", "胖了", "到了", "买了",
                "拍了", "堆了", "收到", "练了", "去过")):
            fact.result = joined[:80]
        fact.action = joined[:80]
        facts[role] = fact
    return roles, facts


def build_searchable_text(main_topic, sub_topic, participants, facts_per_role, full_summary) -> str:
    """生成 searchable_text：纯关键词组合，专供 BM25 索引。"""
    parts = [main_topic or "", sub_topic or ""]
    parts += list(participants or [])
    for role, fact in (facts_per_role or {}).items():
        fd = fact.to_dict() if isinstance(fact, RoleFact) else dict(fact or {})
        parts.append(role)
        for key in ("action", "result", "stance"):
            v = str(fd.get(key, "") or "")
            # 按标点拆成短关键词片段（2~24 字，兼顾短句与长句的 BM25 trigram 命中）
            for seg in _CLAUSE_RE.split(v):
                seg = seg.strip()
                if 2 <= len(seg) <= 24:
                    parts.append(seg)
    # 从摘要里补充关键片段（2~24 字；聊天场景主题多样，不限定业务词表）
    for seg in _CLAUSE_RE.split(full_summary or ""):
        seg = seg.strip()
        if 2 <= len(seg) <= 24:
            parts.append(seg)
    seen = set()
    out = []
    for p in parts:
        p = _PUNCT_RE.sub("", str(p or "")).strip()
        p = normalize_query_text(p)   # 星期X/礼拜X → 周X，与查询侧规范化一致
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return " ".join(out)


def build_semantic_context(full_summary, facts_per_role, main_topic="", sub_topic="", limit=None) -> str:
    """生成 semantic_context：事件核心压缩（默认 ≤200 字），供向量嵌入。

    主题词前置，保证向量携带主题信号（主题也独立进入 searchable_text 供 BM25）。
    """
    limit = limit or cfg.SEMANTIC_CORE_MAX
    head = ""
    if main_topic or sub_topic:
        head = f"【{main_topic or '综合'}" + (f"/{sub_topic}" if sub_topic else "") + "】"
    parts = [full_summary or ""]
    for role, fact in (facts_per_role or {}).items():
        fd = fact.to_dict() if isinstance(fact, RoleFact) else dict(fact or {})
        bits = [role]
        for key in ("stance", "result", "action"):
            v = str(fd.get(key, "") or "").strip()
            if v and v not in bits:
                bits.append(v)
        parts.append("：".join(bits)[:limit])
    text = head + "；".join(p for p in parts if p)
    if len(text) > limit:
        text = text[:limit].rstrip("，；。 ")
    return text


def _chunk_raw(raw_text, limit) -> list:
    """把超长原始文本按段落逻辑切分为多个块（供多片段入库）。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", raw_text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 > limit and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = (cur + "\n" + p).strip()
    if cur:
        chunks.append(cur)
    return chunks or [raw_text]


def make_fragment(
    raw_text: str,
    fragment_id: str = None,
    ts: float = None,
    year: int = None,
    quarter: str = None,
    main_topic: str = None,
    sub_topic: str = None,
    participants: list = None,
    facts_per_role: dict = None,
    full_summary: str = None,
) -> MemoryFragment:
    """从一段文本构建一个 MemoryFragment（自动补全元数据与双路检索数据）。"""
    raw_text = (raw_text or "").strip()
    ts = ts if ts is not None else time_utils.now_ts()
    if year is None or quarter is None:
        auto_year, auto_q = time_utils.year_quarter(ts)
        year = year if year is not None else auto_year
        quarter = quarter if quarter else auto_q

    if not main_topic or not sub_topic:
        g_main, g_sub = guess_topic(raw_text)
        main_topic = main_topic or g_main
        sub_topic = sub_topic or g_sub

    roles, facts = extract_roles_and_facts(raw_text, participants, facts_per_role)

    summary = full_summary
    if not summary:
        # 摘要：取文本首段（去角色行前缀），≤200 字
        summary = raw_text[:cfg.SEMANTIC_CORE_MAX].strip()
        summary = _LINE_RE.sub(lambda m: m.group(2).strip(), summary)
        if len(summary) > cfg.SEMANTIC_CORE_MAX:
            summary = summary[:cfg.SEMANTIC_CORE_MAX].rstrip("，。； ")

    searchable = build_searchable_text(main_topic, sub_topic, roles, facts, summary)
    context = build_semantic_context(summary, facts, main_topic, sub_topic)

    return MemoryFragment(
        id=fragment_id or new_id(),
        year=year,
        quarter=quarter,
        ts=ts,
        main_topic=main_topic,
        sub_topic=sub_topic,
        participants=roles,
        facts_per_role=facts,
        searchable_text=searchable,
        semantic_context=context,
        full_summary=summary,
        raw_text=raw_text,
    )


def split_fragments(raw_text: str, **kwargs) -> list:
    """超长文本 → 多个 MemoryFragment（按 RAW_CHUNK_MAX 逻辑切分）。"""
    base_id = kwargs.pop("fragment_id", None)
    chunks = _chunk_raw(raw_text, cfg.RAW_CHUNK_MAX)
    frags = []
    for i, chunk in enumerate(chunks):
        fid = f"{base_id}_{i}" if base_id else None
        frags.append(make_fragment(chunk, fragment_id=fid, **kwargs))
    return frags
