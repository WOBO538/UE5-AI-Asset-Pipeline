# -*- coding: utf-8 -*-
"""ComfyUI HTTP 客户端 —— V3 AI 生成层 与 P2 外部流转层 的对接模块。

职责：
    读取 CHORD 工作流 API 模板 → 替换输入图与输出前缀 → POST /prompt 提交
    → 轮询 /history/{prompt_id} → 从 ComfyUI output 目录回收 5 张 PBR 贴图
    → 复制到指定目录并按后缀命名（_BaseColor/_Normal/_Roughness/_Metalness/_Height）。

设计原则：
    - 只使用 Python 标准库（urllib / json / shutil），不依赖 requests，
      pipeline_watcher 无需额外安装依赖即可 import；
    - generate_pbr 全程 try-except 兜底：ComfyUI 未启动 / 超时 / 任何失败
      一律返回 {"status": "fallback", "reason": "..."}，绝不抛异常阻断主流程。

命令行测试：
    python comfy_client.py --image <输入图绝对路径> --out <输出目录> --prefix mat_01
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ================================================================ 配置常量

# 本机 ComfyUI 服务地址
API_URL = "http://127.0.0.1:8188"

# ComfyUI 的 output 目录（SaveImage 实际落盘位置）
COMFY_OUTPUT_DIR = Path(r"D:\AI\ComfyUI\ComfyUI_windows_portable\ComfyUI\output")

# ComfyUI 的 input 目录（LoadImage 相对路径基准；输入图暂存于此）
COMFY_INPUT_DIR = Path(r"D:\AI\ComfyUI\ComfyUI_windows_portable\ComfyUI\input")

# CHORD 工作流的 API 格式模板（V3 提取产物）
# 解析优先级：环境变量 ASSET_PIPELINE_TEMPLATE > 模块同级 config/
#            > （打包后）exe 同级 config/ > PyInstaller 解包目录 config/
# 不再硬编码桌面路径，换机器 / 桌面文件被移走都不会失效
TEMPLATE_FILENAME = "chord_api_template.json"


def _resolve_template_path() -> Path:
    """按优先级解析工作流模板路径（支持环境变量覆盖，便于部署时替换模板）。"""
    env = os.environ.get("ASSET_PIPELINE_TEMPLATE", "").strip()
    if env:
        return Path(env)

    candidates = []
    if getattr(sys, "frozen", False):
        # 打包后：优先 exe 同级 config/（用户可直接替换），其次 onefile 解包临时目录
        candidates.append(Path(sys.executable).resolve().parent / "config" / TEMPLATE_FILENAME)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "config" / TEMPLATE_FILENAME)
    else:
        candidates.append(Path(__file__).resolve().parent / "config" / TEMPLATE_FILENAME)

    for cand in candidates:
        if cand.is_file():
            return cand
    # 都不存在也返回首选路径：generate_pbr 内部会捕获异常并降级，不阻断主流程
    return candidates[0]


TEMPLATE_PATH = _resolve_template_path()

# 输入图暂存子目录（相对 ComfyUI input 目录）与暂存保留上限
STAGING_SUBDIR = "asset_pipeline"
STAGING_MAX_FILES = 100

# SaveImage 节点 ID -> 输出贴图命名后缀
# 已核对模板：4=BaseColor 5=Normal 6=Roughness 7=Metalness 9=Height
SAVE_NODE_SUFFIX = {
    "4": "_BaseColor",
    "5": "_Normal",
    "6": "_Roughness",
    "7": "_Metalness",
    "9": "_Height",
}

# 生成等待超时（秒）—— 8GB 显存低配机器，宁可早点降级兜底
GENERATE_TIMEOUT = 180

# 轮询 /history 的间隔（秒）
POLL_INTERVAL = 2.0


class ComfyUIClient:
    """向本地 ComfyUI 提交 CHORD 材质估计任务并回收 PBR 贴图。"""

    def __init__(
        self,
        api_url: str = API_URL,
        template_path: str | os.PathLike | None = None,
        comfy_output_dir: str | os.PathLike | None = None,
        comfy_input_dir: str | os.PathLike | None = None,
        timeout: float = GENERATE_TIMEOUT,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.template_path = Path(template_path) if template_path else TEMPLATE_PATH
        self.comfy_output_dir = Path(comfy_output_dir) if comfy_output_dir else COMFY_OUTPUT_DIR
        self.comfy_input_dir = Path(comfy_input_dir) if comfy_input_dir else COMFY_INPUT_DIR
        self.timeout = timeout
        # client_id 会话内一致，便于 WebSocket 侧过滤事件
        self.client_id = uuid.uuid4().hex

    # ------------------------------------------------------------ 内部工具

    def _http_json(self, url: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
        """HTTP JSON 请求：payload 传 None 走 GET，否则 POST JSON。"""
        data = None
        headers = {"Content-Type": "application/json"}
        method = "GET"
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            method = "POST"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def is_ready(self, timeout: float = 2.0) -> bool:
        """健康检查：GET /system_stats 返回 200 视为服务就绪。"""
        try:
            with urllib.request.urlopen(f"{self.api_url}/system_stats", timeout=timeout) as resp:
                return getattr(resp, "status", None) == 200
        except Exception:
            return False

    def _stage_input_image(self, image_path: str | os.PathLike) -> tuple[str, Path]:
        """把输入图暂存进 ComfyUI input 目录，返回 (工作流相对路径, 暂存绝对路径)。

        背景：本机便携版 ComfyUI 对 LoadImage 传「input 目录之外的绝对路径」会做
        目录包含校验并返回 400（Invalid image file），因此统一改为「复制进 input
        子目录 + 提交相对文件名」。命名含源路径哈希，同源文件只复制一次。
        """
        src = Path(image_path).resolve()
        if not src.is_file():
            raise FileNotFoundError(f"输入图片不存在：{src}")
        digest = hashlib.sha256(str(src).encode("utf-8")).hexdigest()[:8]
        # 相对 input 目录的路径，保持正斜杠（JSON 安全 / ComfyUI 约定）
        rel_name = f"{STAGING_SUBDIR}/{digest}_{src.name}"
        staged = Path(self.comfy_input_dir) / rel_name.replace("/", os.sep)
        if not staged.exists() or staged.stat().st_size != src.stat().st_size:
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, staged)
        return rel_name, staged

    def _patch_template(self, image_path: str | os.PathLike, prefix: str) -> tuple[dict, Path]:
        """读取模板并打补丁：LoadImage 输入图 + 5 个 SaveImage 的 filename_prefix。

        返回 (工作流 dict, 本次暂存输入图的绝对路径)；后者用于任务结束后清理。
        """
        with open(self.template_path, "r", encoding="utf-8") as f:
            workflow = json.load(f)

        # ① LoadImage 节点（模板中是 "2"）：输入图统一暂存到 ComfyUI input 后
        #    提交相对路径（正斜杠），规避便携版对 input 目录外绝对路径的校验
        rel_name, staged_path = self._stage_input_image(image_path)
        load_hit = False
        for node in workflow.values():
            if isinstance(node, dict) and node.get("class_type") == "LoadImage":
                node["inputs"]["image"] = rel_name
                load_hit = True
                break
        if not load_hit:
            raise ValueError("模板中没有找到 LoadImage 节点，无法注入输入图")

        # ② 所有 SaveImage 节点：统一写入本次任务的 filename_prefix
        save_count = 0
        for node in workflow.values():
            if isinstance(node, dict) and node.get("class_type") == "SaveImage":
                node["inputs"]["filename_prefix"] = prefix
                save_count += 1
        if save_count == 0:
            raise ValueError("模板中没有找到 SaveImage 节点")

        return workflow, staged_path

    def _remove_staged(self, staged_path: Path | None) -> None:
        """删除本次任务暂存的输入图；失败只 warning，绝不影响返回值结构。

        只在 /history 确认任务结束、贴图回收完毕后调用，避免删掉 ComfyUI 正在读取的文件。
        """
        if staged_path is None:
            return
        try:
            if Path(staged_path).is_file():
                Path(staged_path).unlink()
                print(f"[ComfyUI] 已清理暂存输入图：{staged_path}")
        except Exception as exc:  # noqa: BLE001 —— 清理失败绝不能影响主流程
            print(f"[ComfyUI] Warning: 暂存输入图清理失败（忽略）：{exc}", file=sys.stderr)

    def _prune_staging_dir(self, limit: int = STAGING_MAX_FILES, exclude: Path | None = None) -> None:
        """保险丝：asset_pipeline 暂存目录文件数超过 limit 时，按 mtime 清理最旧的。

        只操作 ComfyUI/input/asset_pipeline 子目录，绝不触碰 input 下其他用户图片；
        exclude 用于排除本轮仍在被 ComfyUI 读取的文件。全部 try-except，失败不影响结果。
        """
        try:
            staging = Path(self.comfy_input_dir) / STAGING_SUBDIR
            if not staging.is_dir():
                return
            files = [p for p in staging.iterdir() if p.is_file()]
            if exclude is not None:
                try:
                    excluded = Path(exclude).resolve()
                    files = [p for p in files if p.resolve() != excluded]
                except Exception:
                    pass
            if len(files) <= limit:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            removed = 0
            for old in files[: len(files) - limit]:
                try:
                    old.unlink()
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[ComfyUI] Warning: 旧暂存图删除失败（忽略）：{old}（{exc}）", file=sys.stderr)
            if removed:
                print(f"[ComfyUI] 暂存目录超过 {limit} 张，已按修改时间清理 {removed} 张旧图。")
        except Exception as exc:  # noqa: BLE001
            print(f"[ComfyUI] Warning: 暂存目录清理失败（忽略）：{exc}", file=sys.stderr)

    def _wait_history(self, prompt_id: str) -> dict:
        """轮询 /history/{prompt_id} 直到生成完成 / 出错 / 超时，返回该 prompt 的历史条目。"""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                data = self._http_json(f"{self.api_url}/history/{prompt_id}", timeout=10.0)
            except urllib.error.HTTPError:
                # 404 = 结果尚未出炉，继续等
                time.sleep(POLL_INTERVAL)
                continue

            entry = data.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    messages = status.get("messages") or []
                    raise RuntimeError(f"ComfyUI 执行失败：{messages}")
                if entry.get("outputs"):
                    return entry
            time.sleep(POLL_INTERVAL)
        raise TimeoutError(f"生成超时（{self.timeout:.0f} 秒）")

    @staticmethod
    def _fallback(reason: str) -> dict:
        """降级兜底：打印 Error 日志并返回 fallback 结构，绝不抛异常。"""
        print(f"[ComfyUI] Error: {reason}（降级：由调用方用原图兜底）", file=sys.stderr)
        return {"status": "fallback", "reason": reason}

    # ------------------------------------------------------------ 核心方法

    def generate_pbr(
        self,
        input_image_path: str | os.PathLike,
        output_dir: str | os.PathLike,
        prefix: str = "material",
    ) -> dict:
        """提交 CHORD 工作流，回收 5 张 PBR 贴图。

        成功返回：
            {"status": "success", "prompt_id": "...", "files": [绝对路径 x N]}
        失败返回：
            {"status": "fallback", "reason": "..."}   —— 绝不抛异常。
        """
        staged_path: Path | None = None
        try:
            if not Path(input_image_path).is_file():
                return self._fallback(f"输入图片不存在：{input_image_path}")

            if not self.is_ready():
                return self._fallback(f"ComfyUI 未启动或不可达（{self.api_url}）")

            # ① 组装并提交任务（输入图在此暂存到 ComfyUI input 子目录）
            workflow, staged_path = self._patch_template(input_image_path, prefix)
            try:
                submit = self._http_json(
                    f"{self.api_url}/prompt",
                    {"prompt": workflow, "client_id": self.client_id},
                    timeout=30.0,
                )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                # 提交失败：ComfyUI 未接手该图，可立即安全清理
                self._remove_staged(staged_path)
                staged_path = None
                return self._fallback(f"提交任务失败（HTTP {exc.code}）：{detail}")
            prompt_id = submit.get("prompt_id")
            if not prompt_id:
                self._remove_staged(staged_path)
                staged_path = None
                return self._fallback(f"/prompt 未返回 prompt_id：{submit}")

            # ② 轮询等待完成（低显存机器最长 180 秒）
            history_entry = self._wait_history(prompt_id)
            outputs = history_entry.get("outputs") or {}

            # ③ 从 ComfyUI output 目录回收并按后缀重命名
            out_dir = Path(output_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            files: list[str] = []
            missing: list[str] = []
            for node_id, suffix in SAVE_NODE_SUFFIX.items():
                node_output = outputs.get(node_id) or {}
                images = node_output.get("images") or []
                if not images:
                    missing.append(f"{node_id}{suffix}")
                    continue
                info = images[-1]  # 单批任务一般一张；多张取最后一张
                src = self.comfy_output_dir / str(info.get("subfolder", "")).replace("\\", "/") / info["filename"]
                src = Path(src)
                if not src.is_file():
                    print(f"[ComfyUI] Warning: 输出文件不存在，已跳过：{src}", file=sys.stderr)
                    missing.append(f"{node_id}{suffix}")
                    continue
                dst = out_dir / f"{prefix}{suffix}{src.suffix or '.png'}"
                shutil.copy2(src, dst)
                files.append(str(dst))

            # ④ /history 已确认任务结束且贴图已回收完毕，此时删除本轮的暂存输入图最安全
            self._remove_staged(staged_path)
            staged_path = None

            if not files:
                return self._fallback(f"任务完成但未回收任何贴图（缺失：{missing}）")
            if missing:
                print(f"[ComfyUI] Warning: 部分贴图缺失 {missing}，本次交付 {len(files)} 张。", file=sys.stderr)

            print(f"[ComfyUI] 生成成功：prompt_id={prompt_id}，{len(files)} 张贴图 -> {out_dir}")
            return {"status": "success", "prompt_id": prompt_id, "files": files}

        except Exception as exc:
            return self._fallback(f"{type(exc).__name__}: {exc}")
        finally:
            # 超时 / 异常时本轮暂存图可能仍被 ComfyUI 读取，不删（只做容量保险丝，并排除它）
            self._prune_staging_dir(exclude=staged_path)


# ---------------------------------------------------------------- CLI 测试入口

def main() -> int:
    parser = argparse.ArgumentParser(description="ComfyUI CHORD PBR 生成测试工具")
    parser.add_argument("--image", required=True, help="输入图片绝对路径")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--prefix", default="material", help="SaveImage 前缀（默认 material）")
    args = parser.parse_args()

    client = ComfyUIClient()
    result = client.generate_pbr(args.image, args.out, prefix=args.prefix)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())