# memory_engine/time_utils.py
# 时间词解析与季度运算：把「去年Q3 / 上季度 / 最近30天」等转成可执行的时间边界。
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

import memory_engine.config as cfg
from memory_engine.text_utils import normalize_query_text


def now_ts() -> float:
    return time.time()


def year_quarter(ts: float) -> tuple:
    dt = datetime.fromtimestamp(ts)
    return dt.year, f"Q{(dt.month - 1) // 3 + 1}"


def quarter_range(year: int, quarter: str) -> tuple:
    """返回该季度 [start_ts, end_ts)。"""
    q = int(str(quarter).upper().replace("Q", ""))
    month = (q - 1) * 3 + 1
    start = datetime(year, month, 1)
    if q == 4:
        end = datetime(year + 1, 1, 1)
    else:
        end = datetime(year, month + 3, 1)
    return start.timestamp(), end.timestamp()


def prev_quarter(year: int, quarter: str) -> tuple:
    q = int(str(quarter).upper().replace("Q", ""))
    if q == 1:
        return year - 1, "Q4"
    return year, f"Q{q - 1}"


def quarter_key(year: int, quarter: str) -> str:
    return f"{year}/{quarter}"


def parse_time_range(text: str, now: float = None) -> dict:
    """解析用户输入中的显式时间词，返回时间边界与检索目标库。

    返回: {start, end, year, quarter, use_archive, all_time, mode}
      - mode: "active"（活跃库）/ "archive"（归档库）/ "both"（并查）
      - 支持：今天/当天/今日、明天/明日、后天、昨天/昨日、前天、
              星期X/周X/礼拜X（可带 上/下/这/本 前缀）、本周/上周/下周、
              本月/上个月/下个月、最近N天、今年/去年/前年（可带 Q1-Q4）、
              单独 Q1-Q4、X季度、上季度、显式年份
      - 早于当前季度的季度（如“今年Q2”在 Q3 时）自动查归档库；
        “今年”整年同时查活跃库与归档库。
    """
    now = now or now_ts()
    cy, cq = year_quarter(now)
    text = (text or "").strip()

    start = None
    end = None
    year = None
    quarter = None
    has_time_word = False
    time_text = ""   # 命中的时间词原文（供检索层做“时间-内容”确认，如 星期三→周三）

    def day_start(ts):
        dt = datetime.fromtimestamp(ts)
        return datetime(dt.year, dt.month, dt.day).timestamp()

    def week_start(ts):
        dt = datetime.fromtimestamp(ts)
        mon = dt - timedelta(days=dt.weekday())
        return datetime(mon.year, mon.month, mon.day).timestamp()

    def month_start(ts):
        dt = datetime.fromtimestamp(ts)
        return datetime(dt.year, dt.month, 1).timestamp()

    # 今天 / 当天 / 今日
    if re.search(r"今天|当天|今日", text):
        has_time_word = True
        time_text = "今天"
        start, end = day_start(now), now
    # 明天 / 明日
    elif re.search(r"明天|明日", text):
        has_time_word = True
        time_text = "明天"
        start, end = day_start(now) + 86400, day_start(now) + 2 * 86400
    # 后天
    elif re.search(r"后天", text):
        has_time_word = True
        time_text = "后天"
        start, end = day_start(now) + 2 * 86400, day_start(now) + 3 * 86400
    # 昨天 / 昨日
    elif re.search(r"昨天|昨日", text):
        has_time_word = True
        time_text = "昨天"
        start, end = day_start(now) - 86400, day_start(now)
    # 前天
    elif re.search(r"前天", text):
        has_time_word = True
        time_text = "前天"
        start, end = day_start(now) - 2 * 86400, day_start(now) - 86400
    # 星期X / 周X / 礼拜X（可带 上/下/这/本 前缀；如 星期三、周三、上周三、下周三）
    m = re.search(r"(上|下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])", text)
    if m and start is None:
        has_time_word = True
        time_text = normalize_query_text(m.group(0))   # 星期三/礼拜三 → 周三（与入库侧一致）
        wd = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}[m.group(2)]
        base = week_start(now) + (wd - 1) * 86400
        pre = m.group(1)
        if pre == "上":
            base -= 7 * 86400
        elif pre == "下":
            base += 7 * 86400
        start, end = base, base + 86400
    # 本周 / 这周 / 这个星期 / 这星期
    elif re.search(r"本周|这周|这个星期|这星期", text):
        has_time_word = True
        time_text = "本周"
        start, end = week_start(now), now
    # 上周 / 上个星期 / 上星期
    elif re.search(r"上周|上个星期|上星期", text):
        has_time_word = True
        time_text = "上周"
        start, end = week_start(now) - 7 * 86400, week_start(now)
    # 下周 / 下个星期 / 下星期
    elif re.search(r"下周|下个星期|下星期", text):
        has_time_word = True
        time_text = "下周"
        start, end = week_start(now) + 7 * 86400, week_start(now) + 14 * 86400
    # 本月 / 这个月 / 这月
    elif re.search(r"本月|这个月|这月", text):
        has_time_word = True
        time_text = "这个月"
        start, end = month_start(now), now
    # 上个月 / 上月
    elif re.search(r"上个月|上月", text):
        has_time_word = True
        time_text = "上个月"
        d = datetime.fromtimestamp(now)
        first = datetime(d.year, d.month, 1)
        prev_end = first.timestamp()
        if d.month == 1:
            prev_start = datetime(d.year - 1, 12, 1).timestamp()
        else:
            prev_start = datetime(d.year, d.month - 1, 1).timestamp()
        start, end = prev_start, prev_end
    # 下个月 / 下月
    elif re.search(r"下个月|下月", text):
        has_time_word = True
        time_text = "下个月"
        d = datetime.fromtimestamp(now)
        if d.month == 12:
            nxt_start = datetime(d.year + 1, 1, 1).timestamp()
            nxt_end = datetime(d.year + 1, 2, 1).timestamp()
        else:
            nxt_start = datetime(d.year, d.month + 1, 1).timestamp()
            nxt_end = datetime(d.year, d.month + 2, 1).timestamp()
        start, end = nxt_start, nxt_end

    # 最近 / 近 N 天（周 / 月）
    m = re.search(r"(?:最近|近|最近这|近这)\s*(\d+)\s*(天|日|周|星期|个月|月)", text)
    if m and start is None:
        has_time_word = True
        time_text = m.group(0)
        n = int(m.group(1))
        unit = m.group(2)
        days = n if unit in ("天", "日") else (n * 7 if unit in ("周", "星期") else n * 30)
        start = now - days * 86400
        end = now

    # 今年 / 去年 / 前年（可带季度：去年Q3）
    m = re.search(r"(前年|去年|今年|当年|去年那|去年底)\s*(?:的)?\s*(Q[1-4])?", text)
    if m and start is None:
        has_time_word = True
        yw = m.group(1)
        q = m.group(2)
        time_text = yw
        if yw in ("前年",):
            year = cy - 2
        elif yw in ("去年", "去年那", "去年底"):
            year = cy - 1
        else:  # 今年 / 当年
            year = cy
        if q:
            quarter = q.upper()
        if year is not None:
            if quarter:
                s, e = quarter_range(year, quarter)
                start, end = s, e
            else:
                s = datetime(year, 1, 1).timestamp()
                e = datetime(year + 1, 1, 1).timestamp()
                start, end = s, e

    # 单独出现 Q1-Q4（如“Q3去海边玩”）：按当前年
    if quarter is None and year is None and start is None:
        m = re.search(r"Q([1-4])", text.upper())
        if m:
            has_time_word = True
            quarter = f"Q{m.group(1)}"
            year = cy
            time_text = quarter
            s, e = quarter_range(year, quarter)
            start, end = s, e

    # “X季度 / 第X季度”（含 今年/去年/前年 前缀与中文数字，如“今年2季度”“四季度”）
    # 注意：年份前缀可能已被上面的“今年/去年”正则解析（quarter 为空）→ 在此补全季度。
    if quarter is None:
        m = re.search(r"(前年|去年|今年|当年)?\s*(?:第)?([1-4一二三四])\s*季度", text)
        if m:
            has_time_word = True
            time_text = m.group(0)
            _MAP = {"一": 1, "二": 2, "三": 3, "四": 4}
            qnum = _MAP.get(m.group(2)) if m.group(2) in _MAP else int(m.group(2))
            yw = m.group(1)
            if yw:
                year = {"前年": cy - 2, "去年": cy - 1, "今年": cy, "当年": cy}[yw]
            elif year is None:
                year = cy
            quarter = f"Q{qnum}"
            s, e = quarter_range(year, quarter)
            start, end = s, e

    # 上季度 / 上一季度
    if re.search(r"上季度|上个季度|上一季度", text) and start is None:
        has_time_word = True
        time_text = "上季度"
        year, quarter = prev_quarter(cy, cq)
        s, e = quarter_range(year, quarter)
        start, end = s, e

    # 显式年份，如“2024年”
    m = re.search(r"(20\d{2})\s*年?", text)
    if m and start is None:
        has_time_word = True
        year = int(m.group(1))
        time_text = m.group(1)
        s = datetime(year, 1, 1).timestamp()
        e = datetime(year + 1, 1, 1).timestamp()
        start, end = s, e

    all_time = not has_time_word and cfg.DEFAULT_TIME_WINDOW_DAYS == 0
    if not has_time_word and not all_time:
        start = now - cfg.DEFAULT_TIME_WINDOW_DAYS * 86400
        end = now

    # 检索目标库：active / archive / both
    mode = "active"
    if year is not None:
        if quarter:
            mode = "archive" if (year, quarter) < (cy, cq) else "active"
        else:
            mode = "archive" if year < cy else ("both" if year == cy else "active")
    else:
        mode = "both" if all_time else "active"

    return {
        "start": start,
        "end": end,
        "year": year,
        "quarter": quarter,
        "mode": mode,
        "use_archive": mode == "archive",
        "all_time": all_time,
        "time_text": time_text,
    }


def extract_topics(text: str) -> tuple:
    """从输入中匹配主主题标签（用于硬过滤剪枝）。

    只认聊天场景的明确主题词（美食/旅行/娱乐/爱好/学习/社交/回忆/健康/日程），
    避免「一起/约/安排/以前」等泛词过度约束；返回 (main_topic, None)；未命中返回 (None, None)。
    """
    from memory_engine.preprocessing import query_topics   # 惰性导入，避免循环依赖
    return query_topics(text)
