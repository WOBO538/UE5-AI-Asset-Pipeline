# -*- coding: utf-8 -*-
"""资产管线 UE 消费层：读取 Imported 目录的 trigger.txt 与清单，驱动校验、重命名与报告。"""

import os
import json
import time
import subprocess

import unreal
import asset_validator


def _resolve_ai_repo_root():
    """动态定位 AI 层仓库根目录（不写死绝对路径，换电脑 / 移文件夹均可自动适配）。

    优先级：
      1. 环境变量 AI_PIPELINE_ROOT（部署到其他 UE 项目时手动指定）
      2. 相对路径推算：从本文件所在目录逐级向上，找包含 src/ai_layer/analyze.py 的目录
      3. 都找不到：返回 None，并输出带操作指引的明确报错（AI 分析降级为纯规则校验，管线不中断）

    返回仓库根目录（其下应存在 src/ai_layer/analyze.py），失败返回 None。
    """
    # 1) 环境变量优先
    env_root = os.environ.get("AI_PIPELINE_ROOT", "").strip()
    if env_root:
        if os.path.exists(os.path.join(env_root, "src", "ai_layer", "analyze.py")):
            return env_root
        unreal.log_warning(
            f"[AI] 环境变量 AI_PIPELINE_ROOT 指向的路径无效（未找到 src/ai_layer/analyze.py）："
            f"{env_root}，已回退相对路径推算。")

    # 2) 相对路径推算：逐级向上找 src/ai_layer 标志目录，不写死层级数
    script_file = globals().get("__file__")
    cur = os.path.dirname(os.path.abspath(script_file)) if script_file else os.getcwd()
    while True:
        if os.path.exists(os.path.join(cur, "src", "ai_layer", "analyze.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:  # 已到磁盘根目录仍未找到
            break
        cur = parent

    # 3) 推算失败：明确报错 + 操作指引，AI 层降级为纯规则校验
    unreal.log_error(
        "[AI] 无法定位 AI 层仓库根目录（该目录下应存在 src/ai_layer/analyze.py）。"
        "请在系统环境变量中设置 AI_PIPELINE_ROOT 指向管线仓库根目录"
        "（例如 D:\\Projects\\UE5-AI-Asset-Pipeline），"
        "或将本插件目录放置在仓库根目录的 plugin/ 子目录下。"
        "本轮 AI 分析降级为纯规则校验。")
    return None


# 模块加载时解析一次；解析失败为 None，由调用处降级为纯规则校验
_AI_REPO_ROOT = _resolve_ai_repo_root()


# ---------------------------------------------------------------------------
# V3 PBR 材质生成：常量
# ---------------------------------------------------------------------------
_PBR_ROOT = "/Game/Imported/PBR/"
_PBR_SUFFIXES = ("_BaseColor", "_Normal", "_Roughness", "_Metalness", "_Height")
_PBR_PARAM_NAMES = {
    "_BaseColor": "BaseColor",
    "_Normal": "Normal",
    "_Roughness": "Roughness",
    "_Metalness": "Metalness",
    "_Height": "Height",
}
_MASTER_MATERIAL_DIR = "/Game/AssetPipeline/MasterMaterials"
_MASTER_MATERIAL_NAME = "M_Master_PBR"


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
    # 仓库根目录由模块级 _resolve_ai_repo_root() 动态解析（AI_PIPELINE_ROOT -> 相对路径推算）
    if _AI_REPO_ROOT:
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
    else:
        unreal.log_warning(f"[AI] {zip_name} 仓库根目录解析失败，跳过 AI 分析（沿用纯规则校验）。")

    rules = asset_validator.load_rules()
    results = {"passed": [], "failed": [], "renamed": [], "materials": []}

    # 兼容清单元素混存（纯字符串 / 带标签字典）：统一解析成 {"path", "meta", "is_pbr"}
    if not isinstance(assets, list):
        unreal.log_warning(f"[管线] {zip_name} 清单 assets 字段不是数组，已按空清单处理。")
        assets = []
    parsed_assets = []
    for item in assets:
        parsed = _parse_manifest_asset(item)
        if parsed is None:
            # 无法识别的元素只告警跳过，绝不中断整批导入
            unreal.log_warning(f"[管线] {zip_name} 清单中存在无法识别的条目，已跳过：{item!r}")
            continue
        parsed_assets.append(parsed)

    pbr_items = [a for a in parsed_assets if a["is_pbr"]]
    for item in parsed_assets:
        if item["is_pbr"]:
            continue  # AI 生成贴图走 PBR 分支（导入贴图 + 生成材质），不参与命名重命名
        try:
            _process_asset(item["path"], rules, results)
        except Exception as e:
            # 单个资产异常只记录，不影响其余资产
            unreal.log_warning(f"[管线] 资产处理异常（已跳过）：{item['path']}（{e}）")

    if pbr_items:
        _process_pbr_assets(pbr_items, results)

    asset_validator.generate_reports(results)

    # 标记已处理：只改名（保留文件），清单 import_manifest.json 保留不删除
    os.replace(trigger_path, trigger_path + ".done")

    if _can_show_dialog():
        try:
            unreal.EditorDialog.show_message(
                "资产管线：校验完成（{0}）".format(zip_name),
                "通过 {0} 个，失败 {1} 个，重命名 {2} 个，生成材质 {3} 个。".format(
                    len(results["passed"]), len(results["failed"]), len(results["renamed"]),
                    len(results.get("materials", []))),
                unreal.AppMsgType.OK,
            )
        except Exception as e:
            unreal.log_warning(f"[管线] 弹窗失败: {e}")
    else:
        unreal.log(f"[管线] {zip_name} 处于无界面/命令行模式，跳过弹窗通知。")
    unreal.log(f"[管线] {zip_name} 处理完成：通过 {len(results['passed'])} / 失败 {len(results['failed'])} / "
               f"重命名 {len(results['renamed'])} / 材质 {len(results.get('materials', []))}。")


def _can_show_dialog():
    """判断能否弹 Slate 模态框：命令行/无界面模式（-run= / -unattended / -nullrhi）下禁止。

    无界面模式下 SlateApplication 未初始化，弹窗会触发 C++ 断言崩溃（不是 Python 异常），
    因此必须在调用前用命令行参数判断，而不是靠 try/except。
    """
    try:
        cmd = str(unreal.SystemLibrary.get_command_line()).lower()
    except Exception:
        return True
    for flag in ("-run=", "-unattended", "-nullrhi"):
        if flag in cmd:
            return False
    return True


def _parse_manifest_asset(item):
    """兼容清单元素的两种形态，统一解析为 {"path": str, "meta": dict, "is_pbr": bool}。

    - 纯字符串："/Game/Imported/cobblestone_color_diff_1k"
    - 带标签字典：{"path": "/Game/Imported/PBR/<name>/<prefix>_BaseColor",
                   "is_generated": true, "type": "PBR"}
    无法识别（None / 数字 / 缺 path 的字典 / 空字符串）返回 None，由调用方告警跳过。
    """
    if isinstance(item, str):
        path = item.strip()
        if not path:
            return None
        return {"path": path, "meta": {}, "is_pbr": path.startswith(_PBR_ROOT)}

    if isinstance(item, dict):
        path = item.get("path") or item.get("asset_path")
        if not isinstance(path, str) or not path.strip():
            return None
        path = path.strip()
        is_pbr = (
            str(item.get("type", "")).upper() == "PBR"
            or bool(item.get("is_generated"))
            or path.startswith(_PBR_ROOT)
        )
        return {"path": path, "meta": item, "is_pbr": is_pbr}

    return None


def _match_pbr_suffix(name):
    """返回贴图名命中的 PBR 后缀（_BaseColor 等），未命中返回 None。"""
    for suffix in _PBR_SUFFIXES:
        if name.endswith(suffix):
            return suffix
    return None


def _content_file_for_asset(asset_path):
    """把 /Game/xxx 映射回 Content 目录下的源文件（.png/.jpg/.jpeg/.tga），找不到返回 None。"""
    if not asset_path.startswith("/Game/"):
        return None
    try:
        content_root = unreal.Paths.project_content_dir()
    except Exception:
        return None
    rel = asset_path[len("/Game/"):].replace("/", os.sep)
    base = os.path.splitext(os.path.join(content_root.rstrip("/\\"), rel))[0]
    for ext in (".png", ".jpg", ".jpeg", ".tga"):
        if os.path.isfile(base + ext):
            return base + ext
    return None


def _ensure_texture_asset(asset_path):
    """确保贴图在 UE 里已作为 Texture2D 资产存在；不存在则用 Content 下的源图导入。

    返回 True/False；任何失败都只记日志，不抛异常。
    """
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            return True
    except Exception as e:
        unreal.log_warning(f"[材质] 查询资产存在性失败（继续尝试导入）：{asset_path}（{e}）")

    source = _content_file_for_asset(asset_path)
    if not source:
        unreal.log_warning(f"[材质] 贴图资产不存在，且 Content 下没有同名源文件，无法导入：{asset_path}")
        return False

    task_cls = getattr(unreal, "AssetImportTask", None)
    tools_helper = getattr(unreal, "AssetToolsHelpers", None)
    tools = tools_helper.get_asset_tools() if tools_helper is not None else None
    if task_cls is None or tools is None or not callable(getattr(tools, "import_asset_tasks", None)):
        unreal.log_error("[材质] 当前引擎缺少 AssetImportTask / import_asset_tasks，无法导入 AI 贴图。")
        return False

    try:
        task = task_cls()
        task.filename = source
        task.destination_path = asset_path.rsplit("/", 1)[0]
        task.destination_name = asset_path.rsplit("/", 1)[-1].split(".")[0]
        task.replace_existing = False
        task.automated = True
        # 不让 AssetTools 自动保存（其失败时会弹 Slate 模态框，无界面模式会断言崩溃），
        # 改由本模块 _apply_texture_settings 里显式 save_loaded_asset 落盘
        task.save = False
        tools.import_asset_tasks([task])
    except Exception as e:
        unreal.log_error(f"[材质] 导入 AI 贴图失败：{asset_path}（{e}）")
        return False

    try:
        if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            unreal.log(f"[材质] 已导入 AI 贴图：{asset_path}（源文件 {source}）")
            return True
    except Exception:
        pass
    unreal.log_warning(f"[材质] 导入完成但资产仍不存在：{asset_path}")
    return False


def _apply_texture_settings(asset_path, suffix):
    """按后缀设置贴图 sRGB / 压缩：BaseColor 开 sRGB，Normal 用 Normalmap，其余关 sRGB。"""
    try:
        tex = unreal.EditorAssetLibrary.load_asset(asset_path)
        if tex is None:
            return
        srgb = suffix == "_BaseColor"
        tex.set_editor_property("srgb", srgb)
        comp_enum = getattr(unreal, "TextureCompressionSettings", None)
        comp = getattr(comp_enum, "TC_Normalmap" if suffix == "_Normal" else "TC_Default", None) \
            if comp_enum is not None else None
        if comp is not None:
            tex.set_editor_property("compression_settings", comp)
        save_loaded = getattr(unreal.EditorAssetLibrary, "save_loaded_asset", None)
        if callable(save_loaded):
            save_loaded(tex)
        unreal.log(f"[材质] 贴图属性已设置（sRGB={srgb}）：{asset_path}")
    except Exception as e:
        unreal.log_warning(f"[材质] 设置贴图属性失败（忽略）：{asset_path}（{e}）")


def _material_editing_library():
    """getattr 探测材质编辑 API（不同 5.x 小版本可能存在差异）；不可用返回 None。"""
    mel = getattr(unreal, "MaterialEditingLibrary", None)
    if mel is None:
        return None
    for name in ("create_material_expression", "connect_material_property"):
        if not callable(getattr(mel, name, None)):
            return None
    return mel


def _save_asset(asset, asset_path):
    """把资产保存到磁盘（优先 save_loaded_asset，失败退化为 save_asset）。返回是否成功。"""
    save_loaded = getattr(unreal.EditorAssetLibrary, "save_loaded_asset", None)
    if callable(save_loaded):
        try:
            return bool(save_loaded(asset))
        except Exception as e:
            unreal.log_warning(f"[材质] save_loaded_asset 失败，改用 save_asset：{asset_path}（{e}）")
    try:
        return bool(unreal.EditorAssetLibrary.save_asset(asset_path))
    except Exception as e:
        unreal.log_error(f"[材质] 资产保存失败：{asset_path}（{e}）")
        return False


def _ensure_master_material():
    """确保母材质存在，返回其资产路径；创建/保存失败返回 None（只记日志，不抛异常）。

    母材质结构：5 个 TextureSampleParameter2D（BaseColor/Normal/Roughness/Metalness/Height）
    → BaseColor 接 Base Color，Normal（Normalmap 采样）接 Normal，Roughness/Metalness 取 R 通道
    分别接 Roughness/Metallic；Height 参数暂不接输出（可选扩展，美术可自行使用）。
    """
    master_path = f"{_MASTER_MATERIAL_DIR}/{_MASTER_MATERIAL_NAME}"
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(master_path):
            return master_path
    except Exception:
        pass

    mel = _material_editing_library()
    material_cls = getattr(unreal, "Material", None)
    factory_cls = getattr(unreal, "MaterialFactoryNew", None)
    tex_expr_cls = getattr(unreal, "MaterialExpressionTextureSampleParameter2D", None)
    prop_enum = getattr(unreal, "MaterialProperty", None)
    tools_helper = getattr(unreal, "AssetToolsHelpers", None)
    if (mel is None or material_cls is None or factory_cls is None
            or tex_expr_cls is None or prop_enum is None or tools_helper is None):
        unreal.log_error("[材质] 当前引擎缺少创建母材质所需 API，跳过材质生成（不影响其余资产）。")
        return None

    try:
        master = tools_helper.get_asset_tools().create_asset(
            _MASTER_MATERIAL_NAME, _MASTER_MATERIAL_DIR, material_cls, factory_cls())
        if master is None:
            unreal.log_error(f"[材质] 母材质创建失败：{master_path}")
            return None

        wiring = (
            ("BaseColor", "MP_BASE_COLOR", "", -300),
            ("Normal", "MP_NORMAL", "", -100),
            ("Roughness", "MP_ROUGHNESS", "R", 100),
            ("Metalness", "MP_METALLIC", "R", 300),
            ("Height", None, "", 500),
        )
        for param_name, prop_name, out_name, pos_y in wiring:
            expr = mel.create_material_expression(master, tex_expr_cls, -600, pos_y)
            try:
                expr.set_editor_property("parameter_name", param_name)
            except Exception as e:
                unreal.log_warning(f"[材质] 设置参数名失败（{param_name}）：{e}")
            if param_name == "Normal":
                try:
                    sampler_enum = getattr(unreal, "MaterialSamplerType", None)
                    if sampler_enum is not None:
                        expr.set_editor_property("sampler_type", sampler_enum.SAMPLERTYPE_NORMAL)
                except Exception as e:
                    unreal.log_warning(f"[材质] 设置法线采样类型失败（忽略）：{e}")
            if prop_name is None:
                continue  # Height：仅作为参数储备
            material_property = getattr(prop_enum, prop_name, None)
            if material_property is None:
                unreal.log_warning(f"[材质] 材质属性 {prop_name} 不可用，跳过该输出连接。")
                continue
            try:
                mel.connect_material_property(expr, out_name, material_property)
            except Exception as e:
                unreal.log_warning(f"[材质] 连接 {param_name} -> {prop_name} 失败：{e}")

        layout = getattr(mel, "layout_material_expressions", None)
        if callable(layout):
            try:
                layout(master)
            except Exception:
                pass
        recompile = getattr(mel, "recompile_material", None)
        if callable(recompile):
            try:
                recompile(master)
            except Exception as e:
                unreal.log_warning(f"[材质] 母材质重新编译失败（忽略）：{e}")

        if _save_asset(master, master_path):
            unreal.log(f"[材质] 已生成母材质并保存到磁盘：{master_path}")
        else:
            unreal.log_error(f"[材质] 母材质保存失败：{master_path}")
        return master_path
    except Exception as e:
        unreal.log_error(f"[材质] 创建母材质异常，材质生成降级跳过：{e}")
        return None


def _create_material_instance(directory, prefix, textures):
    """为同一目录/同一前缀的贴图组创建 MI（<prefix>_M）；已存在则跳过（幂等）。"""
    mi_name = f"{prefix}_M"
    mi_path = f"{directory}/{mi_name}"
    try:
        if unreal.EditorAssetLibrary.does_asset_exist(mi_path):
            unreal.log(f"[材质] MI 已存在，跳过重建（幂等，避免覆盖美术手调参数）：{mi_path}")
            return mi_path
    except Exception:
        pass

    master_path = _ensure_master_material()
    if not master_path:
        return None

    mel = _material_editing_library()
    factory_cls = getattr(unreal, "MaterialInstanceConstantFactoryNew", None)
    mi_cls = getattr(unreal, "MaterialInstanceConstant", None)
    set_parent = getattr(mel, "set_material_instance_parent", None) if mel is not None else None
    set_tex_param = getattr(mel, "set_material_instance_texture_parameter_value", None) if mel is not None else None
    tools_helper = getattr(unreal, "AssetToolsHelpers", None)
    if (mel is None or factory_cls is None or mi_cls is None or tools_helper is None
            or not callable(set_parent) or not callable(set_tex_param)):
        unreal.log_error("[材质] 当前引擎缺少 MI 创建 API，跳过该材质（不影响其余资产）。")
        return None

    mi = tools_helper.get_asset_tools().create_asset(mi_name, directory, mi_cls, factory_cls())
    if mi is None:
        unreal.log_error(f"[材质] MI 创建失败：{mi_path}")
        return None

    master = unreal.EditorAssetLibrary.load_asset(master_path)
    if master is None:
        unreal.log_error(f"[材质] 母材质加载失败：{master_path}")
        return None
    set_parent(mi, master)

    applied = 0
    for suffix, param_name in _PBR_PARAM_NAMES.items():
        tex_path = textures.get(suffix)
        if not tex_path:
            unreal.log_warning(f"[材质] {mi_name} 缺少 {suffix} 贴图，参数 {param_name} 留空。")
            continue
        try:
            tex = unreal.EditorAssetLibrary.load_asset(tex_path)
            if tex is None:
                unreal.log_warning(f"[材质] 贴图加载失败，跳过参数 {param_name}：{tex_path}")
                continue
            set_tex_param(mi, param_name, tex)
            applied += 1
        except Exception as e:
            unreal.log_warning(f"[材质] 设置参数 {param_name} 失败（跳过）：{e}")

    update = getattr(mel, "update_material_instance", None)
    if callable(update):
        try:
            update(mi)
        except Exception as e:
            unreal.log_warning(f"[材质] 刷新 MI 失败（忽略）：{e}")

    if _save_asset(mi, mi_path):
        unreal.log(f"[材质] 已生成材质实例并保存到磁盘：{mi_path}（贴图参数 {applied} 个）")
    else:
        unreal.log_error(f"[材质] 材质实例保存失败：{mi_path}")
    return mi_path


def _process_pbr_assets(pbr_items, results):
    """导入 AI 生成的 PBR 贴图 → 校验贴图设置 → 按 (目录, 前缀) 分组生成 MI。

    单张贴图 / 单个材质失败只记录日志并继续，绝不中断整批。
    """
    groups = {}
    for item in pbr_items:
        asset_path = item["path"].split(".")[0]
        if "/" in asset_path:
            directory, tail = asset_path.rsplit("/", 1)
        else:
            directory, tail = "/Game", asset_path
        name = tail.split(".")[0]
        suffix = _match_pbr_suffix(name)
        if not suffix:
            unreal.log_warning(f"[材质] 无法识别 PBR 贴图后缀，跳过：{asset_path}")
            continue
        prefix = name[:-len(suffix)]
        if not prefix:
            unreal.log_warning(f"[材质] 贴图缺少前缀，无法归组，跳过：{asset_path}")
            continue

        if not _ensure_texture_asset(asset_path):
            results["failed"].append({
                "asset_path": asset_path, "asset_name": name,
                "error_type": "AI贴图缺失",
                "message": "UE 中找不到该贴图，且无法从 Content 源文件导入",
            })
            continue

        _apply_texture_settings(asset_path, suffix)

        tex_result = asset_validator.validate_texture_settings(asset_path)
        if tex_result.get("valid"):
            results["passed"].append({"asset_path": asset_path, "asset_name": name})
        else:
            results["failed"].append({
                "asset_path": asset_path, "asset_name": name,
                "error_type": "贴图设置错误", "message": tex_result.get("message", ""),
            })

        groups.setdefault((directory, prefix), {})[suffix] = asset_path

    for (directory, prefix), textures in sorted(groups.items()):
        try:
            mi_path = _create_material_instance(directory, prefix, textures)
        except Exception as e:
            unreal.log_error(f"[材质] 生成 {prefix} 材质异常（已跳过，不影响其余资产）：{e}")
            continue
        if mi_path:
            results["materials"].append({
                "material_path": mi_path, "prefix": prefix, "textures": len(textures),
                "parent": f"{_MASTER_MATERIAL_DIR}/{_MASTER_MATERIAL_NAME}",
            })


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