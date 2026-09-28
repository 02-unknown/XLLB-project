# memory_engine/storage/l2_sqlite.py
# L2 核心索引层（SQLite 部分）：结构化元数据主表 + FTS5 虚拟索引（BM25） + 向量 BLOB 表。
# 与 l2_vector.py（向量检索）通过 id 强关联，物理文件独立。
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time

import memory_engine.config as cfg

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fragments (
    id TEXT PRIMARY KEY,
    year INTEGER NOT NULL,
    quarter TEXT NOT NULL,
    topic TEXT NOT NULL DEFAULT '综合',
    sub_topic TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    participants TEXT NOT NULL DEFAULT '[]',
    facts_per_role TEXT NOT NULL DEFAULT '{}',
    searchable_text TEXT NOT NULL DEFAULT '',
    semantic_context TEXT NOT NULL DEFAULT '',
    full_summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_frag_year_q ON fragments(year, quarter);
CREATE INDEX IF NOT EXISTS idx_frag_topic ON fragments(topic);
CREATE INDEX IF NOT EXISTS idx_frag_ts ON fragments(ts);
CREATE TABLE IF NOT EXISTS vectors (
    id TEXT PRIMARY KEY,
    vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id TEXT NOT NULL,
    cat_type TEXT NOT NULL,
    cat_value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cat ON categories(cat_type, cat_value);
"""


def _fts_supported(con) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        con.execute("DROP TABLE _fts_probe")
        return True
    except Exception:
        return False


def _fts_trigram(con) -> bool:
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe2 USING fts5(x, tokenize='trigram')")
        con.execute("DROP TABLE _fts_probe2")
        return True
    except Exception:
        return False


_FTS_QUERY_CLEAN = re.compile(r'[^\u4e00-\u9fffa-zA-Z0-9]+')


class SqliteIndex:
    """一个 SQLite 库（活跃库或归档库）的封装：主表 + FTS5 + 向量表。"""

    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._lock = __import__("threading").RLock()
        with self._lock:
            self.conn.executescript(_SCHEMA)
            # 旧库迁移：补 last_access 列（初始化为 ts）
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(fragments)").fetchall()]
            if "last_access" not in cols:
                self.conn.execute("ALTER TABLE fragments ADD COLUMN last_access REAL")
                self.conn.execute("UPDATE fragments SET last_access = ts")
            self.conn.commit()
            self.fts_enabled = False
            self.trigram = False
            if _fts_supported(self.conn):
                try:
                    self.conn.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS search_idx "
                        "USING fts5(searchable_text, id UNINDEXED, tokenize='trigram')"
                    )
                    self.trigram = True
                except Exception:
                    try:
                        self.conn.execute(
                            "CREATE VIRTUAL TABLE IF NOT EXISTS search_idx "
                            "USING fts5(searchable_text, id UNINDEXED)"
                        )
                    except Exception:
                        pass
                self.fts_enabled = True
            self.conn.commit()

    # ---------------- 写入 ----------------
    def insert(self, fragment) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO fragments "
                "(id, year, quarter, topic, sub_topic, ts, participants, facts_per_role, "
                " searchable_text, semantic_context, full_summary, last_access) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    fragment.id, fragment.year, fragment.quarter, fragment.main_topic,
                    fragment.sub_topic, fragment.ts,
                    json.dumps(fragment.participants, ensure_ascii=False),
                    json.dumps(fragment.facts_dict(), ensure_ascii=False),
                    fragment.searchable_text, fragment.semantic_context, fragment.full_summary,
                    fragment.ts,
                ),
            )
            if self.fts_enabled:
                try:
                    self.conn.execute(
                        "INSERT OR REPLACE INTO search_idx(searchable_text, id) VALUES (?, ?)",
                        (fragment.searchable_text, fragment.id),
                    )
                except Exception:
                    pass
            self.conn.commit()

    def insert_vector(self, fragment_id: str, vec_blob: bytes) -> None:
        with self._lock:
            self.conn.execute("INSERT OR REPLACE INTO vectors(id, vec) VALUES (?, ?)", (fragment_id, vec_blob))
            self.conn.commit()

    # ---------------- 硬过滤（毫秒级剪枝） ----------------
    def hard_filter(self, start_ts=None, end_ts=None, year=None, quarter=None,
                    topics=None, cap=cfg.CANDIDATE_CAP) -> list:
        """在 B-Tree 索引上执行结构化剪枝，返回 ≤cap 个候选 id（按时间倒序，最近优先）。

        注意：不再使用主题词做硬剪枝（同一事件可能被存储侧归入别的主题，主题过滤会
        盲目剪掉正确候选）；主题信号由双路打分承担。topics 参数保留兼容，传入也会被忽略。
        """
        clauses = []
        params = []
        if year is not None:
            clauses.append("year = ?")
            params.append(int(year))
        if quarter:
            clauses.append("quarter = ?")
            params.append(str(quarter).upper())
        if start_ts is not None:
            clauses.append("ts >= ?")
            params.append(float(start_ts))
        if end_ts is not None:
            clauses.append("ts < ?")
            params.append(float(end_ts))
        sql = "SELECT id FROM fragments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(int(cap))
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [r["id"] for r in rows]

    # ---------------- BM25（FTS5） ----------------
    def bm25_search(self, query: str, id_set, top_k=cfg.TOP_K) -> list:
        """返回 [(id, score, thin)]，score∈[0,1]（绝对尺度，不再由上层相对归一化）。

        score = min(1.0, FTS归一化 + 2-gram归一化)：
          - FTS5/trigram 精确片段命中 → 按候选内最大 FTS 分归一化；
          - 2-gram 重叠补充（中文短词/人名召回，IDF 加权）→ 按“查询自身全部
            gram 的潜力”归一化：只共享 1 个泛用填充词（如“好呀/好好”）时比例低，
            得分低，不会被放大成高置信；共享内容词越多得分越高。
        thin=True 表示该命中仅靠 1 个 2-gram 且无 FTS 支持（弱匹配，供上层
        用向量路径做“共识校验”，过滤泛用填充词巧合）。
        FTS5 不可用时自动退化为纯 2-gram 评分。
        """
        cleaned = _FTS_QUERY_CLEAN.sub(" ", (query or "")).strip()
        qtext = cleaned.replace(" ", "")
        qgrams = {qtext[i:i + 2] for i in range(max(0, len(qtext) - 1))} if qtext else set()
        fts_scores = {}
        gram_scores = {}
        gram_counts = {}

        # 1) FTS5 trigram 命中（精确片段）
        if self.fts_enabled and cleaned:
            with self._lock:
                if self.trigram:
                    shingles = [qtext[i:i + 3] for i in range(0, max(1, len(qtext) - 2))]
                    shingles = [s for s in shingles if len(s) >= 3][:24]
                    match = " OR ".join(f'"{t}"' for t in shingles) if shingles else ""
                else:
                    match = f'"{cleaned}"'
                if match:
                    try:
                        rows = self.conn.execute(
                            "SELECT id, bm25(search_idx) AS s FROM search_idx "
                            "WHERE search_idx MATCH ? ORDER BY s LIMIT ?",
                            (match, top_k * 3),
                        ).fetchall()
                        for r in rows:
                            if r["id"] in id_set:
                                fts_scores[r["id"]] = fts_scores.get(r["id"], 0.0) + max(0.0, -float(r["s"]))
                    except Exception:
                        pass

        # 2) 2-gram 重叠补充（人名/短词/换说法召回，带 IDF：常见片段降权、特色词加权）
        if qgrams and id_set:
            texts = {}
            with self._lock:
                for i in range(0, len(id_set), 200):
                    chunk = list(id_set)[i:i + 200]
                    placeholders = ",".join("?" * len(chunk))
                    rows = self.conn.execute(
                        f"SELECT id, searchable_text FROM fragments WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                    for r in rows:
                        texts[r["id"]] = r["searchable_text"] or ""
            n = len(texts) or 1
            df = {g: 0 for g in qgrams}
            grams_by_id = {}
            for fid, text in texts.items():
                tgrams = {text[j:j + 2] for j in range(max(0, len(text) - 1))}
                grams_by_id[fid] = tgrams
                for g in (tgrams & qgrams):
                    df[g] += 1
            idf = {g: math.log((n + 1) / (df[g] + 1)) + 1.0 for g in qgrams}
            potential = sum(idf.values()) or 1.0     # 查询自身全部 gram 的“潜力”（绝对参照）
            for fid, tgrams in grams_by_id.items():
                shared = tgrams & qgrams
                if shared:
                    gram_scores[fid] = sum(idf[g] for g in shared) / potential
                    gram_counts[fid] = len(shared)

        # 合并为绝对尺度：FTS 归一化 + 2-gram 归一化，封顶 1.0；
        # thin：仅 1 个 2-gram 且无 FTS 支持的弱匹配
        max_fts = max(fts_scores.values()) if fts_scores else 0.0
        out = []
        for fid in set(fts_scores) | set(gram_scores):
            fts_norm = (fts_scores.get(fid, 0.0) / max_fts) if max_fts > 0 else 0.0
            gram_norm = gram_scores.get(fid, 0.0)
            thin = fts_norm <= 0.0 and gram_counts.get(fid, 0) == 1
            out.append((fid, min(1.0, fts_norm + gram_norm), thin))
        out.sort(key=lambda x: x[1], reverse=True)
        return out[:top_k]

    # ---------------- 读取 ----------------
    def get(self, fragment_id: str):
        with self._lock:
            row = self.conn.execute("SELECT * FROM fragments WHERE id = ?", (fragment_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "year": row["year"],
            "quarter": row["quarter"],
            "main_topic": row["topic"],
            "sub_topic": row["sub_topic"],
            "ts": row["ts"],
            "last_access": row["last_access"],
            "participants": json.loads(row["participants"] or "[]"),
            "facts_per_role": json.loads(row["facts_per_role"] or "{}"),
            "searchable_text": row["searchable_text"],
            "semantic_context": row["semantic_context"],
            "full_summary": row["full_summary"],
        }

    def touch(self, ids) -> None:
        """更新最近访问时间（治理淘汰与缓存策略依赖）。"""
        if not ids:
            return
        with self._lock:
            for i in range(0, len(ids), 200):
                chunk = ids[i:i + 200]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"UPDATE fragments SET last_access = ? WHERE id IN ({placeholders})",
                    [time.time()] + list(chunk),
                )
            self.conn.commit()

    def oldest_ids(self, limit: int) -> list:
        """按最近访问时间升序取最旧的一批 id（供配额淘汰）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM fragments ORDER BY last_access ASC, ts ASC LIMIT ?", (int(limit),)
            ).fetchall()
        return [r["id"] for r in rows]

    def iter_rows(self, ids=None):
        """逐行产出 fragments（供整理 / 分类 / 治理）。"""
        with self._lock:
            if ids is None:
                rows = self.conn.execute("SELECT * FROM fragments").fetchall()
            else:
                if not ids:
                    return
                for i in range(0, len(ids), 200):
                    chunk = ids[i:i + 200]
                    placeholders = ",".join("?" * len(chunk))
                    rows = self.conn.execute(
                        f"SELECT * FROM fragments WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                    for r in rows:
                        yield dict(r)
                    return
            for r in rows:
                yield dict(r)

    def query_rows(self, text: str = "", ts_from=None, ts_to=None, limit: int = 200, offset: int = 0):
        """按「关键词 + 时间范围」分页查询片段（管理页展示用，SQL 侧过滤 + 分页）。

        此前管理页是 `iter_rows()` 全表拉取再到 Python 侧过滤：记忆一多（几万条）就会
        一次性扫全库并把全部数据塞进前端 DOM，表现为「打开管理页/滚动很卡」。这里改为：
          · 关键词 → SQL LIKE（检索文本 + 语义核心，与页面语义一致）；
          · 时间 → SQL 时间窗；
          · LIMIT/OFFSET 分页 + COUNT 总数。
        返回 (rows, total)：rows 为 dict 列表（同 iter_rows 结构）。
        """
        text = str(text or "").strip()
        where, params = [], []
        if text:
            like = f"%{text}%"
            where.append("(searchable_text LIKE ? OR full_summary LIKE ?)")
            params.extend([like, like])
        if ts_from is not None:
            where.append("ts >= ?")
            params.append(ts_from)
        if ts_to is not None:
            where.append("ts < ?")
            params.append(ts_to)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        with self._lock:
            total = int(self.conn.execute(
                f"SELECT COUNT(*) FROM fragments{clause}", params).fetchone()[0])
            rows = self.conn.execute(
                f"SELECT * FROM fragments{clause} ORDER BY ts DESC LIMIT ? OFFSET ?",
                params + [limit, offset]).fetchall()
        return [dict(r) for r in rows], total

    def page_rows(self, limit: int = 200, offset: int = 0, order: str = "ts_desc"):
        """分页读取片段（无过滤条件时使用）。返回 (rows, total)。"""
        return self.query_rows(limit=limit, offset=offset)

    def search_rows(self, text: str, limit: int = 200, offset: int = 0):
        """按关键词分页检索片段。返回 (rows, total)。"""
        return self.query_rows(text=text, limit=limit, offset=offset)

    def partition_ids(self) -> dict:
        """返回 {(year, quarter, topic): [ids]}，供整理分组。"""
        with self._lock:
            rows = self.conn.execute("SELECT id, year, quarter, topic FROM fragments").fetchall()
        out = {}
        for r in rows:
            key = (r["year"], r["quarter"], r["topic"])
            out.setdefault(key, []).append(r["id"])
        return out

    # ---------------- 分类索引（分类记录的预留接口实现） ----------------
    def participants_of(self, ids) -> dict:
        """批量返回 {id: [参与者名]}（供角色相关性加权）。"""
        if not ids:
            return {}
        out = {}
        with self._lock:
            for i in range(0, len(ids), 200):
                chunk = list(ids)[i:i + 200]
                placeholders = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    f"SELECT id, participants FROM fragments WHERE id IN ({placeholders})", chunk
                ).fetchall()
                for r in rows:
                    try:
                        out[r["id"]] = json.loads(r["participants"] or "[]")
                    except Exception:
                        out[r["id"]] = []
        return out

    def clear_categories(self) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM categories")
            self.conn.commit()

    def add_category(self, fid: str, cat_type: str, cat_value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO categories(id, cat_type, cat_value) VALUES (?, ?, ?)",
                (fid, cat_type, cat_value),
            )
            self.conn.commit()

    def list_categories(self, cat_type: str = None, limit: int = 100):
        """分类清单：[{cat_type, cat_value, count}]。"""
        with self._lock:
            if cat_type:
                rows = self.conn.execute(
                    "SELECT cat_type, cat_value, COUNT(*) AS n FROM categories "
                    "WHERE cat_type = ? GROUP BY cat_value ORDER BY n DESC LIMIT ?",
                    (cat_type, int(limit)),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT cat_type, cat_value, COUNT(*) AS n FROM categories "
                    "GROUP BY cat_type, cat_value ORDER BY cat_type, n DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [{"cat_type": r["cat_type"], "cat_value": r["cat_value"], "count": r["n"]} for r in rows]

    def records_in_category(self, cat_type: str, cat_value: str, limit: int = 50) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM categories WHERE cat_type = ? AND cat_value = ? LIMIT ?",
                (cat_type, cat_value, int(limit)),
            ).fetchall()
        return [r["id"] for r in rows]

    def texts_of(self, ids) -> dict:
        """返回 {id: searchable_text}（供检索层做时间-内容确认等轻量检查）。"""
        if not ids:
            return {}
        out = {}
        with self._lock:
            for i in range(0, len(ids), 200):
                chunk = list(ids)[i:i + 200]
                placeholders = ",".join("?" * len(chunk))
                rows = self.conn.execute(
                    f"SELECT id, searchable_text FROM fragments WHERE id IN ({placeholders})", chunk
                ).fetchall()
                for r in rows:
                    out[r["id"]] = r["searchable_text"] or ""
        return out

    def get_rows(self, ids) -> list:
        if not ids:
            return []
        rows = []
        with self._lock:
            for i in range(0, len(ids), 200):
                chunk = ids[i:i + 200]
                placeholders = ",".join("?" * len(chunk))
                rows += [dict(r) for r in self.conn.execute(
                    f"SELECT * FROM fragments WHERE id IN ({placeholders})", chunk
                ).fetchall()]
        return rows

    def get_vectors(self, ids=None) -> dict:
        """返回 {id: vec_blob}。"""
        with self._lock:
            if ids is None:
                rows = self.conn.execute("SELECT id, vec FROM vectors").fetchall()
            else:
                if not ids:
                    return {}
                placeholders = ",".join("?" * len(ids))
                rows = self.conn.execute(f"SELECT id, vec FROM vectors WHERE id IN ({placeholders})", ids).fetchall()
        return {r["id"]: r["vec"] for r in rows}

    def ids_before(self, year, quarter) -> list:
        """当前季度之前的全部 id（用于季度滚动归档）。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM fragments WHERE (year < ?) OR (year = ? AND quarter < ?)",
                (int(year), int(year), str(quarter).upper()),
            ).fetchall()
        return [r["id"] for r in rows]

    def delete(self, ids) -> None:
        if not ids:
            return
        with self._lock:
            for i in range(0, len(ids), 200):
                chunk = ids[i:i + 200]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(f"DELETE FROM fragments WHERE id IN ({placeholders})", chunk)
                self.conn.execute(f"DELETE FROM vectors WHERE id IN ({placeholders})", chunk)
                if self.fts_enabled:
                    try:
                        self.conn.execute(f"DELETE FROM search_idx WHERE id IN ({placeholders})", chunk)
                    except Exception:
                        pass
            self.conn.commit()

    def count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) AS n FROM fragments").fetchone()["n"]

    def rebuild_fts(self) -> int:
        """FTS5 索引重建（searchable_text 极短，重建成本低）。"""
        if not self.fts_enabled:
            return 0
        with self._lock:
            try:
                self.conn.execute("INSERT INTO search_idx(search_idx) VALUES('rebuild')")
                self.conn.commit()
            except Exception:
                pass
        return self.count()

    def close(self):
        try:
            with self._lock:
                self.conn.close()
        except Exception:
            pass
