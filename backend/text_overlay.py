# -*- coding: utf-8 -*-
"""中文文字排版合成：AI 画底图，代码叠加真实排版。

为什么不用模型直接生成文字：生成模型至今排不稳长段中文
（错字、变形、间距不齐），而海报/明信片的核心卖点恰恰是
"用户自定义文字保证正确"。所以底图提示词会要求模型不画文字，
文字由本模块用 Pillow 叠加 —— 字体、字号、位置全部程序可控。

排版预设（layout，与 templates_store.TEXT_LAYOUTS 对应）：

    postcard_bottom  明信片：底部白色卡带，标题居左、日期居右
    poster_center    海报：下三分之一暗色纱罩，标题副标题居中
    stamp_corner     邮票角标：右上角邮戳白框（日期 + 地点）

自适应：文字超宽自动缩字号，body 角色自动折行。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# 中文字体候选：按序找到第一个存在的（Windows -> Linux -> macOS）
_FONT_CANDIDATES = [
    os.environ.get("TEXT_FONT_PATH", ""),
    "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑 Bold
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
]

_FONT_PATH: Optional[str] = None


def _font_path() -> str:
    global _FONT_PATH
    if _FONT_PATH is None:
        for cand in _FONT_CANDIDATES:
            if os.path.isfile(cand):
                _FONT_PATH = cand
                break
        else:
            _FONT_PATH = ""  # 兜底：PIL 内置位图字体（不支持中文尺寸控制）
    return _FONT_PATH


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = _font_path()
    if not path:
        raise RuntimeError("中文字体未就绪：请安装 fonts-noto-cjk 或设置 TEXT_FONT_PATH")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        raise RuntimeError("中文字体读取失败：请检查 TEXT_FONT_PATH")


def _text_width(draw: ImageDraw.ImageDraw, text: str,
                font: ImageFont.FreeTypeFont) -> float:
    try:
        return draw.textlength(text, font=font)
    except AttributeError:  # 极老版本 Pillow
        return font.getsize(text)[0]


def _shrink_to_fit(draw: ImageDraw.ImageDraw, text: str, max_width: float,
                   size: int, min_size: int = 12) -> ImageFont.FreeTypeFont:
    """字号从 size 往下探，直到文本宽度不超过 max_width。"""
    while size > min_size:
        font = _font(size)
        if _text_width(draw, text, font) <= max_width:
            return font
        size = int(size * 0.92)
    return _font(min_size)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: float) -> List[str]:
    """按宽度折行（中文按字符切，英文按词切）。"""
    lines: List[str] = []
    line = ""
    for ch in text:
        if ch == "\n":
            lines.append(line)
            line = ""
            continue
        if _text_width(draw, line + ch, font) > max_width and line:
            lines.append(line)
            line = ch
        else:
            line += ch
    if line:
        lines.append(line)
    return lines or [""]


def _body_block(draw, text, max_width, size, max_height):
    """Wrap every paragraph and fit the whole block without truncating text."""
    size=max(1,int(size))
    while True:
        font=_font(size)
        lines=_wrap_text(draw,text.replace('\r\n','\n').replace('\r','\n'),font,max_width)
        leading=size+max(1,int(size*.25))
        if len(lines)*leading<=max_height or size==1:
            return font,lines,leading
        size=max(1,int(size*.9))


def _today_str() -> str:
    import time
    return time.strftime("%Y.%m.%d")


def collect_values(spec: List[Dict[str, Any]],
                   values: Optional[Dict[str, str]]) -> Dict[str, str]:
    """按模板的字段规格合并用户输入：缺失填默认值，{today} 换成当天。

    返回 {key: 文本}，只保留非空项。
    """
    merged: Dict[str, str] = {}
    for f in spec or []:
        key = f.get("key", "")
        raw = str((values or {}).get(key, "") or "").strip()
        if not raw:
            raw = str(f.get("default", "") or "").strip()
        if raw == "{today}":
            raw = _today_str()
        max_len = int(f.get("max_len", 30) or 30)
        if max_len > 0:
            raw = raw[:max_len]
        if raw:
            merged[key] = raw
    return merged


def apply(input_path: str, output_path: str, layout: str,
          spec: List[Dict[str, Any]], values: Dict[str, str]) -> str:
    """把文字叠到底图上并写回 output_path（JPEG）。"""
    img = Image.open(input_path).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    role_text = _by_role(spec, values)

    if layout == "poster_center":
        _draw_poster_center(img, draw, role_text)
    elif layout == "stamp_corner":
        _draw_stamp_corner(img, draw, role_text)
    else:  # postcard_bottom（默认）
        _draw_postcard_bottom(img, draw, role_text)

    img.save(output_path, "JPEG", quality=95)
    return output_path


def _by_role(spec: List[Dict[str, Any]],
             values: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for f in spec or []:
        text = values.get(f.get("key", ""), "")
        if text:
            out[f.get("role", "body")] = text
    return out


# --------------------------------------------------------------------------- #
# 排版预设
# --------------------------------------------------------------------------- #
def _draw_postcard_bottom(img: Image.Image, draw: ImageDraw.ImageDraw,
                          role: Dict[str, str]) -> None:
    """明信片：底部白色卡带。标题（左，加粗黑）+ 日期（右，灰小字）。"""
    w, h = img.size
    band_h = max(int(h * 0.13), 56)
    header_h=band_h
    body=role.get('body','')
    body_font,body_lines,body_leading=(None,[],0)
    if body:
        body_font,body_lines,body_leading=_body_block(draw,body,w*.89,max(12,int(h*.020)),max(1,int(h*.40)))
        band_h+=len(body_lines)*body_leading+int(h*.025)
    band_top = h - band_h
    draw.rectangle([0, band_top, w, h], fill=(250, 249, 246, 255))
    draw.line([(0, band_top), (w, band_top)], fill=(31, 41, 55, 60), width=max(2, h // 900))

    pad = int(w * 0.055)
    title = role.get("title", "")
    subtitle = role.get("subtitle", "")
    date = role.get("date", "")
    sign = role.get("sign", "")
    ink = (31, 41, 55, 255)
    gray = (107, 114, 128, 255)

    # 左侧：标题（自动缩到卡带宽度一半以内）+ 副标题
    if title:
        size = max(int(header_h * 0.42), 18)
        font = _shrink_to_fit(draw, title, w * 0.62, size)
        draw.text((pad, band_top + int(header_h * 0.16)), title, font=font, fill=ink)
        if subtitle:
            sub_font = _font(max(int(header_h * 0.24), 11))
            sub_y = band_top + int(header_h * 0.16) + font.size + int(header_h * 0.08)
            if sub_y + sub_font.size < h - 4:
                draw.text((pad, sub_y), subtitle, font=sub_font, fill=gray)
    elif subtitle:
        font = _shrink_to_fit(draw, subtitle, w * 0.62, max(int(header_h * 0.32), 14))
        draw.text((pad, band_top + int(header_h * 0.3)), subtitle, font=font, fill=ink)

    # 右侧：日期 / 落款
    right = role.get("date") or sign
    if right:
        font = _font(max(int(header_h * 0.26), 12))
        rw = _text_width(draw, right, font)
        draw.text((w - pad - rw, band_top + int(header_h * 0.34)),
                  right, font=font, fill=gray)
    if body_font:
        y=band_top+header_h
        for line in body_lines:
            draw.text((pad,y),line,font=body_font,fill=ink)
            y+=body_leading


def _draw_poster_center(img: Image.Image, draw: ImageDraw.ImageDraw,
                        role: Dict[str, str]) -> None:
    """海报：下三分之一加深色纱罩保证可读性，文字居中。"""
    w, h = img.size
    title = role.get("title", "")
    subtitle = role.get("subtitle", "")
    date = role.get("date", "")
    body = role.get("body", "")
    sign = role.get("sign", "")

    # 预估文字块高度，纱罩盖住它
    title_font = _font(int(h * 0.075)) if title else None
    sub_font = _font(int(h * 0.028)) if subtitle else None
    small_font = _font(int(h * 0.022)) if (date or sign) else None
    body_font,body_lines,body_leading=(None,[],0)
    if body:
        body_font,body_lines,body_leading=_body_block(draw,body,w*.86,max(12,int(h*.022)),max(1,int(h*.48)))
    block_h = 0
    if title_font:
        block_h += title_font.size
    if sub_font:
        block_h += int(sub_font.size * 1.7)
    if small_font:
        block_h += int(small_font.size * 1.6)
    if body_font:
        block_h+=len(body_lines)*body_leading+int(h*.015)
    block_h = max(block_h, int(h * 0.12))

    scrim_top = h - block_h - int(h * 0.07)
    # 纵向渐变纱罩：从透明到深黑，比硬边矩形高级
    scrim = Image.new("L", (1, h - scrim_top), 0)
    for y in range(scrim.size[1]):
        scrim.putpixel((0, y), int(200 * (y / scrim.size[1]) ** 1.5))
    scrim = scrim.resize((w, scrim.size[1]))
    black = Image.new("RGBA", (w, h - scrim_top), (10, 10, 14, 255))
    black.putalpha(scrim)
    img.paste(black, (0, scrim_top), black)

    cx = w / 2
    y = h - block_h - int(h * 0.045)
    white = (250, 250, 248, 255)
    if title:
        font = _shrink_to_fit(draw, title, w * 0.86, title_font.size)
        tw = _text_width(draw, title, font)
        draw.text((cx - tw / 2, y), title, font=font, fill=white)
        y += font.size + int(h * 0.012)
    if subtitle:
        font = _shrink_to_fit(draw, subtitle, w * 0.8, sub_font.size)
        tw = _text_width(draw, subtitle, font)
        draw.text((cx - tw / 2, y), subtitle, font=font,
                  fill=(214, 211, 209, 255))
        y += int(font.size * 1.7)
    if body_font:
        for line in body_lines:
            tw=_text_width(draw,line,body_font)
            draw.text((cx-tw/2,y),line,font=body_font,fill=(214,211,209,255))
            y+=body_leading
        y+=int(h*.015)
    if date or sign:
        text = " · ".join(x for x in (date, sign) if x)
        font = _font(small_font.size)
        tw = _text_width(draw, text, font)
        draw.text((cx - tw / 2, y), text, font=font, fill=(163, 163, 163, 255))


def _draw_stamp_corner(img: Image.Image, draw: ImageDraw.ImageDraw,
                       role: Dict[str, str]) -> None:
    """邮票角标：右上角邮戳白框（第一行日期，第二行地点/落款）。"""
    w, h = img.size
    date = role.get("date", "")
    place = role.get("subtitle") or role.get("sign") or ""
    body=role.get('body','')
    if not (date or place or body):
        return

    line1 = date or place
    line2 = place if (date and place) else ""
    font1 = _font(max(int(h * 0.026), 13))
    font2 = _font(max(int(h * 0.02), 10))
    body_font,body_lines,body_leading=(None,[],0)
    if body:
        body_font,body_lines,body_leading=_body_block(draw,body,w*.42,max(12,int(h*.020)),max(1,int(h*.45)))

    w1 = _text_width(draw, line1, font1)
    w2 = _text_width(draw, line2, font2) if line2 else 0
    box_w = int(max(w1, w2) + h * 0.05)
    box_h = int(font1.size * 1.5 + (font2.size * 1.5 if line2 else 0) + h * 0.02)
    if body_font:
        box_w=int(max(w1,w2,*(_text_width(draw,line,body_font) for line in body_lines))+h*.05)
        box_h+=len(body_lines)*body_leading+int(h*.012)
    x1, y1 = w - int(w * 0.05) - box_w, int(h * 0.05)

    # 白底 + 双线框（外实内虚，邮票感）
    draw.rectangle([x1, y1, x1 + box_w, y1 + box_h],
                   fill=(252, 251, 248, 235), outline=(31, 41, 55, 220),
                   width=max(2, h // 700))
    inner = 4
    for i in range(inner, inner + 3):
        draw.rectangle([x1 + i, y1 + i, x1 + box_w - i, y1 + box_h - i],
                       outline=(31, 41, 55, 90))
    tx = x1 + (box_w - w1) / 2
    draw.text((tx, y1 + int(h * 0.008)), line1, font=font1,
              fill=(31, 41, 55, 255))
    if line2:
        tx = x1 + (box_w - w2) / 2
        draw.text((tx, y1 + int(font1.size * 1.5) + int(h * 0.006)),
                  line2, font=font2, fill=(107, 114, 128, 255))
    if body_font:
        y=y1+int(font1.size*1.5)+(int(font2.size*1.5) if line2 else 0)+int(h*.012)
        for line in body_lines:
            draw.text((x1+h*.025,y),line,font=body_font,fill=(31,41,55,255))
            y+=body_leading
