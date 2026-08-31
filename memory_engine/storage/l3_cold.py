# memory_engine/storage/l3_cold.py
# L3 冷存储：磁盘文件系统，按 时间-主题 树状目录分区保存完整原文。
# 日常检索绝不触碰此层；仅在确定命中且上层需要原文详述时，按 id 一次磁盘 IO 读取。
# 格式：pyarrow 可用时使用 Parquet（year/quarter/topic 分区），否则降级为 JSONL。
from __future__ import annotations

import json
import os

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
    _HAS_PARQUET = True
except Exception:
    _HAS_PARQUET = False

import memory_engine.config as cfg


class ColdStorage:
    def __init__(self, root: str = None):
        self.root = root or cfg.COLD_DIR
        os.makedirs(self.root, exist_ok=True)

    # ---------------- 路径与分区 ----------------
    def _partition_dir(self, year: int, quarter: str, main_topic: str) -> str:
        d = os.path.join(self.root, str(year), str(quarter).upper(), (main_topic or "综合").strip(" /\\"))
        os.makedirs(d, exist_ok=True)
        return d

    def _file_path(self, year, quarter, main_topic) -> str:
        d = self._partition_dir(year, quarter, main_topic)
        return os.path.join(d, "full.parquet" if _HAS_PARQUET else "full.jsonl")

    # ---------------- 写入 ----------------
    def write(self, fragment) -> str:
        """把原始全文按 (年/季度/主题) 归档，返回引用路径。"""
        path = self._file_path(fragment.year, fragment.quarter, fragment.main_topic)
        if _HAS_PARQUET:
            self._append_parquet(path, fragment.id, fragment.raw_text or fragment.full_summary)
        else:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"id": fragment.id, "full_text": fragment.raw_text or fragment.full_summary},
                                   ensure_ascii=False) + "\n")
        return os.path.relpath(path, self.root).replace("\\", "/")

    def _append_parquet(self, path, fid, full_text):
        table = pa.table({"id": [fid], "full_text": [full_text]})
        if os.path.exists(path):
            try:
                old = pq.read_table(path)
                table = pa.concat_tables([old, table])
            except Exception:
                pass
        pq.write_table(table, path)

    # ---------------- 清理 ----------------
    def clear(self) -> None:
        """删除全部冷存储分区文件（记忆初始化用；引擎运行中调用安全，无持久的打开句柄）。"""
        import shutil
        if os.path.isdir(self.root):
            shutil.rmtree(self.root, ignore_errors=True)
        os.makedirs(self.root, exist_ok=True)

    # ---------------- 读取 ----------------
    def read(self, ref: str):
        """按引用路径读取原文（缺失返回 None）。"""
        path = os.path.join(self.root, ref.replace("/", os.sep))
        if not os.path.isfile(path):
            return None
        if path.endswith(".parquet"):
            try:
                t = pq.read_table(path)
                rows = t.to_pylist()
                if not rows:
                    return None
                return rows[0].get("full_text", "")
            except Exception:
                return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        return json.loads(line).get("full_text", "")
        except Exception:
            return None
        return None

    def read_by_id(self, year, quarter, main_topic, fragment_id: str):
        """按分区与 id 读取原文（含 JSONL 多行场景）。"""
        path = self._file_path(year, quarter, main_topic)
        if not os.path.isfile(path):
            return None
        if path.endswith(".parquet"):
            try:
                t = pq.read_table(path)
                rows = t.to_pylist()
                for r in rows:
                    if r.get("id") == fragment_id:
                        return r.get("full_text", "")
            except Exception:
                return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("id") == fragment_id:
                        return obj.get("full_text", "")
        except Exception:
            return None
        return None

    def update_texts(self, year, quarter, main_topic, text_map) -> int:
        """原位更新分区文件中指定 id 的原文（用于原文压缩降级，不删除记录）。"""
        if not text_map:
            return 0
        path = self._file_path(year, quarter, main_topic)
        if not os.path.isfile(path):
            return 0
        updated = 0
        if path.endswith(".parquet"):
            try:
                t = pq.read_table(path)
                rows = t.to_pylist()
                for r in rows:
                    if r.get("id") in text_map:
                        r["full_text"] = text_map[r["id"]]
                        updated += 1
                if updated:
                    pq.write_table(pa.table({"id": [r["id"] for r in rows],
                                             "full_text": [r.get("full_text", "") for r in rows]}), path)
            except Exception:
                return 0
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                out = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        out.append(line)
                        continue
                    if obj.get("id") in text_map:
                        obj["full_text"] = text_map[obj["id"]]
                        updated += 1
                    out.append(json.dumps(obj, ensure_ascii=False))
                if updated:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(out) + ("\n" if out else ""))
            except Exception:
                return 0
        return updated

    def read_partition(self, year, quarter, main_topic) -> list:
        """读取整个分区的条目列表：[{id, full_text}]（记忆管理页 L3 详情展示用）。"""
        path = self._file_path(year, quarter, main_topic)
        if not os.path.isfile(path):
            return []
        out = []
        if path.endswith(".parquet"):
            try:
                t = pq.read_table(path)
                for r in t.to_pylist():
                    out.append({"id": r.get("id", ""), "full_text": r.get("full_text", "")})
                return out
            except Exception:
                return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    out.append({"id": obj.get("id", ""), "full_text": obj.get("full_text", "")})
        except Exception:
            return []
        return out

    def remove_ids(self, year, quarter, main_topic, fragment_ids) -> int:
        """从分区文件中删除指定 id 的原文（释放 L3 空间），返回删除条数。"""
        if not fragment_ids:
            return 0
        id_set = set(fragment_ids)
        path = self._file_path(year, quarter, main_topic)
        if not os.path.isfile(path):
            return 0
        removed = 0
        if path.endswith(".parquet"):
            try:
                t = pq.read_table(path)
                rows = [r for r in t.to_pylist() if r.get("id") not in id_set]
                removed = len(t) - len(rows)
                if removed > 0:
                    if rows:
                        pq.write_table(pa.table({"id": [r["id"] for r in rows],
                                                 "full_text": [r.get("full_text", "") for r in rows]}), path)
                    else:
                        # 全部移除：空 parquet 仍有页脚占用，直接删文件
                        try:
                            os.remove(path)
                        except Exception:
                            pass
            except Exception:
                return 0
        else:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                keep = []
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        keep.append(line)
                        continue
                    if obj.get("id") in id_set:
                        removed += 1
                    else:
                        keep.append(line)
                if removed > 0:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(keep) + ("\n" if keep else ""))
            except Exception:
                return 0
        # 空分区文件直接删除目录；顺带清理空的主题/季度/年份目录（只到 root 之下）
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            try:
                os.remove(path)
            except Exception:
                pass
        for d in (os.path.dirname(path),
                  os.path.dirname(os.path.dirname(path)),
                  os.path.dirname(os.path.dirname(os.path.dirname(path)))):
            if d == self.root or not d:
                continue
            try:
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            except Exception:
                pass
        return removed

    def partitions(self) -> list:
        """已归档的分区概览（年/季度/主题）。"""
        out = []
        if not os.path.isdir(self.root):
            return out
        for y in sorted(os.listdir(self.root)):
            yd = os.path.join(self.root, y)
            if not os.path.isdir(yd):
                continue
            for q in sorted(os.listdir(yd)):
                qd = os.path.join(yd, q)
                if not os.path.isdir(qd):
                    continue
                for t in sorted(os.listdir(qd)):
                    td = os.path.join(qd, t)
                    if os.path.isdir(td):
                        out.append(f"{y}/{q}/{t}")
        return out
