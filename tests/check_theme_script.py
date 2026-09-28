# check_theme_script.py —— 校验「背景主题内联脚本」拼出的 JS 语法正确（用 node --check）
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web import server as srv  # noqa: E402

html = srv._theme_inline_script()
m = re.search(r"<script>([\s\S]*?)</script>", html)
if not m:
    print("未找到内联脚本")
    sys.exit(2)

js = m.group(1)
print("生成的脚本：")
print(js)
print()

fd, path = tempfile.mkstemp(suffix=".js")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("var window={},document={body:{classList:{add:function(){}},style:{setProperty:function(){}}}};\n")
        f.write("function Image(){}\n")
        f.write(js)
except Exception:
    os.remove(path)
    raise
try:
    rc = subprocess.call(["node", "--check", path])
finally:
    try:
        os.remove(path)
    except OSError:
        pass
print()
print("语法检查：%s" % ("✓ 通过" if rc == 0 else "✗ 失败"))
sys.exit(rc)
