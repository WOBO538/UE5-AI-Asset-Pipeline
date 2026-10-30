# Asset Pipeline v3.0.0

美术资产自动流转工具（外部流转层）。

## 使用方法

1. 双击 `AssetPipeline.exe`
2. 首次启动会弹出配置向导：
   - 选择你的 UE 项目文件（.uproject）
   - 选择管线工作根目录（可直接取消，默认使用 UE 项目同盘的 `Asset_Pipeline`）
3. 配置完成后，程序进入监听模式
4. 把资产压缩包（.zip，**内部必须包含 `Content` 文件夹**）丢进 Inbox 目录
5. 程序会自动解压、导入资产、生成清单、归档

## 目录结构

程序会在工作根目录下自动创建：

- `Inbox/` —— 待处理的 Zip 投放目录
- `Processing/` —— 解压中转目录
- `Archive/` —— 按日期归档的原始 Zip

资产会被复制到 UE 项目的 `Content/Imported/`，同时生成 `import_manifest.json` 和 `trigger.txt` 供 UE5 插件消费。

## 常见问题

- **程序闪退/无反应**：检查 Inbox 是否可写，或杀毒软件是否拦截
- **Zip 处理失败**：确认压缩包完整（未损坏），且包含 Content 文件夹
- **归档失败**：通常是杀毒软件占用文件，稍后重试；不影响已处理的资产

## 配置说明

首次启动后会在 exe 同目录生成 `config.ini`。如需手动修改，参考 `config.ini.example`。

## 卸载

直接删除 exe 和 config.ini 即可。工作目录下的文件需手动清理。