# -*- coding: utf-8 -*-
"""共享的 .env 解析模块：唯一事实来源，打通「向导写」与「AI 层读」。

设计要点：
    - 纯标准库（os / sys / re），不依赖 dotenv / openai，可安全打进 PyInstaller exe；
    - resolve_env_path() 由 pipeline_watcher.py（exe 首次配置向导，负责写）
      与 config.py（AI 层，负责读）共同调用，杜绝「写到一个地方、读另一个地方」；
    - 所有对外可见的 Key 一律经 mask_key() 脱敏后再打印 / 回显。

路径解析优先级（resolve_env_path）：
    1. 环境变量 AI_ENV_PATH / ASSET_PIPELINE_ENV（显式覆盖，最高优先）
    2. frozen（PyInstaller exe）：exe 同级目录 .env（下载即用）
    3. 源码运行：本模块同目录 .env（仓库 src/ai_layer/.env，开发环境不变）
"""

import os
import re
import sys

# 显式覆盖 .env 路径的环境变量（自高到低）
_ENV_PATH_OVERRIDES = ("AI_ENV_PATH", "ASSET_PIPELINE_ENV")

# 占位符判定词（大小写不敏感的子串匹配；命中即视为「未配置」）
_PLACEHOLDER_MARKERS = (
    "your_key_here", "your-key-here", "yourkeyhere",
    "changeme", "change_me", "change-me",
    "placeholder", "example", "dummy",
    "replace_me", "replace-me", "replace",
    "sk-xxxx", "sk-xxxxx", "sk-xxxxxx", "sk-xxx", "sk-...",
    "sk-your", "sk_key", "sk-test", "sk-dummy",
    "test_key", "your_key", "yourkey",
    "xxxx", "xxxxx", "xxxxxx", "xxxxxxx",
)


def resolve_env_path() -> str:
    """返回唯一确定的 .env 文件路径（幂等，不隐式创建文件）。

    优先级：环境变量覆盖 > frozen 的 exe 同级目录 > 源码态本模块同目录。
    """
    for key in _ENV_PATH_OVERRIDES:
        val = os.environ.get(key, "").strip()
        if val:
            return os.path.abspath(val)
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(sys.executable)), ".env")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def read_env_file(path: str) -> dict:
    """读取 .env 为 {KEY: VALUE}，跳过空行与 # 注释，容忍文件不存在。"""
    result = {}
    if not path or not os.path.exists(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            name, _, value = line.partition("=")
            result[name.strip()] = value.strip()
    return result


def write_env_value(path: str, key: str, value: str) -> dict:
    """把单个键写回 .env：只更新该键，保留其它行；UTF-8 写入。

    返回写盘后的完整键值（便于调用方核对）。value 传 None 视为空串。
    """
    key = key.strip()
    value = "" if value is None else str(value).strip()

    kept = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()
        for raw in original.splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept.append(line)
                continue
            if "=" in line:
                name = line.split("=", 1)[0].strip()
                if name == key:
                    continue  # 目标键：稍后统一追加更新后的值
                kept.append(line)
            else:
                kept.append(line)

    dir_name = os.path.dirname(path)
    if dir_name and not os.path.isdir(dir_name):
        os.makedirs(dir_name, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept + ["{0}={1}".format(key, value)]) + "\n")

    return read_env_file(path)


def is_placeholder_key(value) -> bool:
    """判断 value 是否为占位符：空、明显占位词、样例串，均视为「未配置」。"""
    if value is None:
        return True
    v = str(value).strip()
    if not v:
        return True
    low = v.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in low:
            return True
    # 「sk-」开头但内容过短（去掉连字符后 < 16 字符）视为样例/占位（真实 DeepSeek/OpenAI Key 均远长于此）
    if low.startswith("sk-") and len(v.replace("-", "")) < 16:
        return True
    # 全部由占位字符构成（如 ******、xxxxxxx）
    if re.fullmatch(r"[xX*._\-]+", v):
        return True
    return False


def mask_key(value) -> str:
    """Key 脱敏：只返回前 6 位 + '...'，杜绝完整 Key 泄漏到日志 / 回显。"""
    if value is None:
        return "<未配置>"
    v = str(value).strip()
    if not v:
        return "<空>"
    return v[:6] + "..."