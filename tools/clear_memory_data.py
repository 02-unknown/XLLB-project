# -*- coding: utf-8 -*-
"""清除旧数据：物理删除 runtime/memory_engine 全部内容，重建为全新空库。"""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_engine
import memory_engine.config as cfg

eng = memory_engine.get_engine()
eng.init()
eng.close()
shutil.rmtree(cfg.DATA_DIR, ignore_errors=True)
memory_engine.reset_engine()
eng = memory_engine.get_engine()      # 全新空库
eng.init()
print("已清除旧数据。当前记忆库：活跃=%d 归档=%d 冷分区=%d 向量=%d" % (
    eng.active.count(), eng.archive_db.count(),
    len(eng.cold.partitions()) if eng.cold else 0,
    eng.vector.size() if eng.vector else 0))
