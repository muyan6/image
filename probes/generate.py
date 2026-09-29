# -*- coding: utf-8 -*-
"""
探针图生成器
============

给目标修图平台投喂这些图，再从它返回的结果里把信号读回来，
就能反推它的处理链路：重采样核、色调曲线、是否扩散模型、
是否重编码、元数据是否保留……

用法::

    python generate.py          # 生成 input/p1..p8

每张图都埋了特定的「隐藏信息」：

    p1_resample   脉冲栅格 + 条纹扫频 + 锐利边缘  -> 重采样核与缩放倍数
    p2_tone       32 级灰阶梯 + 9 个色块          -> 色调曲线与白平衡增益
    p3_hifreq     1/2/4px 棋盘 + 平滑对照         -> 高频存活率 / 是否重编码
    p4_latent     纯平滑渐变（无 8px 成分）       -> 扩散模型 VAE 网格指纹
    p5_text       大字排版 + 8px 小字             -> 忠实保留 vs 脑补
    p6_texture    多尺度合成纹理                  -> 细节是保留还是重画
    p7_meta       塞满 EXIF / PNG tEXt            -> 元数据存活情况
    p8_flat       7 级灰底 + 已知噪声             -> 降噪强度（提交两次测随机性）
"""
from __future__ import annotations

import os

import cv2
import numpy as np
from PIL import Image, PngImagePlugin

# --------------------------------------------------------------------------- #
# 版面常量（analyze.py 会 import 这些，改这里两边同步）
# --------------------------------------------------------------------------- #
H, W = 1440, 1920          # 长边 1920 > 平台归一化的 1536，顺便测缩放
TAG_BOX = (0, 0, 460, 130)  # 左上角标签区，分析时避开

# p2 色调
WEDGE_Y, WEDGE_H = 200, 170
WEDGE_X0, WEDGE_W, WEDGE_STEPS = 80, 1760, 32
PATCH_Y, PATCH_H, PATCH_X0, PATCH_W = 470, 220, 80, 190
PATCHES = ["white", "gray", "black", "red", "green",
           "blue", "cyan", "magenta", "yellow"]
PATCH_COLORS = {
    "white": (255, 255, 255), "gray": (128, 128, 128), "black": (0, 0, 0),
    "red": (0, 0, 255), "green": (0, 255, 0), "blue": (255, 0, 0),
    "cyan": (255, 255, 0), "magenta": (255, 0, 255), "yellow": (0, 255, 255),
}

# p1 边缘探针位置
EDGE_Y, EDGE_X = 1040, 600

# p3 棋盘区
CHECKER_RECTS = [(60, 200, 620, 900), (680, 200, 1240, 900),
                 (1300, 200, 1860, 900)]

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "input")
RNG = np.random.default_rng(20260927)      # 固定种子，结果可复现


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
def _bgr(gray):
    return cv2.merge([gray, gray, gray])


def _tag(img, text):
    """左上角打标签，肉眼确认哪张是哪张。分析时避开 TAG_BOX。"""
    cv2.putText(img, text, (40, 78), cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                (0, 0, 0), 9, cv2.LINE_AA)
    cv2.putText(img, text, (40, 78), cv2.FONT_HERSHEY_SIMPLEX, 1.8,
                (255, 255, 255), 3, cv2.LINE_AA)
    return img


# --------------------------------------------------------------------------- #
# 探针
# --------------------------------------------------------------------------- #
def p1_resample():
    """重采样指纹：脉冲栅格 + 条纹扫频 + 锐利边缘。"""
    img = np.zeros((H, W, 3), np.uint8)

    # 条纹扫频：周期 2->32px。越细的条纹越先被重采样吃掉，
    # 哪一根开始变成灰糊，就能反推缩放倍率的量级。
    y0, hh = 60, 130
    for i, period in enumerate([2, 3, 4, 6, 8, 12, 16, 24, 32]):
        x0 = 60 + i * 200
        stripe = (((np.arange(170) // period) % 2) * 255).astype(np.uint8)
        img[y0:y0 + hh, x0:x0 + 170] = np.repeat(
            np.repeat(stripe[None, :, None], hh, 0), 3, 2)

    # 脉冲栅格：孤立亮点。重采样后散成「扩散函数」，形状暴露核类型：
    #   有振铃（负瓣）-> Lanczos
    #   平滑无振铃   -> 双三次 / 双线性
    #   方块         -> 最近邻
    for y in range(300, 900, 150):
        for x in range(160, 1800, 160):
            img[y, x] = (255, 255, 255)

    # 锐利边缘：竖边，用来量边缘扩散函数（10-90% 上升宽度 + 过冲）
    img[960:1130, 200:EDGE_X] = 0
    img[960:1130, EDGE_X:1000] = 255
    img[960:1130, 1080:1500] = 64
    # 竖细线栅格：间距 80px，用来量横向模糊
    img[1180:1340, 200:1600] = 0
    img[1180:1340, 200:1600:80] = 255

    return _tag(img, "P1 RESAMPLE")


def p2_tone():
    """色调曲线 + 白平衡：灰阶梯 + 色块。"""
    img = np.full((H, W, 3), 40, np.uint8)

    # 32 级灰阶梯：0 -> 255。输出的每一级相对输入画出来就是色调曲线。
    # 纯超分的曲线是 45 度直线；CLAHE/阴影提亮会把暗部抬起来。
    step_w = WEDGE_W // WEDGE_STEPS
    for i in range(WEDGE_STEPS):
        value = int(round(i * 255 / (WEDGE_STEPS - 1)))
        x0 = WEDGE_X0 + i * step_w
        img[WEDGE_Y:WEDGE_Y + WEDGE_H, x0:x0 + step_w] = value

    # 9 个色块：白平衡 / 饱和度 / 色相偏移的直接读数
    for i, name in enumerate(PATCHES):
        x0 = PATCH_X0 + i * (PATCH_W + 10)
        img[PATCH_Y:PATCH_Y + PATCH_H, x0:x0 + PATCH_W] = PATCH_COLORS[name]

    return _tag(img, "P2 TONE")


def p3_hifreq():
    """高频存活：棋盘 + 平滑对照。"""
    img = np.full((H, W, 3), 110, np.uint8)
    yy, xx = np.indices((H, W))

    for period, (x0, y0, x1, y1) in zip((1, 2, 4), CHECKER_RECTS):
        c = (((yy // period) + (xx // period)) % 2 * 255).astype(np.uint8)
        img[y0:y1, x0:x1] = _bgr(c[y0:y1, x0:x1])

    # 底部平滑渐变作对照：棋盘区丢了高频、这里没丢，就说明是重编码/降噪。
    # 行数必须与目标切片一致：切片 [1040 : H-40] 的高度是 H-40-1040。
    top, bottom = 1040, H - 40
    grad = np.tile(np.linspace(0, 255, W, dtype=np.uint8), (bottom - top, 1))
    img[top:bottom, :] = _bgr(grad)

    return _tag(img, "P3 HIFREQ")


def p4_latent():
    """扩散模型指纹：纯平滑渐变，刻意不含任何 8px 周期成分。

    扩散模型的 VAE 是 8 倍空间下采样，解码时会留下每 8 像素一个周期的
    网格痕迹。输入里没有这个频率，输出里出现 -> 只可能是模型带出来的。
    """
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    smooth = 128 + 55 * np.sin(xx / 700.0) + 38 * np.cos(yy / 600.0)
    smooth += RNG.normal(0, 1.2, (H, W))        # 极低幅度噪声，不含周期
    return _tag(_bgr(np.clip(smooth, 0, 255).astype(np.uint8)), "P4 LATENT")


def p5_text():
    """生成式检测：图里写字。忠实保留 = 判别式；糊掉/脑补 = 生成式。"""
    img = np.full((H, W, 3), 215, np.uint8)
    lines = ["PHOTO RESCUE PROBE 5", "ABCDEFGHIJKLM", "NOPQRSTUVWXYZ",
             "0123456789", "the quick brown fox"]
    y = 250
    for line in lines:
        cv2.putText(img, line, (140, y), cv2.FONT_HERSHEY_SIMPLEX,
                    2.2, (25, 25, 25), 5, cv2.LINE_AA)
        y += 165
    # 小字号：越小的字越容易被「脑补」成别的笔画
    cv2.putText(img, "small text probe 8px", (140, y + 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (25, 25, 25), 1, cv2.LINE_AA)
    return _tag(img, "P5 TEXT")


def p6_texture():
    """细节再造：多尺度合成纹理。超分保留统计特征，扩散模型会重画。"""
    tex = np.zeros((H, W), np.float32)
    for sigma, amp in ((1.0, 1.0), (2.0, 0.6), (4.0, 0.4), (8.0, 0.25)):
        n = RNG.normal(0, 1, (H, W)).astype(np.float32)
        tex += amp * cv2.GaussianBlur(n, (0, 0), sigma)
    tex -= tex.min()
    tex /= max(float(tex.max()), 1e-6)
    return _tag(_bgr((tex * 255).astype(np.uint8)), "P6 TEXTURE")


def p7_meta():
    """元数据载体。真正的载荷在 EXIF/tEXt 里，不在像素上。"""
    img = np.full((H, W, 3), 120, np.uint8)
    cv2.circle(img, (W // 2, H // 2), 420, (205, 165, 125), -1)
    cv2.circle(img, (W // 2, H // 2), 300, (150, 110, 80), -1)
    return _tag(img, "P7 META")


def p8_flat():
    """噪声底：7 级灰底 + 已知 sigma 的高斯噪声，量降噪强度。"""
    img = np.zeros((H, W, 3), np.uint8)
    levels = [32, 64, 96, 128, 160, 192, 224]
    band = H // len(levels)
    for i, lv in enumerate(levels):
        y0 = i * band
        noise = RNG.normal(0, 6.0, (band, W))
        img[y0:y0 + band] = _bgr(
            np.clip(lv + noise, 0, 255).astype(np.uint8))
    return _tag(img, "P8 FLAT")


# --------------------------------------------------------------------------- #
# 保存（带上元数据载荷）
# --------------------------------------------------------------------------- #
def save(name, img_bgr):
    path = os.path.join(OUT, name + ".png")
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    im = Image.fromarray(rgb)

    # EXIF 载荷：这些字段能不能活着回来，直接暴露平台的图像库
    exif = im.getexif()
    for tag, value in (
        (0x010F, "ProbeCam"),                    # Make
        (0x0110, "PROBE-1"),                     # Model
        (0x0131, "probe-gen 1.0"),               # Software
        (0x0132, "2026:09:27 12:00:00"),         # DateTime
        (0x010E, "PROBE-DESCRIPTION"),           # ImageDescription
        (0x013B, "PROBE-ARTIST"),                # Artist
        (0x8298, "PROBE-COPYRIGHT"),             # Copyright
    ):
        try:
            exif[tag] = value
        except Exception:
            pass

    # PNG tEXt 载荷
    info = PngImagePlugin.PngInfo()
    info.add_text("Software", "probe-gen 1.0")
    info.add_text("Comment", "PROBE-PNG-COMMENT")
    info.add_text("Probe", name)

    im.save(path, "PNG", exif=exif, pnginfo=info)
    size = os.path.getsize(path)
    print("  %-16s %dx%d  %8d bytes" % (name + ".png", W, H, size))


def main():
    os.makedirs(OUT, exist_ok=True)
    probes = [
        ("p1_resample", p1_resample),
        ("p2_tone",     p2_tone),
        ("p3_hifreq",   p3_hifreq),
        ("p4_latent",   p4_latent),
        ("p5_text",     p5_text),
        ("p6_texture",  p6_texture),
        ("p7_meta",     p7_meta),
        ("p8_flat",     p8_flat),
    ]
    print("生成探针图 -> %s" % OUT)
    for name, fn in probes:
        save(name, fn())
    print()
    print("完成。提交顺序建议（按「每条小鱼干换到的信息量」排序）：")
    print("  1. p8_flat  提交两次  -> 先定生死：输出是否逐字节相同")
    print("  2. p2_tone            -> 色调曲线，判断有没有滤镜层")
    print("  3. p4_latent          -> 8px 网格，判断是不是扩散模型")
    print("  4. p1_resample        -> 缩放倍数与重采样核")
    print("  5. p7_meta            -> 元数据，常常直接暴露工具名")
    print("  6~8. p3 / p5 / p6     -> 补充证据")
    print()
    print("把平台返回的图放进 result/ ，文件名里带上探针名即可")


if __name__ == "__main__":
    main()
