# -*- coding: utf-8 -*-
"""给还没有封面的模板生成占位示例图。

用法（在 backend 目录下，用 venv 的 python 跑）：

    .venv/Scripts/python tools/gen_covers.py

- 只处理 cover 为空的模板，已上传真封面的不动；
- 生成 720x900（3:4）的分组主题色渐变图 + 模板名排版，
  右下角带 PLACEHOLDER 水印 —— 提醒用真实管线生成图替换
  （后台「模板」页上传，配了 COS 自动走 COS 存储）。
- 生成后把 cover 写回 local:{id}.jpg。
"""
from __future__ import annotations

import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from templates_store import (TemplateStore, covers_dir)  # noqa: E402
from settings_store import SettingsStore  # noqa: E402

# 分组主题色（上 -> 下渐变）
THEMES = {
    "restore": [(214, 226, 235), (168, 196, 214)],
    "anime": [(255, 224, 214), (244, 172, 154)],
    "poster": [(38, 44, 58), (74, 85, 104)],
    "postcard": [(240, 230, 210), (206, 186, 156)],
}
FALLBACK_THEME = [(226, 232, 240), (178, 190, 205)]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]

W, H = 720, 900


def _font(size: int):
    for cand in FONT_CANDIDATES:
        if os.path.isfile(cand):
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def make_cover(name: str, subtitle: str, group: str) -> bytes:
    top, bottom = THEMES.get(group, FALLBACK_THEME)
    img = Image.new("RGB", (W, H))
    for y in range(H):
        t = y / H
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(0, W, 4):
            for dx in range(4):
                img.putpixel((min(x + dx, W - 1), y), color)

    draw = ImageDraw.Draw(img, "RGBA")

    # 装饰：几组随机圆与斜线，模拟版画噪点
    rng = random.Random(name)
    for _ in range(24):
        r = rng.randint(8, 90)
        x, y = rng.randint(-40, W + 40), rng.randint(-40, H + 40)
        alpha = rng.randint(10, 34)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255, alpha))
    for _ in range(700):
        x, y = rng.randint(0, W - 1), rng.randint(0, H - 1)
        draw.point((x, y), fill=(255, 255, 255, rng.randint(8, 26)))

    # 巨大的首字水印
    big = _font(int(H * 0.42))
    ch = name[0] if name else "图"
    try:
        bw = draw.textlength(ch, font=big)
    except AttributeError:
        bw = big.getsize(ch)[0]
    draw.text(((W - bw) / 2, H * 0.18), ch, font=big, fill=(255, 255, 255, 92))

    # 底部卡带：模板名 + 副标题
    band_top = int(H * 0.76)
    draw.rectangle([0, band_top, W, H], fill=(20, 24, 33, 200))
    pad = int(W * 0.06)
    name_font = _font(int(W * 0.072))
    sub_font = _font(int(W * 0.032))
    draw.text((pad, band_top + int(H * 0.035)), name, font=name_font,
              fill=(250, 250, 248, 255))
    if subtitle:
        draw.text((pad, band_top + int(H * 0.115)), subtitle[:24],
                  font=sub_font, fill=(203, 213, 225, 235))
    draw.text((pad, H - int(H * 0.035)), "PLACEHOLDER · 后台上传真实示例图",
              font=_font(int(W * 0.026)), fill=(148, 163, 184, 220))

    # 边框（新丑风）
    draw.rectangle([6, 6, W - 7, H - 7], outline=(255, 255, 255, 150), width=3)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def main() -> None:
    data_dir = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "data")
    store = TemplateStore(data_dir)
    settings = SettingsStore(data_dir)
    out_dir = covers_dir()

    made = skipped = 0
    for tpl in store.list_templates():
        if str(tpl.get("cover") or "").strip():
            skipped += 1
            continue
        data = make_cover(tpl["name"], tpl.get("subtitle", ""),
                          tpl.get("group_id", ""))
        filename = "%s_v1.jpg" % tpl["id"]
        with open(os.path.join(out_dir, filename), "wb") as fh:
            fh.write(data)
        store.set_cover(tpl["id"], "local:%s" % filename)
        made += 1
        print("生成封面: %s (%s)" % (tpl["name"], filename))
    print("完成：新生成 %d 张，跳过 %d 张（已有封面）" % (made, skipped))


if __name__ == "__main__":
    main()
