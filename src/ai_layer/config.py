# -*- coding: utf-8 -*-
"""通用 AI 配置加载模块：四段式 API Key 读取（env -> .env -> 交互输入 -> 降级兜底）。

第 1 段  系统环境变量 AI_API_KEY（优先级最高，load_dotenv 不覆盖）
第 2 段  本地 .env 文件 AI_API_KEY
第 3 段  交互式输入：提示手动输入 Key，并选择提供商自动补全 BASE_URL/MODEL，
         成功后写回 .env，下次免输入
第 4 段  降级兜底：回车跳过 / 非终端环境（如 UE subprocess 无 TTY）→ api_key=None，
         由调用方触发纯规则 / Mock 降级，保证管线不中断

通用配置项（与具体提供商解耦）：
    AI_PROVIDER  默认 deepseek
    AI_API_KEY
    AI_BASE_URL
    AI_MODEL
"""

import os
import sys

from dotenv import load_dotenv

# 提供商预设：选择提供商后自动补全 base_url / model
PROVIDER_PRESETS = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}

_KEY_NAMES = ("AI_PROVIDER", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL")

# .env 路径统一走 env_path.resolve_env_path()：与 exe 首次配置向导共用同一解析函数，
# 保证「写到哪、读到哪」一致。源码态 resolve_env_path() 仍指向本模块同目录 .env，开发环境不变。
try:
    from .env_path import resolve_env_path
except ImportError:
    from env_path import resolve_env_path

_ENV_FILE = resolve_env_path()

_PROMPT_KEY = "未检测到 API Key，请手动输入（直接回车跳过，将启用纯规则/Mock 降级模式）："
_PROMPT_PROVIDER = "选择提供商（1-DeepSeek, 2-OpenAI, 3-自定义）："


def load_ai_config(interactive=None, env_file=None, input_fn=None, print_fn=None) -> dict:
    """四段式读取通用 AI 配置。

    参数:
        interactive: None=自动判断（仅终端 TTY 才可交互）；True/False 强制开关（测试用）。
        env_file:    指定 .env 路径；默认本模块同目录 .env（测试可传临时文件）。
        input_fn / print_fn: 交互函数，测试可替换以模拟用户输入。

    返回:
        {"provider", "api_key", "base_url", "model", "key_source"}
        key_source ∈ {"env", "dotenv", "interactive", "degraded"}
        无 Key 时 api_key=None，调用方走降级逻辑。
    """
    input_fn = input_fn or input
    print_fn = print_fn or print
    env_file = env_file or _ENV_FILE

    # ---- 第 1 段：系统环境变量（装载 .env 前取样，确保优先级最高）----
    env_key = os.environ.get("AI_API_KEY")

    # ---- 第 2 段：本地 .env（override=False：已存在的系统环境变量不被覆盖）----
    load_dotenv(env_file, override=False)
    if env_key:
        key_source = "env"
    elif os.environ.get("AI_API_KEY"):
        key_source = "dotenv"
    else:
        key_source = None

    config = {
        "provider": os.environ.get("AI_PROVIDER", "deepseek"),
        "api_key": os.environ.get("AI_API_KEY"),
        "base_url": os.environ.get("AI_BASE_URL"),
        "model": os.environ.get("AI_MODEL"),
        "key_source": key_source,
    }

    if config["api_key"]:
        _apply_provider_defaults(config, print_fn)
        return config

    # ---- 第 3 段：交互式输入（仅真正终端可交互；UE subprocess 自动跳过）----
    if interactive is None:
        interactive = sys.stdin.isatty()
    if interactive and _read_key_interactively(config, env_file, input_fn, print_fn):
        _apply_provider_defaults(config, print_fn)
        return config

    # ---- 第 4 段：降级兜底：不传 Key，由调用方走纯规则 / Mock 降级 ----
    config["key_source"] = "degraded"
    print_fn("[配置] 未获得 API Key，进入纯规则/Mock 降级模式（管线不中断）。")
    return config


def _apply_provider_defaults(config, print_fn):
    """按 provider 预设补全缺失的 base_url / model，并打印命中来源。"""
    preset = PROVIDER_PRESETS.get(config["provider"])
    if preset:
        config["base_url"] = config["base_url"] or preset["base_url"]
        config["model"] = config["model"] or preset["model"]
    source_names = {"env": "第1段-系统环境变量", "dotenv": "第2段-.env文件",
                    "interactive": "第3段-交互式输入"}
    print_fn("[配置] AI Key 命中来源：{0}（provider={1}, model={2}）".format(
        source_names.get(config["key_source"], config["key_source"]),
        config["provider"], config["model"]))


def _read_key_interactively(config, env_file, input_fn, print_fn) -> bool:
    """第 3 段交互流程：输入 Key -> 选提供商 -> 补全配置 -> 写回 .env。"""
    try:
        key = input_fn(_PROMPT_KEY).strip()
    except (EOFError, OSError):
        print_fn("[配置] 标准输入不可用，跳过交互，进入降级模式。")
        return False
    if not key:
        return False  # 直接回车：进入第 4 段降级兜底

    provider = _choose_provider(input_fn, print_fn)

    config["provider"] = provider
    config["api_key"] = key
    config["key_source"] = "interactive"

    if provider == "custom":
        try:
            config["base_url"] = input_fn("请输入 AI_BASE_URL：").strip() or None
            config["model"] = input_fn("请输入 AI_MODEL：").strip() or None
        except (EOFError, OSError):
            pass  # 自定义字段读不到时不阻塞：允许只提交 Key
    else:
        preset = PROVIDER_PRESETS[provider]
        config["base_url"] = config["base_url"] or preset["base_url"]
        config["model"] = config["model"] or preset["model"]

    _write_env_file(env_file, config)
    print_fn("[配置] 配置已写入 {0}，下次启动自动读取，无需重复输入。".format(env_file))
    return True


def _choose_provider(input_fn, print_fn) -> str:
    """交互式选择提供商：1-DeepSeek / 2-OpenAI / 3-自定义，非法输入重问。"""
    while True:
        try:
            choice = input_fn(_PROMPT_PROVIDER).strip()
        except (EOFError, OSError):
            return "deepseek"
        if choice == "1":
            return "deepseek"
        if choice == "2":
            return "openai"
        if choice == "3":
            return "custom"
        print_fn("输入无效，请输入 1 / 2 / 3。")


def _write_env_file(env_file, config) -> None:
    """把通用 AI 配置写回 .env：只更新 AI_* 四个键，保留其他行。"""
    values = {
        "AI_PROVIDER": config["provider"] or "",
        "AI_API_KEY": config["api_key"] or "",
        "AI_BASE_URL": config["base_url"] or "",
        "AI_MODEL": config["model"] or "",
    }
    lines = []
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f.read().splitlines():
                name = line.split("=", 1)[0].strip()
                if name not in _KEY_NAMES:
                    lines.append(line)
    for name in _KEY_NAMES:
        lines.append("{0}={1}".format(name, values[name]))
    os.makedirs(os.path.dirname(env_file) or ".", exist_ok=True)
    with open(env_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")