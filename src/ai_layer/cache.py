"""缓存：内容寻址 key（asset_path + mtime + file_size）+ 读写（TTL 30 天）。"""

import hashlib
import json
import os
import time

CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
CACHE_TTL_DAYS = 30


def resolve_physical_asset(asset_path: str, ue_project_content: str):
    """/Game/... -> <Content>/....uasset ；地图为 .umap。找不到返回 None。"""
    if not asset_path.startswith("/Game/"):
        return None
    rel = asset_path[len("/Game/"):]
    base = os.path.join(ue_project_content, rel.replace("/", os.sep))
    for ext in (".uasset", ".umap"):
        p = base + ext
        if os.path.exists(p):
            return p
    return None


def make_cache_key(asset_path: str, ue_project_content: str) -> str:
    p = resolve_physical_asset(asset_path, ue_project_content)
    if p:
        mtime = os.path.getmtime(p)      # 浮点秒，保留毫秒精度
        file_size = os.path.getsize(p)
    else:
        mtime, file_size = -1.0, -1      # 文件不存在兜底
    raw = json.dumps(
        {"asset_path": asset_path, "mtime": mtime, "file_size": file_size},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(key: str):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # TTL 过期判定：超过 CACHE_TTL_DAYS 天视为失效（人工可能已改过命名规范）
    if time.time() - data.get("created_at", 0) > CACHE_TTL_DAYS * 86400:
        return None
    return data.get("result")


def set_cached(key: str, result: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{key}.json"), "w", encoding="utf-8") as f:
        json.dump({"created_at": time.time(), "result": result},
                  f, ensure_ascii=False)