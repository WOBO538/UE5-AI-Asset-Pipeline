"""核心入口：analyze_manifest + 三个辅助函数（解析 / 重试 / 调用）。

AI 层是纯 Python，由 UE 消费层调用：UE 消费层读 import_manifest.json 后，
把 assets 列表 + ue_project_content 传入 analyze_manifest。

通用 AI 配置（多提供商、四段式 Key 读取）见 config.py。
"""

import json
import os
import re
import time

try:
    from . import cache
    from . import config
    from . import prompt_templates
    from . import mock_ai
except ImportError:
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import cache
    import config
    import prompt_templates
    from mock_ai import mock_response

# ---- 通用 AI 配置：四段式读取（系统环境变量 -> .env -> 交互输入 -> 降级兜底）----
# 与具体提供商解耦，支持 DeepSeek / OpenAI / 自定义，config.py 内部同步装载 .env
_CFG = config.load_ai_config()

API_KEY = _CFG["api_key"]
PROVIDER = _CFG["provider"]
MODEL = _CFG["model"]
BASE_URL = _CFG["base_url"]
BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "20"))
TIMEOUT = float(os.environ.get("AI_TIMEOUT", "30.0"))
MAX_RETRIES = int(os.environ.get("AI_MAX_RETRIES", "3"))

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
    return _client


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _parse_json(raw: str):
    """非法 JSON 兜底：直接解析 → 剥离 markdown → 正则抓 {...} → 返回 None。"""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _call_with_retry(chunk: list[str]) -> str:
    """调模型（带指数退避重试），返回模型原文。"""
    from openai import APIConnectionError, APITimeoutError

    messages = [
        {"role": "system", "content": prompt_templates.SYSTEM_PROMPT},
        {"role": "user", "content": prompt_templates.USER_PROMPT_TEMPLATE.format(
            assets_json=json.dumps(chunk, ensure_ascii=False))},
    ]
    for attempt in range(MAX_RETRIES):
        try:
            resp = _get_client().chat.completions.create(
                model=MODEL,
                messages=messages,
                temperature=0.1,
                timeout=TIMEOUT,
                response_format={"type": "json_object"},
            )
            return resp.choices[0].message.content
        except (APITimeoutError, APIConnectionError):
            if attempt == MAX_RETRIES - 1:
                raise  # 最终仍失败 -> 整批降级
            time.sleep(2 ** attempt)  # 指数退避 1s / 2s / 4s


def _call_and_parse(chunk: list[str]) -> dict:
    """调用 + 解析 + 整批降级，返回 {"results": [...], "failures": [...]}。"""
    try:
        raw = _call_with_retry(chunk)
    except Exception:
        return {"results": [],
                "failures": [{"asset_path": p, "reason": "API timeout"}
                             for p in chunk]}
    parsed = _parse_json(raw)
    if parsed is None:
        return {"results": [],
                "failures": [{"asset_path": p, "reason": "INVALID_JSON"}
                             for p in chunk]}
    return parsed


def analyze_manifest(manifest: dict, ue_project_content: str,
                     mock: bool = False) -> dict:
    """分析一批资产，返回 {"analyzed_at", "model", "results", "failures"}。

    缓存命中直接回填，未命中才批量调 API（或走 mock）。
    """
    assets = manifest.get("assets", [])

    results, failures = [], []

    # Key 缺失（真实模式）：整批失败，不发起任何请求
    if not mock and API_KEY is None:
        return {
            "analyzed_at": _now_iso(),
            "provider": PROVIDER,
            "model": None,
            "results": [],
            "failures": [{"asset_path": p, "reason": "KEY_MISSING"}
                         for p in assets],
        }

    if mock:
        to_analyze = list(assets)
    else:
        to_analyze = []
        for p in assets:
            key = cache.make_cache_key(p, ue_project_content)
            hit = cache.get_cached(key)
            if hit is not None:
                if "reason" in hit:
                    failures.append(hit)
                else:
                    results.append(hit)
            else:
                to_analyze.append(p)

    for i in range(0, len(to_analyze), BATCH_SIZE):
        chunk = to_analyze[i:i + BATCH_SIZE]
        parsed = mock_response(chunk) if mock else _call_and_parse(chunk)
        for rec in parsed.get("results", []):
            results.append(rec)
            if not mock:
                cache.set_cached(cache.make_cache_key(rec.get("asset_path", ""), ue_project_content), rec)
        failures.extend(parsed.get("failures", []))

    return {
        "analyzed_at": _now_iso(),
        "provider": "mock" if mock else PROVIDER,
        "model": "mock" if mock else MODEL,
        "results": results,
        "failures": failures,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, help="import_manifest.json 路径")
    parser.add_argument("--ue-content", required=True, help="UE 项目 Content 目录")
    parser.add_argument("--output", required=True, help="ai_analysis.json 输出路径")
    parser.add_argument("--mock", action="store_true", help="使用 Mock 模式")
    args = parser.parse_args()

    with open(args.manifest, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    result = analyze_manifest(manifest, args.ue_content, mock=args.mock)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=4, ensure_ascii=False)

    print(f"[AI] 分析完成，写入 {args.output}")