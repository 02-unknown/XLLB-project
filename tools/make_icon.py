#!/usr/bin/env python3
# make_icon.py —— 由一张角色图生成应用图标（窗口 / 任务栏图标 + 网页图标）。
#
# 用法（在项目根目录运行）：
#   venv\Scripts\python.exe make_icon.py                 # 默认用根目录 111.png
#   venv\Scripts\python.exe make_icon.py 路径\图片.png    # 指定源图
#
# 生成物（会覆盖已有文件）：
#   web/static/app-icon.png   512×512 正方形图标（去掉透明留白后居中，透明底）
#   web/static/favicon.ico    多尺寸 ico（16/24/32/48/64/128/256），浏览器标签与窗口图标用
#
# 说明：任务栏 / 窗口图标取自网页 favicon（桌面版用 Edge/Chrome 应用模式窗口），
#      所以换图标只需要重新生成本脚本的两个产物，界面各页面已引用 /static/app-icon.png。
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "111.png")
PNG_OUT = os.path.join(ROOT, "web", "static", "app-icon.png")
ICO_OUT = os.path.join(ROOT, "web", "static", "favicon.ico")
SIZE = 512      # 正方形画布边长
MARGIN = 0.08   # 四周留白比例（避免贴边）

im = Image.open(SRC).convert("RGBA")
# 去掉四周透明留白，只保留角色本体
bbox = im.getchannel("A").getbbox()
content = im.crop(bbox) if bbox else im

# 等比缩放后居中放进正方形画布，避免图标被拉伸变形
inner = SIZE * (1 - MARGIN * 2)
scale = min(inner / content.width, inner / content.height)
new = content.resize((max(1, round(content.width * scale)),
                      max(1, round(content.height * scale))), Image.LANCZOS)
canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
canvas.paste(new, ((SIZE - new.width) // 2, (SIZE - new.height) // 2), new)

canvas.save(PNG_OUT)
canvas.save(ICO_OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"源图：{SRC}")
print(f"裁剪区：{bbox} 本体：{content.size} -> {new.size}")
print(f"已生成：{PNG_OUT}")
print(f"已生成：{ICO_OUT}")
