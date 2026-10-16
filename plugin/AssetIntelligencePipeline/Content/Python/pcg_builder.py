import unreal
import os
import json

# category → Graph 路径映射表（v0.3 外置为 JSON）
CATEGORY_GRAPH_MAP = {
    "Prop":        "/Game/PCG_Templates/PCG_Prop",
    "Environment": "/Game/PCG_Templates/PCG_Prop",
    "Character":   "/Game/PCG_Templates/PCG_Character",
    "VFX":         "/Game/PCG_Templates/PCG_Prop",
    "Material":    "/Game/PCG_Templates/PCG_Prop",
    "Other":       "/Game/PCG_Templates/PCG_Prop",
}

# 这些类别不参与 PCG 布景（贴图/UI/音频不是三维实体）
SKIP_CATEGORIES = {"Texture", "UI", "Audio"}

def _set_text_safe(comp, text, world_size=100.0):
    """尝试多种方式设置 TextRenderComponent 的文本和大小。"""
    ok = False
    for method in ("set_text", ):
        fn = getattr(comp, method, None)
        if fn:
            try:
                fn(text)
                ok = True
                break
            except Exception:
                pass
    if not ok:
        try:
            comp.set_editor_property("text", text)
            ok = True
        except Exception:
            pass

    for method in ("set_world_size", ):
        fn = getattr(comp, method, None)
        if fn:
            try:
                fn(world_size)
                break
            except Exception:
                pass
    else:
        try:
            comp.set_editor_property("world_size", world_size)
        except Exception:
            pass
    return ok


def build_scene_from_ai(ai_analysis_path):
    with open(ai_analysis_path, "r", encoding="utf-8") as f:
        analysis = json.load(f)

    category_groups = {}
    for result in analysis.get("results", []):
        cat = result.get("category", "Other")
        category_groups.setdefault(cat, []).append(result)

    print(f"[PCG] 共 {len(category_groups)} 个 category: {list(category_groups.keys())}")

    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    grid_positions = [
        unreal.Vector(0, 0, 0),
        unreal.Vector(800, 0, 0),
        unreal.Vector(0, 800, 0),
        unreal.Vector(800, 800, 0),
        unreal.Vector(-800, 0, 0),
        unreal.Vector(0, -800, 0),
    ]

    for idx, (category, assets) in enumerate(category_groups.items()):
        if category in SKIP_CATEGORIES:
            print(f"[PCG] 跳过 category '{category}'（非三维资产，不布景）")
            continue

        graph_path = CATEGORY_GRAPH_MAP.get(category, "/Game/PCG_Templates/PCG_Prop")
        print(f"[PCG] 加载 Graph: {graph_path}")

        graph = unreal.EditorAssetLibrary.load_asset(graph_path)
        if not graph:
            print(f"[PCG] 加载 Graph 失败: {graph_path}")
            continue

        pos = grid_positions[idx % len(grid_positions)]

        pcg_vol = sub.spawn_actor_from_class(unreal.PCGVolume, pos)
        pcg_comp = pcg_vol.get_component_by_class(unreal.PCGComponent)
        pcg_comp.set_graph(graph)
        print(f"[PCG] + PCGVolume '{category}' @ {pos}")

        text_pos = unreal.Vector(pos.x, pos.y, pos.z + 300)
        try:
            text_actor = sub.spawn_actor_from_class(unreal.TextRenderActor, text_pos)
        except Exception as e:
            print(f"[PCG]   创建 TextRenderActor 失败: {e}")
            continue

        text_comp = None
        try:
            text_comp = text_actor.get_editor_property("text_render")
        except Exception:
            pass
        if text_comp is None:
            text_comp = text_actor.get_component_by_class(unreal.TextRenderComponent)
        if text_comp is None:
            print(f"[PCG]   无法获取 TextRenderComponent")
            continue

        names = [a["asset_path"].rsplit("/", 1)[-1] for a in assets]
        label = "[{}] AI: {} ({} assets)".format(category, ", ".join(names), len(assets))
        ok = _set_text_safe(text_comp, label, world_size=100.0)
        print(f"[PCG]   + TextRender: {label}  (set_ok={ok})")

    print("[PCG] 场景构建完成")


# 便捷入口：用默认测试路径
def build_from_test_zip():
    """用 TestZip03 的 ai_analysis.json 做测试。"""
    p = os.path.join(
        unreal.Paths.project_dir(),
        "Saved", "AssetPipeline", "Processing", "TestZip03", "ai_analysis.json"
    )
    if not os.path.exists(p):
        print(f"[PCG] 未找到 ai_analysis.json: {p}")
        return
    build_scene_from_ai(p)