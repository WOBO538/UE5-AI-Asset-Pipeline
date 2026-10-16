# -*- coding: utf-8 -*-
"""Mock AI 模块：不调用 API 时返回预设的合理假数据。"""


def mock_response(assets: list) -> dict:
    """根据输入资产列表返回一份结构合法的假分析结果。

    参数:
        assets: 资产路径字符串列表

    返回:
        与真实 API 相同结构的 dict，含 results 与 failures 两个数组。
    """
    results = []
    for i, path in enumerate(assets):
        name = path.rsplit("/", 1)[-1]
        # 简单按前缀猜类型
        if name.startswith("T_"):
            category = "Texture"
            suggested = name  # 贴图前缀已合规
        elif name.startswith("SM_"):
            category = "Environment"
            suggested = name
        else:
            category = "Other"
            suggested = "SM_" + name  # 随便加个前缀

        results.append({
            "asset_path": path,
            "suggested_name": suggested,
            "category": category,
            "chinese_description": f"Mock 描述：{name}",
            "tags": ["Mock", "测试", name[:5]],
            "confidence": 0.85,
        })

    return {"results": results, "failures": []}