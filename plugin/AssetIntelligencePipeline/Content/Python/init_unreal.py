"""Asset Intelligence Pipeline - 插件入口。

UE 编辑器启动时，会自动执行本文件（因为它在插件 Content/Python/ 下）。
职责：
    1. 把本插件的 Python 目录注入 sys.path，让同目录下的其他模块可被 import
    2. 打印加载日志，方便在 Output Log 里确认插件是否生效
"""

import os
import sys

# 本文件所在目录 = Content/Python/
_PLUGIN_PYTHON_DIR = os.path.dirname(os.path.abspath(__file__))

# 注入 sys.path（去重，避免重复加载）
if _PLUGIN_PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_PYTHON_DIR)

print(f"[AssetIntelligencePipeline] init_unreal.py 已加载")
print(f"[AssetIntelligencePipeline] Python 目录：{_PLUGIN_PYTHON_DIR}")