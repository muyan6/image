"""Task-local template output variants; never edit the stored template."""
import copy

SINGLE_OUTPUT_RULE = (
    "\n\n【本次成品形式：单独生成图；本段优先于上面的拼图、海报与排版要求】"
    "只保留模板的画风、材质、色彩和主体转化方式。"
    "输出一张完整、连续的风格化成品，不把原照片作为另一个面板放入成品。"
    "禁止上下或左右拼图、对比图、九宫格、重复主体、边框、题字和海报排版。"
    "保留原照片主体及可辨认特征，选择适合主体的完整构图，不机械裁掉模板示例的一半。"
    " Single full-frame styled image only. No original-photo inset, collage, split panels, "
    "before/after comparison, typography or poster layout. These output instructions override "
    "any conflicting layout instructions above; retain the template's visual style."
)


def select_template_output(template, mode="template"):
    if mode not in ("template", "single"):
        raise ValueError("成品形式只能是模板原版或单独生成图")
    if not template:
        if mode == "single":
            raise ValueError("单图成品形式仅适用于模板生成")
        return None
    result = copy.deepcopy(template)
    result["output_mode"] = mode
    if mode == "single":
        result["prompt"] = str(result.get("prompt") or "") + SINGLE_OUTPUT_RULE
        result["layout"] = ""
        result["text_fields"] = []
        result["requires_prompt"] = True
    return result


def select_template_quality(template, quality):
    """Only style/layout come from template; tier models/prices come from global settings."""
    if quality not in ('light','fine'):raise ValueError('Invalid generation tier')
    if not template:return None
    result=copy.deepcopy(template)
    result.update(engine=quality,model_override='',gateway_size='',output_size=0,requires_prompt=True)
    return result
