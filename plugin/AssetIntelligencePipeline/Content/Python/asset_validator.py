# -*- coding: utf-8 -*-
"""资产校验模块：命名规范校验、贴图设置校验与报告生成。"""

import os
import json
import datetime

import unreal


# 内置默认规则：Config/asset_rules.json 缺失时使用
DEFAULT_RULES = {
    "naming_rules": {
        "StaticMesh": ["SM_"],
        "SkeletalMesh": ["SK_"],
        "Texture2D": ["T_"],
        "Material": ["M_"],
        "MaterialInstanceConstant": ["MI_"],
        "Blueprint": ["BP_"],
    },
    "texture_rules": {
        "_N":   {"expected_srgb": False, "expected_compression": "TC_Normalmap"},
        "_D":   {"expected_srgb": True,  "expected_compression": "TC_Default"},
        "_ORM": {"expected_srgb": False, "expected_compression": "TC_Masks"},
    },
}


def load_rules():
    """读取 <项目>/Config/asset_rules.json 规则；文件缺失或解析失败时返回内置默认规则。"""
    config_path = os.path.join(unreal.Paths.project_dir(), "Config", "asset_rules.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            unreal.log_warning(f"[管线] 读取 asset_rules.json 失败，改用默认规则: {e}")
    return DEFAULT_RULES


def validate_naming(asset_path, rules):
    """校验资产命名是否符合其类型对应的前缀规范（如 SM_/T_/M_/MI_/BP_）。

    返回 dict，含字段：
        valid             : 命名是否通过
        asset_class       : 资产类型
        expected_prefixes : 期望前缀列表
        message           : 未通过时的说明
        suggested_name    : 建议的新名称（前缀 + 剥离旧前缀后的语义名）
    """
    name = asset_path.rsplit("/", 1)[-1].split(".")[0]
    class_name = _get_asset_class(asset_path)
    prefixes = rules.get("naming_rules", {}).get(class_name, [])

    # 该类型未配置前缀规则，视为通过（不校验）
    if not prefixes:
        return {
            "valid": True, "asset_class": class_name,
            "expected_prefixes": [], "message": "", "suggested_name": name,
        }

    valid = any(name.startswith(p) for p in prefixes)
    base_name = _strip_known_prefix(name, rules)
    suggested_name = prefixes[0] + base_name

    message = "" if valid else "缺少前缀，期望以 '{}' 开头。".format(" 或 ".join(prefixes))
    return {
        "valid": valid, "asset_class": class_name,
        "expected_prefixes": prefixes,
        "message": message, "suggested_name": suggested_name,
    }


def validate_texture_settings(asset_path):
    """检查贴图（Texture2D）的 sRGB 与压缩设置是否符合其后缀预期。

    非贴图资产直接视为通过。返回 dict，含字段 valid 与 message。
    """
    name = asset_path.rsplit("/", 1)[-1].split(".")[0]
    rules = load_rules()
    texture_rules = rules.get("texture_rules", {})

    try:
        obj = unreal.EditorAssetLibrary.load_asset(asset_path)
    except Exception:
        obj = None

    if obj is None or not isinstance(obj, unreal.Texture):
        return {"valid": True, "message": ""}

    for suffix, expected in texture_rules.items():
        if not name.endswith(suffix):
            continue

        try:
            actual_srgb = obj.get_editor_property("srgb")
            actual_compression = obj.get_editor_property("compression_settings")
        except Exception as e:
            unreal.log_warning(f"[管线] 读取贴图设置失败: {asset_path}（{e}）")
            return {"valid": True, "message": ""}

        expected_srgb = expected["expected_srgb"]
        expected_comp_str = expected["expected_compression"]
        expected_compression = getattr(unreal.TextureCompressionSettings, expected_comp_str, None)

        issues = []
        if actual_srgb != expected_srgb:
            issues.append("sRGB 期望 {0}，实际 {1}".format(expected_srgb, actual_srgb))
        if expected_compression is not None and actual_compression != expected_compression:
            issues.append("压缩期望 {0}，实际 {1}".format(expected_comp_str, actual_compression))

        if issues:
            return {"valid": False, "message": "；".join(issues)}
        return {"valid": True, "message": ""}

    # 后缀不在规则内，不校验
    return {"valid": True, "message": ""}


def generate_reports(results):
    """把校验结果（passed/failed/renamed）输出为 JSON 与 Markdown 报告。

    报告写入 <项目>/Saved/AssetPipeline/Reports/，返回报告目录路径。
    """
    report_dir = os.path.join(unreal.Paths.project_dir(), "Saved", "AssetPipeline", "Reports")
    try:
        os.makedirs(report_dir, exist_ok=True)
    except OSError as e:
        unreal.log_error(f"[管线] 创建报告目录失败: {e}")
        return report_dir

    json_path = os.path.join(report_dir, "validation_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4, ensure_ascii=False)

    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    passed = results.get("passed", [])
    failed = results.get("failed", [])
    renamed = results.get("renamed", [])

    md_path = os.path.join(report_dir, "validation_report.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# 资产校验报告\n\n")
        f.write(f"- 生成时间：{ts}\n")
        f.write(f"- 通过：{len(passed)}，失败：{len(failed)}，重命名：{len(renamed)}\n\n")

        f.write("## 重命名\n\n")
        if renamed:
            f.write("| 旧路径 | 新路径 |\n")
            f.write("| :--- | :--- |\n")
            for r in renamed:
                f.write(f"| `{r['old_path']}` | `{r['new_path']}` |\n")
        else:
            f.write("无。\n")

        f.write("\n## 失败项\n\n")
        if failed:
            f.write("| 资产路径 | 错误分类 | 描述 |\n")
            f.write("| :--- | :--- | :--- |\n")
            for item in failed:
                f.write(f"| `{item['asset_path']}` | {item.get('error_type', '')} | {item.get('message', '')} |\n")
        else:
            f.write("无。\n")

    return report_dir


def _get_asset_class(asset_path):
    """通过 AssetData 读取资产类型名；读取失败返回 "Unknown"。"""
    try:
        asset_data = unreal.EditorAssetLibrary.find_asset_data(asset_path)
        if asset_data is None:
            return "Unknown"
        return str(asset_data.asset_class_path.asset_name)
    except Exception:
        return "Unknown"


def _strip_known_prefix(name, rules):
    """剥离资产名最前面已存在的已知前缀（按长度从长到短匹配），返回语义名。"""
    known = []
    for prefixes in rules.get("naming_rules", {}).values():
        known.extend(prefixes)
    known = sorted(set(known), key=len, reverse=True)
    for prefix in known:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


if __name__ == "__main__":
    pass  # 本模块由消费层调用，不单独作为入口