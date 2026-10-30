# -*- coding: utf-8 -*-
"""Asset Intelligence Pipeline - 插件入口（自动启动 Python 监听服务）。

UE 编辑器加载本插件时，会在插件 Content/Python/ 目录下自动执行本文件。
职责：
    1. 把本插件的 Python 目录注入 sys.path，让同目录的 pipeline_consumer 等模块可被 import
    2. 用 importlib.reload 载入 pipeline_consumer，保证开发迭代后无需重启编辑器即可生效
    3. 注册 Slate Post Tick 回调做「非阻塞」间隔轮询（默认 5 秒一次），自动调用
       pipeline_consumer.check_and_process_pipeline()，免去人工在 Python 控制台敲 import

设计要点：
    * 非阻塞：绝对不在 tick 里 sleep / while True，只用 delta_seconds 累计时间差判断间隔，
      编辑器帧率与交互不受影响。
    * 幂等：脚本被重复执行或 reload 时，先注销旧回调再注册新回调，避免叠加多个 tick。
    * 兜底：所有加载与轮询逻辑都包在 try/except，异常写 unreal.log_error（红色 + traceback），
      连续失败达到阈值自动注销回调，既不刷屏也不影响引擎。
"""

import os
import sys
import time
import traceback
import importlib

import unreal

_LOG_PREFIX = "[AssetIntelligencePipeline]"
_CONSUMER_MODULE_NAME = "pipeline_consumer"
_REQUIRED_FUNC_NAME = "check_and_process_pipeline"
_POLL_INTERVAL_SECONDS = 5.0      # 非阻塞轮询间隔（秒）
_MAX_CONSECUTIVE_FAILURES = 10    # 连续失败阈值，达到后自动注销回调，避免无限刷日志

# 本文件所在目录 = Content/Python/。用 __file__ 动态获取，禁止硬编码绝对路径，
# 保证插件被移动到别的工程后 import 依然能找到同目录的 pipeline_consumer。
_PLUGIN_PYTHON_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_PYTHON_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_PYTHON_DIR)

# ---------------------------------------------------------------------------
# 模块级状态
# ---------------------------------------------------------------------------
# 脚本被 importlib.reload / 重复 exec 时，模块 globals 会被复用；这里优先沿用已有状态，
# 从而能在重新注册前拿到「上一次」的 tick 回调句柄并注销，实现幂等注册。
_STATE = globals().get("_STATE") or {
    "handle": None,     # tick 回调句柄；None 表示未注册或已停止
    "elapsed": 0.0,     # 累计时间差（秒），达到 _POLL_INTERVAL_SECONDS 才触发一次业务轮询
    "failures": 0,      # 连续失败次数
    "poll_count": 0,    # 成功轮询次数（用于打印首次耗时，验证非阻塞）
}


def _log(message):
    """统一的启动/运行日志，便于在 Output Log 中用 [AssetIntelligencePipeline] 过滤。"""
    unreal.log(f"{_LOG_PREFIX} {message}")


def _warn(message):
    """黄色警告日志。"""
    unreal.log_warning(f"{_LOG_PREFIX} {message}")


def _error(message):
    """红色错误日志（用于失败与异常 traceback）。"""
    unreal.log_error(f"{_LOG_PREFIX} {message}")


# ---------------------------------------------------------------------------
# 业务轮询
# ---------------------------------------------------------------------------
_consumer_module = globals().get("_consumer_module")


def _poll_once():
    """执行一次业务轮询。

    内部自兜底：任何异常都在这里吞掉并计入连续失败次数，绝不外抛，
    保证单次业务异常不会让 tick 回调彻底失效。
    """
    state = _STATE
    start = time.perf_counter()

    try:
        # 前提检查：调用前确认模块里确实存在可调用的入口函数。
        # 当前 pipeline_consumer 暴露的函数名与约定一致（check_and_process_pipeline），
        # 若后续改名，这里会在 Output Log 明确报错并停止轮询，而不是静默失败。
        func = getattr(_consumer_module, _REQUIRED_FUNC_NAME, None) if _consumer_module else None
        if not callable(func):
            _error(
                f"{_CONSUMER_MODULE_NAME} 中不存在可调用的 {_REQUIRED_FUNC_NAME}()，停止轮询。"
                f"请确认模块实际对外暴露的入口函数名。")
            _stop_tick("缺少入口函数")
            return
        func()
    except Exception:
        _handle_failure(traceback.format_exc())
        return

    # 走到这里说明本轮成功：重置连续失败计数，并记录耗时用于验证「非阻塞」
    state["failures"] = 0
    state["poll_count"] += 1
    cost_ms = (time.perf_counter() - start) * 1000.0
    if state["poll_count"] == 1:
        # 只在首次打印耗时，避免每 5 秒刷一条日志
        _log(f"首次轮询完成，耗时 {cost_ms:.1f} ms（远小于 {_POLL_INTERVAL_SECONDS:.0f} s 间隔，非阻塞）")
    elif cost_ms > _POLL_INTERVAL_SECONDS * 1000.0 * 0.5:
        _warn(f"本轮轮询耗时 {cost_ms:.1f} ms，已超过间隔的一半，请关注耗时较长的业务操作。")


def _handle_failure(traceback_text):
    """记录一次轮询失败；连续失败达到阈值后自动注销回调。自身绝不抛出异常。"""
    try:
        state = _STATE
        state["failures"] = state.get("failures", 0) + 1
        count = state["failures"]
        _error(
            f"轮询异常（连续第 {count}/{_MAX_CONSECUTIVE_FAILURES} 次）：\n{traceback_text}")
        if count >= _MAX_CONSECUTIVE_FAILURES:
            _stop_tick(f"连续 {count} 次轮询异常，自动注销 tick 回调以避免日志刷屏")
    except Exception:
        # 兜底中的兜底：无论如何都不让异常逃出 tick 回调
        pass


def _stop_tick(reason):
    """注销 tick 回调（幂等、可重复调用）。"""
    state = _STATE
    handle = state.get("handle")
    # 先置空句柄：即使宿主不支持注销 API，下一次 tick 也会立即 early-return，不再执行业务
    state["handle"] = None
    if handle is None:
        return

    unregister = getattr(unreal, "unregister_slate_post_tick_callback", None)
    if not callable(unregister):
        _warn(f"宿主不支持 unreal.unregister_slate_post_tick_callback，仅置空句柄（{reason}）")
        return
    try:
        unregister(handle)
        _warn(f"已注销 tick 回调（{reason}）")
    except Exception:
        _error(f"注销 tick 回调失败（{reason}）：\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Tick 回调（非阻塞核心）
# ---------------------------------------------------------------------------
def _on_tick(delta_seconds):
    """Slate Post Tick 回调：签名固定接收 delta_seconds（距上一帧的秒数）。

    只做累加与阈值判断，绝不 sleep 或死循环；每累计满 5 秒才执行一次业务轮询，
    因此编辑器每一帧的开销可以忽略不计。
    """
    state = _STATE

    # 已停止 / 未注册：直接返回（防止注销后（若注销 API 不生效）仍被调用时做无谓工作）
    if state.get("handle") is None:
        return

    try:
        state["elapsed"] = state.get("elapsed", 0.0) + float(delta_seconds or 0.0)
        if state["elapsed"] < _POLL_INTERVAL_SECONDS:
            return
        state["elapsed"] = 0.0
        _poll_once()
    except Exception:
        # 最后一道防线：保证 tick 回调永远不把异常抛回引擎
        _handle_failure(traceback.format_exc())


def _register_tick():
    """注册 tick 回调；重复执行脚本时先注销旧回调，保证只有一份在跑。"""
    state = _STATE

    # 幂等：只有拿到旧句柄才可能重复注册，先清理再注册
    if state.get("handle") is not None:
        _stop_tick("重新注册前清理旧回调，防止重复注册")

    register = getattr(unreal, "register_slate_post_tick_callback", None)
    if not callable(register):
        _error("当前引擎不支持 unreal.register_slate_post_tick_callback，无法启动自动轮询。")
        return False

    try:
        state["handle"] = register(_on_tick)
    except Exception:
        _error(f"注册 tick 回调失败，自动轮询未启动：\n{traceback.format_exc()}")
        return False

    state["elapsed"] = 0.0
    state["failures"] = 0
    _log(f"监听服务已启动：Slate Tick 非阻塞轮询，每 {_POLL_INTERVAL_SECONDS:.0f} 秒检查一次。")
    return True


# ---------------------------------------------------------------------------
# 模块加载
# ---------------------------------------------------------------------------
def _load_consumer():
    """导入 / 重新加载 pipeline_consumer，并校验入口函数是否存在。失败返回 None。"""
    module_name = _CONSUMER_MODULE_NAME
    try:
        existing = sys.modules.get(module_name)
        if existing is not None:
            # 开发迭代时模块可能已在内存中：reload 让最新代码生效，无需重启编辑器
            module = importlib.reload(existing)
            _log(f"{module_name} 已 importlib.reload（保证最新代码生效）。")
        else:
            module = importlib.import_module(module_name)
            _log(f"{module_name} 首次导入完成。")
    except Exception:
        _error(f"导入 {module_name} 失败，自动轮询未启动：\n{traceback.format_exc()}")
        return None

    # 前提检查：不存在入口函数时明确报错并停止轮询，不静默失败
    func = getattr(module, _REQUIRED_FUNC_NAME, None)
    if not callable(func):
        _error(
            f"{module_name} 中不存在可调用的 {_REQUIRED_FUNC_NAME}()，已停止轮询。"
            f"请确认模块对外暴露的函数名与 init_unreal.py 中的 _REQUIRED_FUNC_NAME 一致。")
        return None

    return module


def _bootstrap():
    """插件加载入口：加载模块 -> 注册 tick。任何一步失败只报错，不影响引擎。"""
    global _consumer_module

    _log("init_unreal.py 已加载。")
    _log(f"Python 目录：{_PLUGIN_PYTHON_DIR}")

    module = _load_consumer()
    if module is None:
        _warn("监听服务未启动（pipeline_consumer 不可用），请检查上方错误日志。")
        return False

    _consumer_module = module
    return _register_tick()


try:
    _bootstrap()
except Exception:
    # 启动阶段兜底：即使注册逻辑本身有 bug，也只是红字报错，不会把引擎带崩
    _error(f"启动 Python 监听服务失败（引擎继续运行，不受影响）：\n{traceback.format_exc()}")
