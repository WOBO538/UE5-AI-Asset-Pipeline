# AI 语义分析层设计文档

> 模块归属：UE5 AI 增强型资产智能管线 —— 第二阶段「AI 语义分析层」
> 上游：外部流转层（已跑通，输出 `import_manifest.json` + `trigger.txt`）
> 下游：UE 引擎内消费层（规则引擎 + 重命名/归类执行）
> 模型底座：DeepSeek（`deepseek-chat`，OpenAI 兼容协议）

---

## 1. 模块目标与边界

### 目标

外部流转层完成「物理搬运」后，资产已经落到 `/Game/Imported/...` 下，但名字和分类仍是原始状态。AI 语义分析层接收 `import_manifest.json`，对其中**每个资产**输出一组语义建议，供下游消费层决定是否重命名 / 归类。

每个资产的五种产出：

| 字段 | 含义 | 下游用途 |
| :--- | :--- | :--- |
| `suggested_name` | 符合 UE 前缀规范的推荐名 | 规则引擎核对后可执行重命名 |
| `category` | 语义分类（`Environment` 等） | PCG 按类取用资产 |
| `chinese_description` | 一句话中文描述 | 资产浏览器 / 搜索展示 |
| `tags` | 中文关键词数组 | 语义检索与匹配 |
| `confidence` | 0~1 置信度 | 低于阈值转人工复核 |

### 边界（核心原则）

```text
                       ┌─────────────────建议──────────────────┐
AI 语义层 ──输出候选──► 规则引擎（确定性校验） ──通过──► 执行器（重命名/归类）
                        ▲        │
                        │        └── 否决/纠正（前缀表、禁用词、长度限制）
                        └── confidence 过低 → 人工复核队列
```

- **AI 只负责「建议」，不负责「执行」**：本层绝不调用 `rename_asset` / `delete_asset` / 任何写操作。
- **最终裁决权在规则引擎**：AI 的 `suggested_name` 只是输入，规则引擎仍需用确定性规则校验（前缀是否合法、是否命中禁用词、是否与现有资产冲突），AI 建议可被无条件否决。
- **无状态、幂等**：相同输入（含文件 `mtime`）必然产生相同输出，配合缓存层。
- **不 `import unreal`**：本层是纯 Python，可在引擎外沙盒运行与测试（UE 内通过 `unreal.PythonScriptPlugin` 以子进程/线程方式调用即可）。
- **失败降级不阻断**：单个资产失败仅记入 `failures`，绝不抛异常中断整批。

---

## 2. 输入契约

上游 `import_manifest.json` 的 `assets` 是**纯路径字符串数组**（不带 `.uasset` 后缀、不含 `asset_class`）。

### 完整输入 JSON 示例

```json
{
  "assets": [
    "/Game/Imported/SoStylized/Environment/Rock",
    "/Game/Imported/SoStylized/Environment/Rock/T_Rock_D"
  ],
  "metadata": {
    "source_zip": "SoStylized.zip",
    "processed_at": "2026-10-08T12:00:00"
  }
}
```

### 字段约束

| 字段 | 必填 | 说明 |
| :--- | :--- | :--- |
| `assets` | 是 | 字符串数组，每个元素为 `/Game/...` 形式的资产对象路径 |
| `metadata.source_zip` | 是 | 来源包名，用于日志与结果关联 |
| `metadata.processed_at` | 是 | 流转层处理时间（ISO8601），作为缓存失效的参考时间线 |

> 说明：`assets` 只有路径，没有 `asset_class`。类型需要 AI 从路径 + 文件名**前缀**推断（`T_`=贴图、`SM_`=静态网格体、`MI_`=材质实例等），详见第 4 章。

---

## 3. 输出契约

输出包含 `results`（成功项）与 `failures`（失败项）两个并列数组，二者互斥：一个资产只会出现在其中之一。

### 成功结果示例

```json
{
  "analyzed_at": "2026-10-08T12:00:05",
  "model": "deepseek-chat",
  "results": [
    {
      "asset_path": "/Game/Imported/SoStylized/Environment/Rock",
      "suggested_name": "SM_Rock_01",
      "category": "Environment",
      "chinese_description": "风格化岩石，用于自然环境铺设",
      "tags": ["自然", "岩石", "低多边形"],
      "confidence": 0.92
    }
  ],
  "failures": []
}
```

### 失败结果示例

```json
{
  "analyzed_at": "2026-10-08T12:00:05",
  "model": "deepseek-chat",
  "results": [],
  "failures": [
    {
      "asset_path": "/Game/Imported/SoStylized/Environment/Rock/T_Rock_D",
      "reason": "API timeout"
    }
  ]
}
```

### 字段约束

| 字段 | 说明 |
| :--- | :--- |
| `analyzed_at` | AI 层完成分析的时间戳 |
| `model` | 实际调用的模型名，便于回溯与对照 |
| `results[].asset_path` | 原样回传，作为全流程主键 |
| `results[].confidence` | 0~1，规则引擎可设阈值（如 <0.6 转人工） |
| `failures[].reason` | 失败原因（`API timeout` / `INVALID_JSON` / `KEY_MISSING` / `MODEL_REFUSAL`） |

---

## 4. Prompt 工程策略

原则：**System Prompt 固化「角色 + 输出格式 + 前缀命名规范 + 分类体系 + 置信度打分」**，User Prompt 只放本批资产，两者分离，方便 A/B 调优且 System 指令的固定部分可被长连接复用。

### 4.1 System Prompt（完整）

```text
你是 Unreal Engine 5 资产命名与语义分析专家。

你接收一批 UE5 资产的对象路径（形如 /Game/Imported/...），推断每个资产
的语义，输出严格符合要求的结构化 JSON。你只提供"建议"，绝不执行任何操作。

## 输出要求
- 只输出一个 JSON 对象，不要任何解释、前后缀文字或 Markdown 代码块。
- 顶层对象含两个数组键："results" 与 "failures"。
- "results" 中每个元素与输入资产一一对应，且只含以下键：
  "asset_path"（字符串，原样回传）、
  "suggested_name"（字符串）、
  "category"（字符串，见分类体系）、
  "chinese_description"（字符串，≤20 字中文）、
  "tags"（字符串数组，3~6 个中文关键词）、
  "confidence"（0~1 浮点数）。

## 类型推断（输入只有路径，无类型字段，按多级优先级判定）
按以下优先级从高到低推断资产类型，命中即停止：
1. 文件名前缀：SM_→StaticMesh、SK_→SkeletalMesh、T_→Texture2D、
   M_→Material、MI_→MaterialInstance、BP_→Blueprint、
   NS_→NiagaraSystem、A_→AnimationSequence。
2. 所在目录名：Meshes/→网格体、Textures/→贴图、Materials/→材质、
   Environment/→多为网格体或材质、Props/→道具网格体 等。
3. 资产名语义：依据名字含义（如 Rock、Crate、Knight）推断类型。
4. 兜底：以上均无法判断时，类型记为 Unknown，confidence 相应下调。

## 命名规范（suggested_name 必须遵守）
采用 UE 前缀 + 大驼峰语义名：
StaticMesh -> SM_、SkeletalMesh -> SK_、Texture2D -> T_、
Material -> M_、MaterialInstance -> MI_、Blueprint -> BP_、
NiagaraSystem -> NS_、AnimationSequence -> A_、其他 -> Asset_。
名称只保留语义，去除乱码；同批同名资产自动追加 _01、_02 流水号。

## 分类体系（category 只能从下列取值）
Environment、Prop、Character、Material、Texture、VFX、Animation、
Audio、UI、Blueprint、Other

## confidence 打分
- 0.9~1.0：路径 + 前缀信息明确，无歧义
- 0.7~0.9：信息较明确，但存在同义可能
- 0.5~0.7：信息不足，仅靠目录名猜测
- 0.0~0.5：几乎无法判断，建议人工复核
```

### 4.2 User Prompt 模板

```text
请分析以下 UE5 资产，返回结构化 JSON。

资产路径列表（JSON 字符串数组）：
{assets_json}

注意：无法判断时 confidence 降到 0.5 以下即可，不要编造。
```

填充示例（`{assets_json}` 替换为实际输入）：

```json
[
  "/Game/Imported/SoStylized/Environment/Rock",
  "/Game/Imported/SoStylized/Environment/Rock/T_Rock_D"
]
```

### 4.3 生成参数建议

| 参数 | 建议值 | 理由 |
| :--- | :--- | :--- |
| `temperature` | 0.1 | 语义建议要确定性 |
| `response_format` | `{"type": "json_object"}` | DeepSeek JSON 模式，强制合法 JSON |
| `max_tokens` | `120 × 批大小` | 防止批量输出被截断 |

---

## 5. 批量调用策略

一次请求分析**多个资产**，摊薄 System Prompt 与固定指令的重复 token 开销。

```python
BATCH_SIZE = 20             # 单次请求最多资产数
MAX_TOKENS_PER_ASSET = 120  # 每资产预留输出 token

def analyze_manifest(assets: list[str], ue_project_content: str) -> dict:
    results, failures = [], []
    for i in range(0, len(assets), BATCH_SIZE):
        chunk = assets[i:i + BATCH_SIZE]
        parsed = _call_and_parse(chunk)              # 见第 6 章
        results.extend(parsed.get("results", []))
        failures.extend(parsed.get("failures", []))
    return {
        "analyzed_at": _now_iso(),
        "model": MODEL,
        "results": results,
        "failures": failures,
    }


def _call_and_parse(chunk: list[str]) -> dict:
    resp = client.chat.completions.create(
        model=MODEL,
        temperature=0.1,
        response_format={"type": "json_object"},
        max_tokens=MAX_TOKENS_PER_ASSET * len(chunk),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                assets_json=json.dumps(chunk, ensure_ascii=False))},
        ],
    )
    return _parse_json(resp.choices[0].message.content)
```

### token 消耗估算

| 项 | 逐条调用 | 批量调用（20/批） |
| :--- | :--- | :--- |
| System Prompt（约 700 token） | N × 700 | ⌈N/20⌉ × 700 |
| User 固定指令（约 60 token） | N × 60 | ⌈N/20⌉ × 60 |
| 单条资产路径（约 15 token） | N × 15 | N × 15 |
| 输出（约 80 token/条） | N × 80 | N × 80 |

以 N=100 估算：逐条约 `100×775 + 100×80 = 85.5k` token；批量约 `5×775 + 100×80 ≈ 11.9k` token，**固定开销下降约 85%**。

### 批大小权衡

- 太小（<10）：固定指令占比高，经济性差。
- 太大（>50）：单次响应体变长，更容易截断或漏项，重试成本上升。
- 默认 20，`max_tokens` 按 `120×N` 动态给足余量。

---

## 6. 失败降级方案

原则：**单个资产失败只写入 `failures`，整批永不中断**。三种情况分别处理。

> 边界说明：进入 `failures` 的资产**不参与 AI 层的重命名建议**（AI 未给出可用的 `suggested_name`），但它们**仍会由 UE 消费层走规则引擎兜底**——命名校验（前缀合法、禁用词、长度）与贴图设置校验照常执行，等价于「关闭 AI、仅走确定性规则」的路径。AI 失败只意味着少了「语义建议」这一路增强，不意味着该资产被跳过。

> TODO：v0.3 实现「人工复核队列」的落地形态；本文档只约定 `failures` 与 `confidence < 阈值` 的资产进入待复核，具体存储 / 界面 / 处理流程暂不展开，留待 v0.3 设计。

### 6.1 API 超时

```python
import time
from openai import APITimeoutError, APIConnectionError

def _call_with_retry(messages, max_retries=3, timeout=30.0):
    for attempt in range(max_retries):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages,
                temperature=0.1, timeout=timeout,
                response_format={"type": "json_object"})
            return resp.choices[0].message.content
        except (APITimeoutError, APIConnectionError):
            if attempt == max_retries - 1:
                raise  # 最终失败 -> 上层整批失败处理
            time.sleep(2 ** attempt)  # 指数退避 1s / 2s / 4s

# 上层：整批超时降级为 failures
try:
    raw = _call_with_retry(messages)
except (APITimeoutError, APIConnectionError):
    return {"results": [],
            "failures": [{"asset_path": p, "reason": "API timeout"}
                         for p in chunk]}
```

### 6.2 非法 JSON

```python
import json
import re

def _parse_json(raw: str) -> dict:
    # 第一次：直接解析
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # 第二次：剥掉可能的 Markdown 代码块再解析
    cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # 仍失败 -> 该批整组降级为 failures，标记 INVALID_JSON
        return {"results": [],
                "failures": [{"asset_path": p, "reason": "INVALID_JSON"}
                             for p in _last_chunk]}
```

### 6.3 Key 缺失（启动期一次性检查，而非逐条请求）

```python
import os

def ensure_api_key() -> str | None:
    return os.environ.get("DEEPSEEK_API_KEY")

# analyze_manifest 入口：Key 缺失不发起任何请求，整批直接失败
if api_key is None:
    return {
        "analyzed_at": _now_iso(),
        "model": None,
        "results": [],
        "failures": [{"asset_path": p, "reason": "KEY_MISSING"}
                     for p in assets],
    }
```

---

## 7. 缓存与限流设计

### 7.1 缓存 key = `asset_path + mtime + file_size`

同一资产只要文件内容没变（`mtime` + `file_size` 均不变），就跳过 API 直接复用上次结果；资产被重新导入（任一字段更新）则自动失效重算。

```python
import hashlib
import os

def resolve_physical_asset(asset_path: str, ue_project_content: str) -> str | None:
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
    raw = json.dumps({"asset_path": asset_path, "mtime": mtime, "file_size": file_size},
                     sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(key: str) -> dict | None:
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def set_cached(key: str, result: dict) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, f"{key}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
```

调用侧：命中缓存直接拼结果，未命中才批量调 API。

```python
uncached = [p for p in assets if get_cached(make_cache_key(p, ue_project_content)) is None]
# 只对 uncached 走批量调用，命中项从缓存回填
```

> 说明：用 `mtime`（浮点，保留毫秒）+ `file_size` 而非文件内容 hash，是因为分析只关心「文件是否被替换过」；二者读取都是 O(1) 的 `os.stat`，对大批量资产远快于读取整个 `uasset` 二进制做 sha256。追加 `file_size` 是为了兜底某些工具改写文件但未更新 mtime 的边界情况。

### 7.2 限流与成本闸门

```python
import threading
from collections import deque

class RateLimiter:
    """滑动窗口：每分钟最多 rpm 次请求。"""
    def __init__(self, rpm: int = 60):
        self.rpm = rpm
        self._window = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            now = time.time()
            with self._lock:
                while self._window and self._window[0] <= now - 60:
                    self._window.popleft()
                if len(self._window) < self.rpm:
                    self._window.append(now)
                    return
                wait = 60 - (now - self._window[0])
            time.sleep(min(wait, 1.0))

# 成本闸门：单日输入 token 超预算则整批拒绝进入失败队列
def within_budget(est_input_tokens: int) -> bool:
    return est_input_tokens <= DAILY_MAX_INPUT_TOKENS
```

---

## 8. 环境变量与密钥管理

密钥与 AI 运行参数走环境变量 + `.env`；**路径一律从 `config.ini` 读取，不写入 `.env`**。绝不硬编码进代码、不进入结果 JSON。

### 8.1 `.env.example`（提交到仓库作为规范）

```ini
# DeepSeek 密钥（唯一敏感项，必填）
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxx

# 模型与端点（OpenAI 兼容）
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

# 批量与限流
AI_BATCH_SIZE=20
AI_RPM=60
AI_DAILY_MAX_INPUT_TOKENS=1000000

# 请求超时（秒）与重试
AI_TIMEOUT=30.0
AI_MAX_RETRIES=3
```

> 说明：`.env` 只放 AI 相关配置（密钥、模型名、端点、批量/限流/超时等）；**任何路径（如 UE 项目 Content 目录）都不写在 `.env`**，统一从 `config.ini` 读取。

### 8.2 `.gitignore` 配置

```gitignore
# 密钥与本地环境
.env
*.env
!.env.example

# 分析结果与缓存（运行时产物）
Saved/AIAnalysis/
cache/
*.log
```

### 8.3 加载与客户端初始化

```python
import os
import configparser
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # 读取同目录 .env（仅 AI 配置），不覆盖已有环境变量

API_KEY = os.environ["DEEPSEEK_API_KEY"]
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "20"))
RPM = int(os.environ.get("AI_RPM", "60"))
TIMEOUT = float(os.environ.get("AI_TIMEOUT", "30.0"))

# 路径类配置从 config.ini 读取，不写在 .env
_cfg = configparser.ConfigParser()
_cfg.read("config.ini")
ue_project_content = _cfg.get("paths", "ue_project_content")

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
```

### 8.4 安全规范

- `DEEPSEEK_API_KEY` 不打印、不入日志、不入分析结果。
- 打包成 EXE 给美术时**不打真实 Key**，改由首启流程引导用户填写 `.env`。
- CI 用加密 secrets 注入环境变量，不落地为文件。

---

## 9. 与 UE 消费层的对接协议

### 9.1 结果放哪

AI 层输出写到**与 `import_manifest.json` 同目录**，命名为 `ai_analysis.json`，方便消费层一次取走 manifest + 分析结果，路径天然对齐。

```text
{Processing}/{zip_name}/
├── import_manifest.json   # 上游流转层生成
├── trigger.txt            # 上游流转层生成（内容 "ready"）
└── ai_analysis.json       # AI 层生成（本层输出）
```

### 9.2 谁调用、何时调用（AI 层由 UE 消费层调用）

```text
外部流转层                    UE 消费层（规则引擎+执行）             AI 语义层
   │                              │                                │
   ├─写 import_manifest.json ────►│                                │
   ├─写 trigger.txt ─────────────►│                                │
   │                              ├─检测到 trigger.txt              │
   │                              ├─读 import_manifest.json         │
   │                              ├─取出 assets 列表 ──────────────►│
   │                              │                                ├─批量调 API（analyze_manifest）
   │                              │  ◄─────────── 返回 results / failures ──┤
   │                              ├─（可选）落盘 ai_analysis.json
   │                              ├─规则引擎校验 suggested_name
   │                              ├─执行重命名/归类
   │                              └─删除 trigger.txt（防重复）
```

- **谁调用**：AI 层由**引擎内 UE 消费层**调用，而不是外部流转层。消费层脚本（`pipeline_consumer.py` 一类）在读取 `import_manifest.json` 之后，把其中的 `assets` 列表作为参数传给 AI 层：`analyze_manifest(assets, ue_project_content)`，据此拿到 `results` / `failures`。
- **AI 层不直接读外部流转层的输出目录**：AI 层不接触 `Processing/` 目录、也不直接读 `import_manifest.json` 文件，一切输入都由 UE 消费层读文件后转发。
- **调用时机**：UE 消费层检测到 `trigger.txt` 后，先读 `import_manifest.json`，再调 AI 层；AI 层故障时按第 6 章降级为「跳过语义分析、仅走规则引擎」。
- **对齐键**：`asset_path`。消费层据此把 `results` 逐条映射回 `import_manifest.json` 的资产。

### 9.3 消费层对接伪代码

```python
import unreal, json, os

PROCESSING_DIR = os.path.join(unreal.Paths.project_dir(), "Processing")

def consume(folder):
    manifest = _read_json(folder, "import_manifest.json")
    analysis = _read_json(folder, "ai_analysis.json")   # 可能为 None

    results = {r["asset_path"]: r for r in analysis.get("results", [])}
    for asset_path in manifest["assets"]:
        rec = results.get(asset_path)
        if not rec:
            continue  # AI 未给出建议，跳过、保留原名

        # ── 规则引擎裁决（最终裁决权在这里）──
        if rec["confidence"] < 0.6:
            _enqueue_manual_review(asset_path, rec)  # TODO: v0.3 实现人工复核队列（当前仅记录）
            continue
        final_name = _validate_against_prefix_table(rec["suggested_name"])
        if final_name != asset_path.rsplit("/", 1)[-1]:
            unreal.EditorAssetLibrary.rename_asset(asset_path, _parent(asset_path) + "/" + final_name)
        _apply_tags(asset_path, rec["tags"])  # 写资产元数据（可选）
```

### 9.4 规则引擎校验（体现「AI 建议、规则裁决」）

```python
PREFIX_TABLE = {"SM_": "StaticMesh", "T_": "Texture2D", "MI_": "MaterialInstance",
                "M_": "Material", "SK_": "SkeletalMesh", "BP_": "Blueprint"}

def _validate_against_prefix_table(suggested: str) -> str:
    # 1) 前缀必须在合法枚举内，否则退回原资产名
    prefix = suggested.split("_", 1)[0] + "_"
    if prefix not in PREFIX_TABLE:
        return suggested  # 交由上层二次处理或保留原名
    # 2) 长度 / 非法字符过滤
    if len(suggested) > 64 or not suggested.replace("_", "").isalnum():
        return suggested
    return suggested  # 通过校验
```

---

## 附：全链路数据流一览

```text
[外部流转层]                      [AI 语义层]                      [UE 消费层]
import_manifest.json    ───────►   analyze_manifest()   ───────►   规则引擎校验
  { assets: [path...],              { results: [...],               execute():
  metadata:{...} }                  failures:[...] }                 rename / 归类 / 打标签
```

- `results` 中的 `suggested_name` 是**候选**，规则引擎校验后才执行。
- `failures` 与 `confidence < 阈值` 都进人工复核队列，不参与自动执行。