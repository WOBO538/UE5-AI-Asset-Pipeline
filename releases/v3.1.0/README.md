# Asset Pipeline v3.1.0

美术资产自动流转工具（外部流转层）。本版新增**首次启动自动引导配置 DeepSeek API Key**，下载双击即可用，不必再手动改源码或找 `.env`。

## 本版更新（v3.1.0）

- **新增**：首次启动自动引导配置 DeepSeek API Key，无需手动改源码。
- **修复**：法线贴图默认槽编译报错（针对 UE 5.7 枚举大写兼容）。
- **修复**：中文前缀在 ComfyUI 中被替换为下划线的兼容问题。
- **优化**：统一 `.env` 路径解析，支持 exe 同级目录配置。

### API Key 引导怎么工作

首次运行会检查 `.env` 里的 `AI_API_KEY`，未配置则弹窗提示粘贴：

- **直接回车 / 留空** → 不写入，降级继续运行（AI 层走规则/Mock 兜底，管线照常跑）
- **无 GUI 环境 / 弹窗失败 / 非交互启动** → 自动跳过，绝不阻塞、绝不崩溃
- Key 只写入 **exe 同级目录的 `.env`**；日志与回显只显示前 6 位（如 `sk-b79...`），不会出现完整 Key

`.env` 路径解析优先级：

1. 环境变量 `AI_ENV_PATH` / `ASSET_PIPELINE_ENV`（显式覆盖）
2. exe 运行：**exe 同级目录 `.env`**
3. 源码运行：`src/ai_layer/.env`（开发环境行为不变）

## 使用方法

1. 双击 `AssetPipeline.exe`
2. 首次启动弹出配置向导：
   - 选择你的 UE 项目文件（`.uproject`）
   - 选择管线工作根目录（可直接取消，默认使用 UE 项目同盘的 `Asset_Pipeline`）
   - 按提示粘贴 DeepSeek API Key（可回车跳过）
3. 配置完成后，程序进入监听模式
4. 把资产压缩包（zip，**内部必须包含 `Content` 文件夹**）丢进 `Inbox` 目录
5. 程序会自动解压、导入资产、生成清单、归档

## 目录结构

程序会在工作根目录下自动创建：

- `Inbox/` —— 待处理的 Zip 投放目录
- `Processing/` —— 解压中转目录
- `Archive/` —— 按日期归档的原始 Zip

资产会被复制到 UE 项目的 `Content/Imported/`，同时生成 `import_manifest.json` 和 `trigger.txt` 供 UE5 插件消费。

## 常见问题

- **程序闪退 / 无反应**：检查 Inbox 是否可写，或杀毒软件是否拦截
- **Zip 处理失败**：确认压缩包完整（未损坏），且包含 `Content` 文件夹
- **归档失败**：通常是杀毒软件占用文件，稍后重试；不影响已处理的资产
- **没有弹 API Key 向导**：说明已配置过，或当前是无 GUI 的启动方式（会按未配置继续跑）
- **想重新配置 Key**：删掉 exe 同级的 `.env` 后重启即可

## 配置说明

首次启动后会在 exe 同目录生成 `config.ini`。如需手动修改，参考 `config.ini.example`。

```ini
[Paths]
PIPELINE_ROOT = D:/Asset_Pipeline
UE_PROJECT_CONTENT = D:/Your_UE_Project/Content
```

## 卸载

直接删除 exe、`config.ini` 和 `.env` 即可。工作目录下的文件需手动清理。
