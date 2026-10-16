# -*- coding: utf-8 -*-
"""资产管线 UE 消费层：读取 Imported 目录的 trigger.txt 与清单，驱动校验、重命名与报告。"""

import os
import json
import time
import subprocess

import unreal
import asset_validator


def check_and_process_pipeline():
    """核心入口：遍历 Processing 目录下每个 zip 子目录，逐个子目录执行校验、重命名与报告。

    流程：
        1. 定位 <项目>/Saved/AssetPipeline/Processing 目录
        2. 遍历其下所有子目录（每个子目录对应一个 zip_name）
        3. 每个子目录：读取 trigger.txt 与 import_manifest.json
        4. 若 trigger.txt 不存在或 mtime 距今小于 3 秒则跳过
        5. 调 asset_validator 逐资产校验，并重命名命名错误项
        6. 生成报告后把 trigger.txt 改名为 trigger.txt.done（清单保留不删除）
        7. 弹窗通知该 zip 的处理结果
    每个子目录独立 try/except，单个失败不影响其他子目录，不让编辑器崩溃。
    """
    base_dir = os.path.join(unreal.Paths.project_dir(), "Saved", "AssetPipeline", "Processing")

    if not os.path.isdir(base_dir):
        unreal.log(f"[管线] Processing 目录不存在: {base_dir}，跳过本轮。")
        return

    try:
        sub_dirs = [d for d in os.listdir(base_dir)
                    if os.path.isdir(os.path.join(base_dir, d))]
    except Exception as e:
        unreal.log_error(f"[管线] 读取 Processing 目录失败: {e}")
        return

    for sub_name in sub_dirs:
        try:
            _process_sub_dir(os.path.join(base_dir, sub_name))
        except Exception as e:
            unreal.log_error(f"[管线] 处理子目录 {sub_name} 异常: {e}")


def _process_sub_dir(sub_dir):
    """处理单个 zip 子目录：校验、重命名、生成报告并弹窗通知。"""
    zip_name = os.path.basename(sub_dir)
    trigger_path = os.path.join(sub_dir, "trigger.txt")

    if not os.path.exists(trigger_path):
        unreal.log(f"[管线] {zip_name} 未发现 trigger.txt，跳过。")
        return

    # mtime 保护：写入不足 3 秒，可能文件尚未写完
    age = time.time() - os.path.getmtime(trigger_path)
    if age < 3.0:
        unreal.log(f"[管线] {zip_name} trigger.txt 写入仅 {age:.2f} 秒，可能未写完，跳过。")
        return

    manifest_path = os.path.join(sub_dir, "import_manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    assets = manifest.get("assets", [])

    # 【AI 增强】通过 subprocess 调用系统 Python 的 AI 层，生成 ai_analysis.json
    # 硬编码路径是 v0.2 妥协，v0.3 应改为 config 或插件配置读取
    _AI_REPO_ROOT = r"D:\Projects\my-portfolio\tools\UE5-AI-Asset-Pipeline"
    _AI_SCRIPT = os.path.join(_AI_REPO_ROOT, "src", "ai_layer", "analyze.py")
    _AI_OUTPUT = os.path.join(sub_dir, "ai_analysis.json")

    if os.path.exists(_AI_SCRIPT):
        try:
            ue_content = unreal.Paths.project_content_dir()
            cmd = [
                "python", _AI_SCRIPT,
                "--manifest", manifest_path,
                "--ue-content", ue_content,
                "--output", _AI_OUTPUT,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                cwd=os.path.join(_AI_REPO_ROOT, "src", "ai_layer"),
            )
            if proc.returncode == 0 and os.path.exists(_AI_OUTPUT):
                unreal.log(f"[AI] {zip_name} 分析完成，写入 {_AI_OUTPUT}")
            else:
                unreal.log_warning(f"[AI] {zip_name} 调用失败 code={proc.returncode}: {proc.stderr[:300]}")
        except subprocess.TimeoutExpired:
            unreal.log_warning(f"[AI] {zip_name} 超时（180s），降级为纯规则校验")
        except Exception as e:
            unreal.log_warning(f"[AI] {zip_name} 调用异常，降级为纯规则校验: {e}")
    else:
        unreal.log_warning(f"[AI] AI 脚本不存在: {_AI_SCRIPT}")

    rules = asset_validator.load_rules()
    results = {"passed": [], "failed": [], "renamed": []}

    for asset_path in assets:
        _process_asset(asset_path, rules, results)

    asset_validator.generate_reports(results)

    # 标记已处理：只改名（保留文件），清单 import_manifest.json 保留不删除
    os.replace(trigger_path, trigger_path + ".done")

    try:
        unreal.EditorDialog.show_message(
            "资产管线：校验完成（{0}）".format(zip_name),
            "通过 {0} 个，失败 {1} 个，重命名 {2} 个。".format(
                len(results["passed"]), len(results["failed"]), len(results["renamed"])),
            unreal.AppMsgType.OK,
        )
    except Exception as e:
        unreal.log_warning(f"[管线] 弹窗失败: {e}")
    unreal.log(f"[管线] {zip_name} 处理完成：通过 {len(results['passed'])} / 失败 {len(results['failed'])} / 重命名 {len(results['renamed'])}。")


def _process_asset(asset_path, rules, results):
    """校验单个资产并归类：命名错误走重命名，贴图设置错误记为失败，其余记为通过。"""
    tail = asset_path.rsplit("/", 1)[-1]
    name = tail.split(".")[0]

    name_result = asset_validator.validate_naming(asset_path, rules)
    texture_result = asset_validator.validate_texture_settings(asset_path)

    if not name_result["valid"]:
        new_name = name_result["suggested_name"]
        if not new_name or new_name == name:
            results["failed"].append({
                "asset_path": asset_path, "asset_name": name,
                "error_type": "命名错误", "message": name_result["message"],
            })
            return

        directory = asset_path.rsplit("/", 1)[0] if "/" in asset_path else "/Game"
        old_obj = directory + "/" + name + "." + name
        new_obj = directory + "/" + new_name + "." + new_name
        new_path = directory + "/" + new_name

        try:
            if unreal.EditorAssetLibrary.does_asset_exist(new_obj):
                unreal.EditorAssetLibrary.delete_asset(new_obj)
            dup = unreal.EditorAssetLibrary.duplicate_asset(old_obj, new_obj)
            if dup:
                unreal.EditorAssetLibrary.delete_asset(old_obj)
            ok = bool(dup) and not unreal.EditorAssetLibrary.does_asset_exist(old_obj)
        except Exception as e:
            unreal.log_warning(f"[管线] 重命名异常: {asset_path}（{e}）")
            ok = False

        if ok:
            results["renamed"].append({
                "old_path": asset_path, "new_path": new_path,
                "old_name": name, "new_name": new_name,
            })
        else:
            results["failed"].append({
                "asset_path": asset_path, "asset_name": name,
                "error_type": "命名错误", "message": name_result["message"],
            })
        return

    if not texture_result["valid"]:
        results["failed"].append({
            "asset_path": asset_path, "asset_name": name,
            "error_type": "贴图设置错误", "message": texture_result["message"],
        })
        return

    results["passed"].append({"asset_path": asset_path, "asset_name": name})


if __name__ == "__main__":
    check_and_process_pipeline()