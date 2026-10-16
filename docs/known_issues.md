# 已知问题（Known Issues）

## v0.1

### 1. ~~manifest / trigger 污染 UE 项目 Content 目录~~ 【v0.2 已修复】

**修复方案**：写入路径改为 `<UE项目>/Saved/AssetPipeline/Processing/{zip_name}/`，与资产目录彻底隔离。UE 不再弹"源内容变更"。

---

### 2. ~~多 Zip 并发覆盖~~ 【v0.2 已修复】

**修复方案**：manifest/trigger 改为写入 `<UE项目>/Saved/AssetPipeline/Processing/{zip_name}/`，UE 消费层遍历 `Processing/*/` 逐目录处理，天然支持并发。

---

### 3. 源文件与 UE 项目插件副本需手动同步

**现象**：插件源码在仓库 `tools/UE5-AI-Asset-Pipeline/plugin/` 下，但 UE 实际加载的是 `<UE项目>/Plugins/AssetIntelligencePipeline/` 下的副本。改动后必须手动 `Copy-Item` 同步。

**影响**：容易忘记同步，导致测试跑的是旧代码。

**v0.2 修复方案**：使用目录软链接（Windows mklink /J）指向仓库源码，或写自动化同步脚本。

---

### 4. 自动重命名功能对已存在且被引用的资产不安全

**现象**：当前使用 `duplicate_asset + delete_asset` 实现重命名。该方案对新导入、未被引用的资产是安全的；但如果资产已经被关卡或蓝图引用，删除旧资产会导致引用断裂。

**影响**：管线目前只处理"刚导入的新资产"，因此 v0.1 无风险。如果未来支持"对项目中已有资产做批量重命名"，需要改用更安全的方案（如 `AssetTools.rename_assets` 配合 redirector fixup）。

**v0.2 关注项**：暂不修复，仅记录为边界条件。

### 5. 多 zip 报告会互相覆盖（v0.2 新发现）

**现象**：`asset_validator.generate_reports()` 输出到固定的 `<项目>/Saved/AssetPipeline/Reports/validation_report.json`，多 zip 串行处理时后一个覆盖前一个。

**影响**：v0.2 单 zip 场景无影响，多 zip 时只保留最后一个报告。

**v0.3 修复方案**：报告输出到 `<Processing>/{zip_name}/reports.json`，或文件名带 zip_name。

---

### 6. 多 zip 会连续弹窗（v0.2 新发现）

**现象**：`_process_sub_dir` 每个子目录处理完弹一次窗，一次处理 5 个 zip 需点 5 次确定。

**影响**：v0.2 单次 1-2 个 zip 无感，批量场景体验差。

**v0.3 修复方案**：改为 `check_and_process_pipeline` 末尾汇总一次弹窗。
---

### 7. AI 层仓库路径硬编码在 pipeline_consumer.py（v0.2 新增）

**现象**：`pipeline_consumer.py` 里 `_AI_REPO_ROOT = r"D:\Projects\my-portfolio\tools\UE5-AI-Asset-Pipeline"` 是硬编码的绝对路径。

**影响**：如果美术/同事从 GitHub clone 到别的路径，AI 层会因找不到脚本而静默降级（仅走规则校验）。

**v0.3 修复方案**：把该路径放入插件配置（`<插件>/Config/ai_layer.json` 或 `.uplugin` 的 AdditionalSettings），或通过环境变量注入。
---

### 8. AI 层错误分类不够精确（v0.2 新增）

**现象**：API 鉴权失败（AuthenticationError）被统一归入 `"API timeout"`，无法区分超时 / 鉴权 / 限流 / 其他错误。

**影响**：日志不够直观，排查时容易误判方向。

**v0.3 修复方案**：`_call_and_parse` 按异常类型分类 reason：
- `APITimeoutError` → "API timeout"
- `AuthenticationError` → "AUTH_ERROR"
- `RateLimitError` → "RATE_LIMIT"
- 其他 → "UNKNOWN"
---

### 9. PCG Python API 暴露限制（v0.3 新增）

**现象**：UE 5.7 的 PCG Python 绑定不支持动态修改 Graph 内容。

**探针结论（三条路径全不通）**：
- 参数覆盖：Graph 的 `user_parameters` 是空 InstancedPropertyBag，无 setter
- Collection 注入：`PCGSoftISMComponentDescriptor` 的 Mesh 字段未暴露给 Python（只有 weight）
- 节点修改：`StaticMeshSpawnerSettings` 没有 `set_mesh_entries` 方法

**影响**：Python 无法动态把 AI 分析出的具体资产塞进 PCG Graph。

**v0.3 当前方案（方案 C-）**：
- 预搭 N 个 Graph 模板（PCG_Environment / PCG_Character / PCG_Prop / PCG_VFX）
- 每个模板的 Mesh Entries 手工填好该类资产的占位
- Python 按 `ai_analysis.json` 的 category 选择对应 Graph 挂载
- 通过 TextRenderActor 在场景中标注 AI 分析结果

**未来改进方向**：
- 若 UE 后续版本暴露 PCG Graph 的写入 API，可升级为"动态注入 Mesh"
- 或写 C++ 插件暴露这套 API 给 Python

---

### 10. PCGVolume 采样导致的编辑器卡顿（v0.3 新增）

**现象**：PCGVolume 挂载含 Surface Sampler 的 Graph 后，编辑器会尝试生成大量采样点，可能拖死 UI。

**根因**：`PCGComponent.generation_trigger` 默认值会在编辑器 tick 时自动触发生成，采样量按 Volume 体积计算，大 Volume 直接拖垮主线程。

**应对**：
- 通过 Python 删除 PCGVolume 时，先 `set_editor_property("activated", False)` 再 `destroy_actor`
- Graph 编辑时开启「暂停重新生成」

**v0.3 方案**：批量创建时给 `PCGComponent` 设置 `generation_trigger = "Manual"`，只在显式调用时生成，避免编辑器实时采样。