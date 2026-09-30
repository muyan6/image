"""Task-local template output variants; never edit the stored template."""
import copy

SINGLE_OUTPUT_RULE = (
    "\n\n【本次成品形式：仅生成后图；仅覆盖模板中展示原照片作对比的要求】"
    "如果模板要求把未经生成的原照片与生成后的画面上下、左右拼接，或将原照片放在对比面板、嵌入框中，"
    "省略这些原照片对比区域，只输出生成后的画面，并让该画面完整占据输出画幅。"
    "保留生成部分的主体、可辨认特征、画风、材质、色彩、场景、构图、装饰与文字排版要求。"
    "如果模板本来不展示原照片作对比，严格沿用模板原有要求，不因本选项重新设计构图。"
    "多张生成画面的拼贴、证件照组图、海报边框、题字和装饰不是原照片对比区域，不要删除或合并。"
    "不要对输入或输出做机械裁半，不要把原照片对比区域当成需要保留的生成内容。"
    " Omit only ungenerated input-photo panels used for before/after comparison. "
    "Keep the generated artwork and its composition, decorations and typography. "
    "If no input-photo comparison is requested, follow the original template unchanged. "
    "Preserve collages containing only generated images. Do not crop an existing image."
)


def select_template_output(template, mode="template"):
    if mode not in ("template", "single"):
        raise ValueError("成品形式只能是按模板生成或仅生成后图")
    if not template:
        if mode == "single":
            raise ValueError("单图成品形式仅适用于模板生成")
        return None
    result = copy.deepcopy(template)
    result["output_mode"] = mode
    if mode == "single":
        result["prompt"] = str(result.get("prompt") or "") + SINGLE_OUTPUT_RULE
        # This preference removes input-photo comparison only, not the artwork's layout/text.
        result["requires_prompt"] = True
    return result


def select_template_quality(template, quality):
    """Only style/layout come from template; tier models/prices come from global settings."""
    if quality not in ('light','fine'):raise ValueError('Invalid generation tier')
    if not template:return None
    result=copy.deepcopy(template)
    result.update(engine=quality,model_override='',gateway_size='',output_size=0,requires_prompt=True)
    return result
