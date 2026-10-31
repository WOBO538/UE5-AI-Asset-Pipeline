# -*- coding: utf-8 -*-

from ast import Set
import os
import re
import sys
import time
import json
from typing import Any
import zipfile
import shutil
import configparser


# ==========================================
# 配置层
# ==========================================

# 轮询扫描 Inbox 的间隔（秒）—— 主循环 while True + time.sleep(该值)
POLL_INTERVAL = 5.0


def _get_base_dir():
    """定位 config.ini 所在目录：打包成 exe 后取 exe 所在目录，开发态取脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# config.ini 固定与脚本（或 exe）同目录，路径不硬编码
BASE_DIR = _get_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")


def load_config() -> dict:
    """读取同目录 config.ini，返回路径配置字典。若配置不存在或无效，调用 init_config_by_gui()。

    UE 工程路径解析优先级（自高到低）：
        1. 环境变量 UE_PROJECT_DIR（.uproject 所在目录，自动推导 <dir>/Content）—— 换工程 / CI 零配置
        2. config.ini 的 [Paths] UE_PROJECT_CONTENT

    返回的 dict 期望包含以下键（供下层各函数使用）：
        pipeline_root     : 管线工作根目录（含 Inbox/Processing/Archive）
        ue_project_content: UE 项目的 Content 目录
        inbox             : 待处理 Zip 的投放目录
        processing        : 解压中转目录
        archive           : 归档目录
        imported          : 资产最终复制位置（UE_PROJECT_CONTENT/Imported）
    """
    # ① 环境变量 UE_PROJECT_DIR 优先
    env_dir = os.environ.get("UE_PROJECT_DIR", "").strip()
    env_content = None
    if env_dir:
        candidate = os.path.join(env_dir, "Content")
        if os.path.isdir(candidate):
            env_content = candidate
            print(f"[配置] 使用环境变量 UE_PROJECT_DIR -> {candidate}")
        else:
            print(f"[配置] 警告：UE_PROJECT_DIR={env_dir} 下未找到 Content 目录，已忽略，回退 config.ini。")

    # ② 无 config.ini 时：有环境变量可直接开工（工作根默认项目同盘 Asset_Pipeline），否则走 GUI 向导
    if not os.path.exists(CONFIG_PATH):
        if env_content:
            drive = os.path.splitdrive(env_content)[0]
            pipeline_root = f"{drive}/Asset_Pipeline"
            _write_config(pipeline_root, env_content)
            return _build_paths(pipeline_root, env_content)
        return init_config_by_gui()

    # ③ config.ini 读取（环境变量命中时覆盖 UE_PROJECT_CONTENT）
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH, encoding="utf-8")
    pipeline_root = ""
    ue_content = env_content
    if cfg.has_section("Paths"):
        pipeline_root = cfg["Paths"].get("PIPELINE_ROOT", "").strip()
        if not ue_content:
            ue_content = cfg["Paths"].get("UE_PROJECT_CONTENT", "").strip()

    # ④ 校验有效性，无效回退 GUI 向导
    if not ue_content or not os.path.isdir(ue_content):
        print("配置的 UE 工程 Content 路径无效或不存在，启动初始化向导...")
        return init_config_by_gui()

    if not pipeline_root or not os.path.isdir(pipeline_root):
        print("配置的管线工作根目录无效或不存在，启动初始化向导...")
        return init_config_by_gui()

    return _build_paths(pipeline_root, ue_content)


def _build_paths(pipeline_root: str, ue_content: str) -> dict:
    """由工作根目录 + UE Content 拼接出四个子路径，返回路径配置字典。"""
    return {
        "pipeline_root": pipeline_root,
        "ue_project_content": ue_content,
        "inbox": os.path.join(pipeline_root, "Inbox"),
        "processing": os.path.join(pipeline_root, "Processing"),
        "archive": os.path.join(pipeline_root, "Archive"),
        "imported": os.path.join(ue_content, "Imported"),
    }


def _write_config(pipeline_root: str, ue_content: str) -> None:
    """把工作根目录与 UE Content 写入 config.ini（UTF-8）。"""
    cfg = configparser.ConfigParser()
    cfg["Paths"] = {
        "PIPELINE_ROOT": pipeline_root.replace("\\", "/").rstrip("/"),
        "UE_PROJECT_CONTENT": ue_content.replace("\\", "/").rstrip("/"),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)
    print(f"[配置] 已自动写入 config.ini：{CONFIG_PATH}")


def init_config_by_gui() -> dict:
    """弹出 tkinter 文件选择器让用户选 .uproject，推导出 Content 路径，生成 config.ini。

    仅在 load_config 发现配置缺失或无效时被调用，用于"首次启动配置向导"。
    """
    # TODO: 1. 函数内延迟 import tkinter（避免无 GUI 环境加载开销）
    # TODO: 2. 创建隐藏主窗口 root.withdraw() 并置顶
    # TODO: 3. filedialog.askopenfilename 让用户选择 .uproject 文件
    # TODO: 4. 由 .uproject 所在目录推导 Content：<project_root>/Content（不存在则回退提示）
    # TODO: 5. 工作根目录默认 C:/Local_Pipeline，允许用户自选
    # TODO: 6. 以 UTF-8 写入 config.ini（[Paths] 段：PIPELINE_ROOT、UE_PROJECT_CONTENT）
    # TODO: 7. 返回与 load_config 相同结构的 dict
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    print("首次启动配置向导：请选择你的 UE 项目文件 (.uproject)")
    uproject_path = filedialog.askopenfilename(
        title="选择你的 UE 项目文件 (.uproject)",
        filetypes=[("Unreal Project", "*.uproject"), ("All Files", "*.*")]
    )

    if not uproject_path:
        print("用户取消配置，程序退出。")
        sys.exit(0)

    project_root = os.path.dirname(uproject_path)
    ue_content = os.path.join(project_root, "Content")
    if not os.path.isdir(ue_content):
        print(f"错误：在 {project_root} 下找不到 Content 目录。")
        sys.exit(1)

    pipeline_root = filedialog.askdirectory(
            title="选择管线工作根目录（取消则自动使用 UE 项目同盘的 Asset_Pipeline）"
    )
    if not pipeline_root:
        project_drive = os.path.splitdrive(project_root)[0]
        pipeline_root = f"{project_drive}/Asset_Pipeline"

    cfg = configparser.ConfigParser()
    cfg["Paths"] = {
        "PIPELINE_ROOT": pipeline_root.replace("\\", "/"),
        "UE_PROJECT_CONTENT": ue_content.replace("\\", "/"),
    }
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cfg.write(f)

    print(f"配置已保存：{CONFIG_PATH}")

    return _build_paths(pipeline_root, ue_content)


# ==========================================
# 工具函数层
# ==========================================

def ensure_directories(paths: dict):
    """确保 Inbox / Processing / Archive 三个目录存在。

    参数 paths 为 load_config / init_config_by_gui 返回的路径字典。
    """
    # TODO: 1. 依次取 paths["inbox"]、paths["processing"]、paths["archive"]
    # TODO: 2. os.makedirs(dir, exist_ok=True) 逐个创建
    os.makedirs(paths["inbox"], exist_ok=True)
    os.makedirs(paths["processing"], exist_ok=True)
    os.makedirs(paths["archive"], exist_ok=True)
    os.makedirs(paths["imported"], exist_ok=True)  # 顺手把这个也建了，后面要用来放资产


def is_file_complete(zip_path: str, timeout: int = 30) -> bool:
    """轮询文件大小是否稳定，判断 Zip 是否写入完成。

    通过多次采样文件大小、连续两次一致即认为写入完成；超时仍未稳定返回 False。
    """
    # TODO: 1. 初始化 last_size = -1, stable_count = 0, waited = 0
    last_size = -1
    stable_count = 0
    waited = 0
    interval = 1.0
    # TODO: 2. while waited < timeout:
    while waited < timeout:
        try:
            current_size = os.path.getsize(zip_path)
        except OSError:
            time.sleep(interval)
            waited += interval
            continue
        if current_size == last_size and current_size > 0:
            stable_count += 1
            if stable_count >= 2:
                return True
        else:
            last_size = current_size
            stable_count = 0

        time.sleep(interval)
        waited += interval
    # TODO:     - os.path.getsize 读取当前大小（异常则 sleep 后重试）
    # TODO:     - 大小与上次一致则 stable_count += 1，否则重置 last_size / stable_count
    # TODO:     - stable_count >= 2 返回 True
    # TODO:     - 每次循环 sleep 固定间隔并累加 waited
    # TODO: 3. 超时返回 False
    return False


def move_with_retry(src: str, dst: str):
    """归档函数，实现"先复制后删除"策略。

    复制成功后再删除源文件；删除失败（如 Windows 文件被占用）只打印警告，绝不抛异常。
    """
    # TODO: 1. 确保 dst 父目录存在（os.makedirs）
    # TODO: 2. shutil.copy2(src, dst) 先复制
    # TODO: 3. 复制成功后 os.remove(src) 删除原文件
    # TODO: 4. 删除抛 OSError/PermissionError 时仅打印警告，不 raise
        # 确保目标父目录存在
    dst_dir = os.path.dirname(dst)
    os.makedirs(dst_dir, exist_ok=True)

    # 复制文件（copy2 会同时复制文件的创建时间等元数据）
    shutil.copy2(src, dst)
        # 尝试删除源文件
    try:
        os.remove(src)
    except (OSError, PermissionError) as e:
        print(f"警告：源文件删除失败，但不影响主流程。原因：{e}")


def _dedupe_path(path: str) -> str:
    """目标路径已存在时自动追加 _1 / _2 后缀，避免同名文件互相覆盖。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _normalize_loose_zip(extract_dir: str) -> str:
    """散装包智能容错：解压内容中没有 Content 目录时，自动创建 Content 并把所有内容整体收纳。

    策略（不对使用者设门槛，美术随手打的包也能跑）：
        1. 在解压目录下新建 Content/
        2. 把解压出来的所有条目（文件 + 子文件夹）原样整体移入 Content/，保留原有目录结构
        3. 同名条目自动加 _1 / _2 后缀，避免覆盖
    随后照常进入 manifest 生成流程；返回 Content 目录路径。
    """
    content_dir = os.path.join(extract_dir, "Content")
    os.makedirs(content_dir, exist_ok=True)

    for entry in os.listdir(extract_dir):
        if entry == "Content":
            continue  # 跳过刚创建的目标目录自身
        src = os.path.join(extract_dir, entry)
        dst = _dedupe_path(os.path.join(content_dir, entry))
        shutil.move(src, dst)

    print("[容错] 未检测到 Content 目录，已自动创建并收纳所有资产。")
    return content_dir


def _find_first_image(root_dir: str):
    """递归找第一张可用图片（.png/.jpg/.jpeg/.tga），无则返回 None。

    按完整路径排序，保证"第一张"语义确定（跨机器结果一致）。
    """
    hits = []
    for dirpath, _dirs, files in os.walk(root_dir):
        for f in files:
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".tga")):
                hits.append(os.path.join(dirpath, f))
    return sorted(hits)[0] if hits else None


def _sanitize_prefix(name: str) -> str:
    """复用生成层的统一命名规则（保留中文等 Unicode 字符），避免两处实现漂移。"""
    from comfy_client import sanitize_prefix  # 延迟导入：保持本模块可独立启动
    return sanitize_prefix(name)


# ==========================================
# 核心处理层
# ==========================================

def process_zip(zip_path: str, paths: dict):
    """核心处理函数：解压资产包并导入 UE 项目、生成清单与触发器、最后归档。

    处理顺序（严格按此执行）：
        ① 解压 Zip 到 Processing/{name}/
        ② 递归查找解压内容里的 Content 文件夹
        ③ 复制 Content 内的资产到 UE_PROJECT_CONTENT/Imported/
        ④ 【优先】生成 import_manifest.json（格式 {"assets": ["/Game/Imported/xxx", ...]}）
        ⑤ 【优先】生成 trigger.txt（内容为 "ready"）
        ⑥ 【最后】尝试归档 Zip 到 Archive/ —— 必须 try-except 包裹，
            捕获 PermissionError 后只打印警告，绝不 raise 阻断主流程
    """
    # TODO: 1. name = 去掉 .zip 后缀的 basename
    # TODO: 2. is_file_complete(zip_path) 等待写入完成，超时则打印警告并 return
    # TODO: 3. 创建 Processing/{name}/ 目录
    # TODO: 4. zipfile.ZipFile(...).extractall() 解压到 Processing/{name}/
    # TODO: 5. 递归查找解压目录内名为 Content 的文件夹
    # TODO: 6. 复制 Content 下内容到 paths["imported"]，并记录相对路径
    # TODO: 7. 【优先】基于复制的资产生成 "/Game/Imported/..." 清单（仅 .uasset/.umap，去扩展名）
    # TODO: 8. 写 import_manifest.json（UTF-8，indent=4，ensure_ascii=False）
    # TODO: 9. 【优先】写 trigger.txt，内容 "ready"
    # TODO: 10.【最后】try: move_with_retry(zip_path, archive 目标)
    #         except PermissionError: 打印警告（绝不 raise）
    # 1. 算出名字（例如 "M4_Rifle.zip" 变成 "M4_Rifle"）
    name = os.path.splitext(os.path.basename(zip_path))[0]

    # 2. 检查文件是否写入完成
    if not is_file_complete(zip_path, timeout=60):
        print(f"警告：{name}.zip 写入超时，跳过处理。")
        return

    # 3. 准备临时解压目录
    extract_dir = os.path.join(paths["processing"], name)
    os.makedirs(extract_dir, exist_ok=True)

    # 4. 解压
    try:
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(extract_dir)
        print(f"解压成功：{name}")
    except zipfile.BadZipFile:
        print(f"错误：{name}.zip 损坏或不是有效的压缩包。")
        return
    # 5. 递归寻找 Content 文件夹
    content_dir = None
    for root, dirs, files in os.walk(extract_dir):
        if "Content" in dirs:
            content_dir = os.path.join(root, "Content")
            break  # 找到第一个就退出循环，别往下找了

    if not content_dir:
        # 智能容错：散装包自动创建 Content 并整体收纳所有资产，再继续跑清单流程
        content_dir = _normalize_loose_zip(extract_dir)
        if not content_dir:
            return
    # 6. 复制资产并记录清单
    imported_assets = []  # 记录 UE 清单的数组

    for root, dirs, files in os.walk(content_dir):
        for file in files:
            src_file = os.path.join(root, file)
            # 计算相对路径（例如 Weapon/M4.uasset）
            rel_path = os.path.relpath(src_file, content_dir)
            # 拼出目标路径
            dst_file = os.path.join(paths["imported"], rel_path)

            # 创建目标子文件夹
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            # 复制文件
            shutil.copy2(src_file, dst_file)

    # 7. 判断扩展名，记录 UE 虚拟路径（常见 UE 资产格式全覆盖，原始美术资产 .fbx/.png/.tga 等不再被忽略）
            if file.lower().endswith(('.uasset', '.umap', '.fbx', '.obj', '.png', '.tga', '.jpg')):
                # 把反斜杠转成正斜杠，并去掉后缀
                game_path = "/Game/Imported/" + rel_path.replace("\\", "/")
                game_path = os.path.splitext(game_path)[0]
                imported_assets.append(game_path)

    print(f"共复制 {len(imported_assets)} 个 UE 资产。")

    # 8.【V3 x P2】PBR 生成：找到第一张输入图 → ComfyUI CHORD → 5 张贴图并入清单
    # 成功：追加带标签的 PBR 条目；失败/异常：仅打印警告，继续用原图触发 UE，绝不阻断
    try:
        first_image = _find_first_image(content_dir) or _find_first_image(extract_dir)
        if first_image:
            print(f"[AI生成] 检测到输入图：{first_image}")
            from comfy_client import ComfyUIClient  # 延迟 import：单文件即模块，故障也只走兜底

            result = ComfyUIClient().generate_pbr(
                first_image,
                output_dir=os.path.join(paths["imported"], "PBR", name),
                prefix=_sanitize_prefix(name),
            )
            if result.get("status") == "success":
                for f in result.get("files", []):
                    base = os.path.splitext(os.path.basename(f))[0]
                    imported_assets.append({
                        "path": f"/Game/Imported/PBR/{name}/{base}",
                        "is_generated": True,
                        "type": "PBR",
                    })
                print(f"[AI生成] 成功：{len(result.get('files', []))} 张 PBR 贴图已加入清单。")
            else:
                print(f"[AI生成] 降级兜底：{result.get('reason')}（继续用原图触发 UE）")
        else:
            print("[AI生成] 包内未找到图片（.png/.jpg/.tga），跳过 PBR 生成。")
    except Exception as e:
        print(f"[AI生成] 异常降级兜底：{e}（继续用原图触发 UE）")

        # 9. 写 manifest 和 trigger 到 UE 项目的 Saved/AssetPipeline/Processing/{zip_name}/
    ue_project_root = os.path.dirname(paths["ue_project_content"])
    ue_processing_dir = os.path.join(ue_project_root, "Saved", "AssetPipeline", "Processing", name)
    os.makedirs(ue_processing_dir, exist_ok=True)

    manifest_path = os.path.join(ue_processing_dir, "import_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"assets": imported_assets}, f, indent=4, ensure_ascii=False)

    trigger_path = os.path.join(ue_processing_dir, "trigger.txt")
    with open(trigger_path, "w", encoding="utf-8") as f:
        f.write("ready")

    # 10. 最后归档 Zip
    today = time.strftime("%Y-%m-%d")
    archive_target = os.path.join(paths["archive"], today, os.path.basename(zip_path))
    try:
        move_with_retry(zip_path, archive_target)
        print(f"归档成功：{name}.zip")
    except Exception as e:
        print(f"警告：归档 {name}.zip 失败，但不影响已处理的资产。原因：{e}")


# ==========================================
# 主循环层
# ==========================================

def scan_and_process(paths: dict):
    """主轮询循环：扫描 Inbox 里的 .zip 文件，用 processed_set 避免重复处理。

    采用 while True + time.sleep(5) 轮询模式（不使用 watchdog）。
    """
    # TODO: 1. processed_set = set()  —— 无论成败都登记，防止重复处理
    # TODO: 2. 循环体（while True）：
    #         - os.listdir(paths["inbox"]) 扫描（异常打印并 continue）
    #         - 过滤 .zip 文件
    #         - 已在 processed_set 中则跳过，否则加入
    #         - try/except 包裹 process_zip，异常只打印并跳过该文件
    #         - 每轮结束 time.sleep(POLL_INTERVAL)
    #
    # 以下为主循环骨架（仅结构，内部逻辑待填充）：
    
    processed_set = []  # 用列表替代集合
    print(f"开始监听文件夹：{paths['inbox']}")

    while True:
        try:
            files = os.listdir(paths["inbox"])
        except OSError as e:
            print(f"扫描目录失败：{e}")
            time.sleep(POLL_INTERVAL)
            continue

        for file in files:
            # 只处理 .zip 文件
            if not file.lower().endswith(".zip"):
                continue
            # 已经在处理记录里，跳过
            if file in processed_set:
                continue

            # 登记并处理
            processed_set.append(file)  # 列表用 append，集合用 add
            zip_path = os.path.join(paths["inbox"], file)
            print(f"发现新文件：{file}")
            try:
                process_zip(zip_path, paths)
            except Exception as e:
                print(f"处理 {file} 时发生异常：{e}")

        time.sleep(POLL_INTERVAL)


def main():
    """程序入口：加载配置 → 确保目录 → 进入轮询主循环。"""
    # TODO: 1. paths = load_config()
    # TODO: 2. ensure_directories(paths)
    # TODO: 3. 打印监控目录 / 导入目录信息
    # TODO: 4. scan_and_process(paths)
    # TODO: 5. 用 try/except KeyboardInterrupt 捕获 Ctrl+C，打印"轮询已停止"
    paths = load_config()
    ensure_directories(paths)
    
    print("=" * 40)
    print(f"监听目录 (Inbox): {paths['inbox']}")
    print(f"导入目录 (Imported): {paths['imported']}")
    print("管线已启动，按 Ctrl+C 退出...")
    print("=" * 40)

    try:
        scan_and_process(paths)
    except KeyboardInterrupt:
        print("\n轮询已停止，管线退出。")


if __name__ == "__main__":
    main()