# -*- coding: utf-8 -*-
"""
探针结果分析器
==============

读平台返回的图，把埋进去的信号提取出来，反推它的处理链路。

用法::

    python analyze.py result/*.png            # 分析指定的返回图
    python analyze.py --dir result            # 分析整个目录
    python analyze.py --dir result --json out.json

文件名以 p1..p8 开头即可自动识别探针类型。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys

import cv2
import numpy as np

# 版面常量与探针生成器共用，避免两边漂移
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from generate import (  # noqa: E402
    CHECKER_RECTS, EDGE_X, EDGE_Y, H, PATCHES, PATCH_COLORS, PATCH_H,
    PATCH_W, PATCH_X0, PATCH_Y, W, WEDGE_H, WEDGE_STEPS, WEDGE_W, WEDGE_X0,
    WEDGE_Y,
)

STRIPE_PERIODS = [2, 3, 4, 6, 8, 12, 16, 24, 32]
STRIPE_Y0, STRIPE_H = 60, 130
STRIPE_X0, STRIPE_W = 60, 170
STRIPE_STEP = 200

FLAT_LEVELS = [32, 64, 96, 128, 160, 192, 224]
FLAT_NOISE_SIGMA = 6.0

# 上游归一化目标（原站与我们一致：长边 1536）
UPSTREAM_LONG_SIDE = 1536

# 左上角标签区。generate.py 在这里画了探针名，分析时必须避开 ——
# 否则标签的高对比笔画会污染统计量（p1 的 2px 条纹、p8 的第一条灰带都会中招）。
from generate import TAG_BOX  # noqa: E402

TAG_X1, TAG_Y1 = TAG_BOX[2], TAG_BOX[3]


# --------------------------------------------------------------------------- #
# 基础
# --------------------------------------------------------------------------- #
def load(path):
    """Unicode 安全地读图，返回 BGR。"""
    data = np.fromfile(path, dtype=np.uint8)
    if data.size == 0:
        raise ValueError("空文件：%s" % path)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("无法解码：%s" % path)
    return img


def probe_kind(name):
    """从文件名提取 p1..p8。"""
    base = os.path.basename(name).lower()
    for i in range(1, 9):
        if base.startswith("p%d" % i) or ("_p%d" % i) in base:
            return "p%d" % i
    return None


def file_meta(path):
    """容器格式、体积、PNG 文本块、EXIF。"""
    out = {
        "bytes": os.path.getsize(path),
        "ext": os.path.splitext(path)[1].lower(),
        "png_text": {},
        "exif": {},
        "jpeg_quality_hint": None,
    }
    try:
        from PIL import Image
        with Image.open(path) as im:
            out["format"] = im.format
            out["mode"] = im.mode
            out["size"] = list(im.size)
            if im.format == "PNG":
                for k, v in (im.info or {}).items():
                    if isinstance(v, str) and len(v) < 300:
                        out["png_text"][str(k)] = v
            exif = im.getexif()
            if exif:
                for tag, value in exif.items():
                    try:
                        out["exif"][str(tag)] = str(value)[:200]
                    except Exception:
                        pass
    except Exception as exc:  # noqa: BLE001
        out["pil_error"] = str(exc)
    return out


def grader(path, img):
    """按输出长边相对原始版面估算缩放倍率。"""
    h, w = img.shape[:2]
    return {
        "out_w": w, "out_h": h,
        "scale_x": w / float(W),
        "scale_y": h / float(H),
        "upscale_vs_1536": max(w, h) / float(UPSTREAM_LONG_SIDE),
    }


def rect(img, x, y, w, h):
    """按版面坐标取 ROI，越界自动裁剪。"""
    H_, W_ = img.shape[:2]
    x0 = max(0, min(W_, int(round(x))))
    y0 = max(0, min(H_, int(round(y))))
    x1 = max(x0 + 1, min(W_, int(round(x + w))))
    y1 = max(y0 + 1, min(H_, int(round(y + h))))
    return img[y0:y1, x0:x1]


def median_bgr(patch):
    """ROI 的中位色，抗噪。"""
    if patch.size == 0:
        return (0.0, 0.0, 0.0)
    flat = patch.reshape(-1, 3)
    return tuple(float(v) for v in np.median(flat, axis=0))


# --------------------------------------------------------------------------- #
# p2 色调曲线 + 白平衡
# --------------------------------------------------------------------------- #
def analyze_tone(img, sx, sy, report):
    """32 级灰阶梯 -> 色调曲线；9 色块 -> 白平衡与饱和度。"""
    step_w = WEDGE_W // WEDGE_STEPS
    measured, expected = [], []
    for i in range(WEDGE_STEPS):
        exp = i * 255.0 / (WEDGE_STEPS - 1)
        # 取阶梯中心，避开边缘过渡
        cx = WEDGE_X0 + i * step_w + step_w * 0.5
        cy = WEDGE_Y + WEDGE_H * 0.5
        patch = rect(img, cx * sx - 6 * sx, cy * sy - 12 * sy, 12 * sx, 24 * sy)
        b, g, r = median_bgr(patch)
        measured.append((r + g + b) / 3.0)
        expected.append(exp)

    report["tone_curve"] = {
        "expected": [round(v, 1) for v in expected],
        "measured": [round(v, 1) for v in measured],
    }

    # 关键取样点
    def at(level):
        idx = min(range(len(expected)), key=lambda i: abs(expected[i] - level))
        return round(measured[idx], 1)

    report["tone_points"] = {
        "in_0": at(0), "in_64": at(64), "in_128": at(128),
        "in_192": at(192), "in_255": at(255),
    }

    # 线性拟合（跳过纯黑与纯白，它们常被裁剪）
    xs = np.array(expected, dtype=np.float64)
    ys = np.array(measured, dtype=np.float64)
    mask = (xs > 8) & (xs < 247)
    if mask.sum() >= 3:
        slope, intercept = np.polyfit(xs[mask], ys[mask], 1)
        report["tone_fit"] = {
            "slope": round(float(slope), 4),
            "intercept": round(float(intercept), 2),
            "gamma": round(float(np.log(0.5) /
                                  np.log(max(1e-6,
                                             (0.5 - intercept) / max(1e-6, slope))
                                          if slope > 0 else 1e-6)), 3)
            if slope > 0 else None,
        }
        verdict = []
        if abs(slope - 1.0) < 0.03 and abs(intercept) < 4:
            verdict.append("近似线性（无色调曲线）")
        if intercept > 6:
            verdict.append("暗部抬升（阴影提亮 / 黑位抬高）")
        if slope > 1.06:
            verdict.append("对比度增强（S 曲线）")
        if slope < 0.94:
            verdict.append("对比度降低（雾化 / 压平）")
        if at(255) < 248:
            verdict.append("高光被压（白点未到 255）")
        if at(0) > 12:
            verdict.append("纯黑被抬起（黑位 > 12）")
        report["tone_verdict"] = verdict or ["轻度调整，接近原样"]

    # 色块
    patches = {}
    for i, name in enumerate(PATCHES):
        x0 = PATCH_X0 + i * (PATCH_W + 10)
        patch = rect(img, (x0 + PATCH_W * 0.5) * sx - 20 * sx,
                     (PATCH_Y + PATCH_H * 0.5) * sy - 20 * sy,
                     40 * sx, 40 * sy)
        b, g, r = median_bgr(patch)
        eb, eg, er = PATCH_COLORS[name]
        patches[name] = {
            "in_rgb": [er, eg, eb],
            "out_rgb": [round(r, 1), round(g, 1), round(b, 1)],
        }
    report["patches"] = patches

    # 白平衡增益：用灰块
    if "gray" in patches:
        r, g, b = patches["gray"]["out_rgb"]
        mid = (r + g + b) / 3.0 or 1.0
        report["white_balance"] = {
            "gain_r": round(r / mid, 4),
            "gain_g": round(g / mid, 4),
            "gain_b": round(b / mid, 4),
        }
        gains = report["white_balance"]
        spread = max(gains["gain_r"], gains["gain_b"]) - min(
            gains["gain_r"], gains["gain_b"])
        report["wb_verdict"] = (
            "灰块被中和（有白平衡）" if spread < 0.04
            else "灰块有偏色（无白平衡或偏色保留）"
        )

    # 饱和度：比较红块相对灰块的色度
    if "red" in patches and "gray" in patches:
        r, g, b = patches["red"]["out_rgb"]
        gray = sum(patches["gray"]["out_rgb"]) / 3.0 or 1.0
        sat = (max(r, g, b) - min(r, g, b)) / gray
        report["saturation_red"] = round(sat, 4)
        if "blue" in patches:
            rb, gb, bb = patches["blue"]["out_rgb"]
            sat_b = (max(rb, gb, bb) - min(rb, gb, bb)) / gray
            report["saturation_blue"] = round(sat_b, 4)


# --------------------------------------------------------------------------- #
# p4 扩散模型指纹
# --------------------------------------------------------------------------- #
def periodic_peaks(signal, top=6):
    """找一维信号里的强周期成分，返回 [(周期px, 相对强度)]。"""
    x = np.asarray(signal, dtype=np.float64)
    x = x - x.mean()
    if x.size < 64:
        return []
    win = np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x * win))
    freqs = np.fft.rfftfreq(x.size)
    spec[0] = 0.0
    if spec.max() <= 0:
        return []
    spec /= spec.max()
    # 局部极大
    cand = []
    for i in range(2, len(spec) - 2):
        if spec[i] > 0.12 and spec[i] >= spec[i - 1] and spec[i] >= spec[i + 1] \
                and spec[i] > spec[i - 2] and spec[i] > spec[i + 2]:
            if freqs[i] > 0:
                cand.append((1.0 / freqs[i], float(spec[i])))
    cand.sort(key=lambda t: -t[1])
    return [(round(p, 2), round(a, 4)) for p, a in cand[:top]]


def grid_2d_score(gray):
    """2D FFT：找 4/8/16/32 px 的二维周期能量集中。

    一维行/列均值只能发现「条纹型」网格。纯棋盘型（±1 交替）在均值上
    会相互抵消 —— 对固定 y 沿 x 求和，一半 +1 一半 -1，正好归零。
    所以必须对整块做 2D FFT。

    但只搜对角方向同样会漏：条纹型网格的能量落在 (±1/p, 0) / (0, ±1/p)
    的轴上，对角搜索完全看不见它。反过来只搜轴对齐会漏掉棋盘型。
    因此每个候选周期同时搜八个方向：

      - 轴对齐 (±1/p, 0) / (0, ±1/p)  -> 条纹型网格
      - 对角   (±1/p, ±1/p)           -> 棋盘型网格

    命名约定（容易混淆，写清楚）：这里返回的「周期 p」是**沿坐标轴的
    重复周期**，也就是 1/(fx) 的分母。

      - p=8 的条纹  = 8px 明暗交替，谱峰在 (1/8, 0)，轴方向
      - p=8 的棋盘  = 4px 方块交替，谱峰在 (1/8, 1/8)，对角方向
      - 8px 方块的棋盘（相邻同色方块各 8px，明暗每 16px 重复一次）
        谱峰在 (1/16, 1/16)，因此**报为 p=16**，不是 p=8

    换句话说：棋盘型的读数天然是「方块边长 × 2」。判读时不要拿
    读数直接当方块尺寸，方块边长 = p/2。

    返回 (scores, best)：
      scores = {周期: 该周期上最强的 峰值/局部基线 比}
      best   = (周期, 比值, 方向种类)，方向种类为 "axis" 或 "diag"
    """
    h, w = gray.shape
    if h < 64 or w < 64:
        return {}, None

    # 汉宁窗压边缘泄漏，否则四条边的不连续会在频谱里造出假峰
    win = np.outer(np.hanning(h), np.hanning(w))
    g = (gray - float(gray.mean())) * win
    spec = np.abs(np.fft.fftshift(np.fft.fft2(g)))

    cy, cx = h // 2, w // 2

    # 归一化频率（周期/像素的倒数）的径向距离
    yy, xx = np.mgrid[0:h, 0:w]
    rad = np.sqrt(((yy - cy) / float(h)) ** 2 + ((xx - cx) / float(w)) ** 2)

    DIRS = (
        ("axis", 1.0, 0.0), ("axis", -1.0, 0.0),
        ("axis", 0.0, 1.0), ("axis", 0.0, -1.0),
        ("diag", 1.0, 1.0), ("diag", 1.0, -1.0),
        ("diag", -1.0, 1.0), ("diag", -1.0, -1.0),
    )

    scores = {}
    best = None
    for period in (4, 8, 16, 32):
        target = 1.0 / float(period)
        period_peak = 0.0
        period_kind = None

        for kind, uy, ux in DIRS:
            fy, fx = uy * target, ux * target
            # 背景必须按**该方向自己的半径**取环带：对角峰的半径是轴对齐的
            # sqrt(2) 倍，混用会让两类方向的比值不可比。
            # 也不能用全谱中位数 —— 高频 bin 大多接近 0，那样任何图都"有网格"。
            r = float(np.hypot(fy, fx))
            band = np.abs(rad - r) < r * 0.30
            band[cy - 2:cy + 3, cx - 2:cx + 3] = False
            if not band.any():
                continue
            local_bg = float(np.median(spec[band]))
            if local_bg <= 0:
                continue

            y = int(round(cy + fy * h))
            x = int(round(cx + fx * w))
            if not (0 <= y < h and 0 <= x < w):
                continue
            # ±1 邻域取最大，容忍栅格对齐误差
            patch = spec[max(0, y - 1):y + 2, max(0, x - 1):x + 2]
            if patch.size == 0:
                continue
            ratio = float(patch.max()) / local_bg
            if ratio > period_peak:
                period_peak = ratio
                period_kind = kind

        if period_kind is None:
            continue
        scores[str(period)] = round(period_peak, 2)
        if best is None or period_peak > best[1]:
            best = (period, period_peak, period_kind)
    return scores, best


def analyze_latent(img, sx, sy, report):
    """纯平滑渐变里出现的周期成分，只可能来自模型内部。

    扩散模型的 VAE 是 8 倍空间下采样，解码时会留下 8 像素周期的网格。
    输入（p4）刻意不含任何 8px 成分，所以输出里出现就是模型带出来的。

    两条证据链并用：
      1D 行/列均值 -> 条纹型网格
      2D FFT       -> 棋盘型网格（1D 会漏）
    """
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float64)
    h, w = gray.shape

    # 取中央大块，避开标签与边界
    cy0, cy1 = int(h * 0.25), int(h * 0.85)
    cx0, cx1 = int(w * 0.15), int(w * 0.90)
    core = gray[cy0:cy1, cx0:cx1]

    # 行/列均值：横向网格看列均值，纵向网格看行均值
    row_profile = core.mean(axis=1)
    col_profile = core.mean(axis=0)

    report["latent"] = {
        "row_peaks": periodic_peaks(row_profile),
        "col_peaks": periodic_peaks(col_profile),
        "core_std": round(float(core.std()), 3),
        "core_mean": round(float(core.mean()), 2),
    }

    # 判定：是否有接近 8 的整数倍周期（1D 证据）
    hits = []
    for label, peaks in (("横向", report["latent"]["col_peaks"]),
                         ("纵向", report["latent"]["row_peaks"])):
        for period, amp in peaks:
            if amp < 0.18:
                continue
            if 6.0 <= period <= 34.0:
                hits.append({"axis": label, "period_px": period, "amp": amp})
    report["latent"]["grid_hits"] = hits

    # 2D 证据：棋盘型网格在 1D 均值上会互相抵消，只有 2D FFT 能看到
    scores, best = grid_2d_score(core)
    report["latent"]["grid_2d_ratio"] = scores
    report["latent"]["grid_2d_best"] = (
        {"period_px": best[0], "ratio": round(best[1], 2), "kind": best[2]}
        if best else None)

    # 局部背景中位数下的强峰才算数。恒等基线上比值一般在 2~5，
    # 真实 VAE 网格能到 15 以上，所以阈值取 8 留足余量。
    grid_2d_hit = bool(best and best[1] >= 8.0 and best[0] in (4, 8, 16, 32))
    kind_cn = {"axis": "条纹型（轴对齐）", "diag": "棋盘型（对角）"}.get(
        best[2] if best else "", "?")

    if hits and grid_2d_hit:
        report["latent_verdict"] = (
            "1D 与 2D 双重命中（1D %d 处周期 %.1f~%.1f px；"
            "2D 最强 %.0fpx 比值 %.1f，%s）—— 扩散模型 VAE 解码网格，"
            "可判定为生成式"
            % (len(hits), min(h["period_px"] for h in hits),
               max(h["period_px"] for h in hits),
               best[0], best[1], kind_cn)
        )
        report["latent_confidence"] = "high"
    elif grid_2d_hit:
        report["latent_verdict"] = (
            "2D 频谱命中 %.0fpx 周期（比值 %.1f，%s，局部背景中位数）—— "
            "解码网格，提示生成式模型；"
            "1D 行/列均值未检出属正常（棋盘型会在均值上抵消）"
            % (best[0], best[1], kind_cn)
        )
        report["latent_confidence"] = "medium-high"
    elif hits:
        report["latent_verdict"] = (
            "1D 命中 %d 处周期纹理（%.1f~%.1f px）但 2D 频谱不强 "
            "—— 可能是重采样摩尔纹而非 VAE 网格，建议重复验证"
            % (len(hits), min(h["period_px"] for h in hits),
               max(h["period_px"] for h in hits))
        )
        report["latent_confidence"] = "low"
    else:
        report["latent_verdict"] = (
            "1D 与 2D 均未检出稳定网格 —— 更像判别式超分"
            "（ESRGAN 类）或传统滤波"
        )
        report["latent_confidence"] = "high" if not scores else "medium"

    # 随机性检查的提示：p8 提交两次若逐字节相同，可直接推翻扩散模型
    report["latent"]["note"] = (
        "若 p8_flat 两次提交输出逐字节相同，则基本可排除扩散模型"
    )


# --------------------------------------------------------------------------- #
# p1 重采样核 + 缩放
# --------------------------------------------------------------------------- #
def analyze_resample(img, sx, sy, report):
    """条纹扫频 -> MTF；锐利边缘 -> 上升宽度与过冲（振铃）。"""
    # --- 条纹对比度保留率 ---
    # 条纹带从 y=60 起，而标签区一直延伸到 y=130，第一条条纹正好被压住。
    # 所以只取条纹带的下半段（y=135 之后），完全避开标签。
    stripe_y0 = max(STRIPE_Y0 + 75, TAG_Y1 + 5)
    stripe_h = STRIPE_Y0 + STRIPE_H - stripe_y0

    contrast = []
    for i, period in enumerate(STRIPE_PERIODS):
        x0 = STRIPE_X0 + i * STRIPE_STEP
        patch = rect(img, x0 * sx, stripe_y0 * sy,
                     STRIPE_W * sx, stripe_h * sy)
        if patch.size == 0:
            contrast.append(0.0)
            continue
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float64)
        # 去掉整体趋势，只留条纹幅度
        contrast.append(round(float(gray.std()), 2))

    reference = contrast[-1] if contrast and contrast[-1] > 1 else None
    mtf = [round(c / reference, 3) if reference else None for c in contrast]
    report["stripe_contrast"] = dict(zip([str(p) for p in STRIPE_PERIODS], contrast))
    report["mtf_normalized"] = dict(
        zip([str(p) for p in STRIPE_PERIODS], mtf))

    # --- 边缘扩散函数 ---
    y0 = int(EDGE_Y * sy)
    band = rect(img, 0, y0, img.shape[1], max(4, int(40 * sy)))
    if band.size:
        gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY).astype(np.float64)
        profile = gray.mean(axis=0)
        # 在映射后的边缘位置附近取窗口
        cx = int(EDGE_X * sx)
        lo, hi = max(0, cx - int(30 * sx)), min(len(profile), cx + int(30 * sx))
        seg = profile[lo:hi]
        if seg.size >= 8:
            low_v = float(np.percentile(seg, 5))
            high_v = float(np.percentile(seg, 95))
            span = high_v - low_v
            if span > 10:
                norm = (seg - low_v) / span
                def cross(level):
                    idx = np.where(norm >= level)[0]
                    return int(idx[0]) if idx.size else None
                i10, i90 = cross(0.10), cross(0.90)
                width = (i90 - i10) if (i10 is not None and i90 is not None) else None
                overshoot = float(norm.max()) - 1.0
                undershoot = float(norm.min())
                report["edge"] = {
                    "rise_10_90_px": round(width / sx, 2) if width else None,
                    "overshoot": round(overshoot, 4),
                    "undershoot": round(undershoot, 4),
                }
                verdict = []
                if width is not None:
                    w_norm = width / sx
                    if w_norm <= 1.6:
                        verdict.append("边缘很锐（几乎未重采样，或用了锐化）")
                    elif w_norm <= 3.0:
                        verdict.append("边缘中等（Lanczos / 双三次量级）")
                    else:
                        verdict.append("边缘明显软化（双线性或强降噪）")
                if overshoot > 0.03:
                    verdict.append("有上冲 -> 核带负瓣（Lanczos / 锐化）")
                elif overshoot > -0.01:
                    verdict.append("无上冲 -> 平滑核（双三次 / 双线性）")
                report["edge_verdict"] = verdict

    # --- 缩放倍率 ---
    report["resample_scale"] = {
        "output_vs_probe": round(sx, 4),
        "output_vs_1536": round(max(img.shape[:2]) / float(UPSTREAM_LONG_SIDE), 3),
    }


# --------------------------------------------------------------------------- #
# p3 高频存活
# --------------------------------------------------------------------------- #
def analyze_hifreq(img, sx, sy, report):
    """周期 1 / 2 / 4 px 的棋盘，看谁活下来。"""
    out = {}
    for period, (x0, y0, x1, y1) in zip((1, 2, 4), CHECKER_RECTS):
        patch = rect(img, x0 * sx, y0 * sy, (x1 - x0) * sx, (y1 - y0) * sy)
        if patch.size == 0:
            continue
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float64)
        out[str(period)] = {
            "std": round(float(gray.std()), 2),
            "range": round(float(gray.max() - gray.min()), 1),
            "mean": round(float(gray.mean()), 1),
        }
    report["checker"] = out
    if "1" in out and "4" in out and out["4"]["std"] > 1:
        report["hifreq_retention_1px"] = round(out["1"]["std"] / out["4"]["std"], 3)
    verdict = []
    for period in ("1", "2", "4"):
        if period in out:
            v = out[period]["std"]
            verdict.append("%spx %s" % (period, "存活" if v > 25 else
                                         ("衰减" if v > 6 else "丢失")))
    report["hifreq_verdict"] = verdict


# --------------------------------------------------------------------------- #
# p8 降噪强度
# --------------------------------------------------------------------------- #
def analyze_flat(img, sx, sy, report):
    """已知 sigma=6 的噪声底，测输出残留噪声。"""
    h = img.shape[0]
    band_h = h // len(FLAT_LEVELS)
    out = {}
    for i, level in enumerate(FLAT_LEVELS):
        y0 = i * band_h + int(band_h * 0.30)
        y1 = i * band_h + int(band_h * 0.70)
        # 第一条灰带横跨标签区，横向从 30% 起采样即可完全避开
        x0 = max(int(img.shape[1] * 0.10), int(TAG_X1 * sx) + 20)
        x1 = int(img.shape[1] * 0.90)
        patch = img[y0:y1, x0:x1]
        if patch.size == 0:
            continue
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float64)
        out[str(level)] = {
            "sigma": round(float(gray.std()), 3),
            "mean": round(float(gray.mean()), 1),
        }
    report["noise"] = out
    sigmas = [v["sigma"] for v in out.values()]
    if sigmas:
        avg = sum(sigmas) / len(sigmas)
        report["noise_sigma_in"] = FLAT_NOISE_SIGMA
        report["noise_sigma_out_avg"] = round(avg, 3)
        report["noise_ratio"] = round(avg / FLAT_NOISE_SIGMA, 3)
        if avg / FLAT_NOISE_SIGMA < 0.35:
            report["denoise_verdict"] = "强降噪（噪声被压掉 65% 以上）"
        elif avg / FLAT_NOISE_SIGMA < 0.75:
            report["denoise_verdict"] = "中度降噪"
        elif avg / FLAT_NOISE_SIGMA < 1.3:
            report["denoise_verdict"] = "几乎未降噪（噪声基本原样）"
        else:
            report["denoise_verdict"] = "噪声反而增强（锐化放大了噪声）"


# --------------------------------------------------------------------------- #
# p5 / p6 形态学（辅助）
# --------------------------------------------------------------------------- #
def analyze_text(img, sx, sy, report):
    """文字区：测笔画对比度与边缘锐度。糊掉/重画 = 生成式。"""
    patch = rect(img, 140 * sx, 200 * sy, 900 * sx, 800 * sy)
    if patch.size == 0:
        return
    gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float64)
    dark = float(np.percentile(gray, 2))
    light = float(np.percentile(gray, 98))
    report["text"] = {
        "contrast": round(light - dark, 1),
        "std": round(float(gray.std()), 2),
        "min": round(float(gray.min()), 1),
        "max": round(float(gray.max()), 1),
    }
    if light - dark < 120:
        report["text_verdict"] = "文字对比度大幅下降 —— 可能被重采样糊掉或被模型改写"
    else:
        report["text_verdict"] = "文字对比度保留 —— 未发生内容级重画"


def analyze_texture(img, sx, sy, report):
    """纹理区：多尺度能量分布，看细节是被保留还是被重画。"""
    core = img[int(img.shape[0] * 0.15):int(img.shape[0] * 0.85),
               int(img.shape[1] * 0.15):int(img.shape[1] * 0.85)]
    if core.size == 0:
        return
    gray = cv2.cvtColor(core, cv2.COLOR_BGR2GRAY).astype(np.float32)
    energy = {}
    for sigma in (1.0, 2.0, 4.0, 8.0):
        blur = cv2.GaussianBlur(gray, (0, 0), sigma)
        detail = gray - blur
        energy[str(sigma)] = round(float(detail.std()), 3)
    report["texture_energy"] = energy
    if "1.0" in energy and "8.0" in energy and energy["8.0"] > 1e-6:
        report["texture_fine_coarse_ratio"] = round(
            energy["1.0"] / energy["8.0"], 3)


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
HANDLERS = {
    "p1": analyze_resample,
    "p2": analyze_tone,
    "p3": analyze_hifreq,
    "p4": analyze_latent,
    "p5": analyze_text,
    "p6": analyze_texture,
    "p8": analyze_flat,
}


def analyze_file(path):
    kind = probe_kind(path)
    report = {
        "file": os.path.basename(path),
        "probe": kind,
        "meta": file_meta(path),
    }
    try:
        img = load(path)
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
        return report

    report.update(grader(path, img))
    sx = report["scale_x"]
    sy = report["scale_y"]

    if kind is None:
        report["note"] = "文件名无法识别探针类型（应以 p1..p8 开头）"
        return report

    handler = HANDLERS.get(kind)
    if handler is None:
        report["note"] = "p7（元数据）已在 meta 字段中报告" if kind == "p7" \
            else "该探针无需像素分析"
        return report

    try:
        handler(img, sx, sy, report)
    except Exception as exc:  # noqa: BLE001
        report["analysis_error"] = "%s: %s" % (type(exc).__name__, exc)
    return report


def print_report(rep):
    line = "=" * 74
    print(line)
    print("### %s   探针=%s" % (rep["file"], rep.get("probe") or "?"))
    if rep.get("error"):
        print("    读取失败：" + rep["error"])
        return

    m = rep.get("meta", {})
    print("    格式=%s  体积=%d  尺寸=%dx%d"
          % (m.get("format"), m.get("bytes", 0),
             rep.get("out_w", 0), rep.get("out_h", 0)))
    print("    相对探针版面缩放  x=%.4f  y=%.4f"
          % (rep.get("scale_x", 0), rep.get("scale_y", 0)))

    if rep.get("probe") == "p7":
        print("    --- 元数据存活 ---")
        print("    PNG 文本块: %s" % (rep["meta"].get("png_text") or "（无）"))
        exif = rep["meta"].get("exif") or {}
        print("    EXIF 字段数: %d" % len(exif))
        for k, v in list(exif.items())[:12]:
            print("      %-8s = %s" % (k, v))
        if not exif and not rep["meta"].get("png_text"):
            print("    -> 元数据被完全剥离（上传后被重编码过）")

    if "tone_fit" in rep:
        print("    --- 色调 ---")
        print("    取样: %s" % rep["tone_points"])
        fit = rep["tone_fit"]
        print("    拟合: y = %.4f x + %.2f" % (fit["slope"], fit["intercept"]))
        print("    判定: %s" % "；".join(rep.get("tone_verdict", [])))
        if "white_balance" in rep:
            wb = rep["white_balance"]
            print("    白平衡增益 R=%.4f G=%.4f B=%.4f"
                  % (wb["gain_r"], wb["gain_g"], wb["gain_b"]))
            print("    -> %s" % rep.get("wb_verdict", ""))
        if "saturation_red" in rep:
            print("    饱和度 红=%.3f 蓝=%.3f"
                  % (rep["saturation_red"], rep.get("saturation_blue", 0)))

    if "latent_verdict" in rep:
        print("    --- 生成式指纹 ---")
        lat = rep["latent"]
        print("    横向强周期: %s" % (lat["col_peaks"] or "无"))
        print("    纵向强周期: %s" % (lat["row_peaks"] or "无"))
        for h in lat.get("grid_hits", []):
            print("      命中 %s 周期 %.2f px  强度 %.3f"
                  % (h["axis"], h["period_px"], h["amp"]))
        if lat.get("grid_2d_ratio"):
            print("    2D 各周期比值: %s" % lat["grid_2d_ratio"])
        gb = lat.get("grid_2d_best")
        if gb:
            kind_cn = {"axis": "条纹型", "diag": "棋盘型"}.get(
                gb.get("kind"), "?")
            print("    2D 最强: %spx 比值 %.2f（%s，阈值 8.0）"
                  % (gb["period_px"], gb["ratio"], kind_cn))
        print("    -> %s" % rep["latent_verdict"])

    if "edge" in rep or "mtf_normalized" in rep:
        print("    --- 重采样 ---")
        e = rep.get("edge")
        if e:
            print("    边缘 10-90%% 上升 = %s px（归一化到探针坐标）"
                  % e.get("rise_10_90_px"))
            print("    上冲 = %s   下冲 = %s" % (e.get("overshoot"), e.get("undershoot")))
        mtf = rep.get("mtf_normalized") or {}
        if mtf:
            print("    MTF（相对 32px）: %s"
                  % "  ".join("%s:%s" % (k, v) for k, v in mtf.items()))
        if rep.get("edge_verdict"):
            print("    -> %s" % "；".join(rep["edge_verdict"]))
        s = rep.get("resample_scale") or {}
        if s:
            print("    输出相对 1536 长边 = %.3fx" % s.get("output_vs_1536", 0))

    if "checker" in rep:
        print("    --- 高频存活 ---")
        for period, v in rep["checker"].items():
            print("    周期 %spx  std=%s  range=%s"
                  % (period, v["std"], v["range"]))
        if "hifreq_retention_1px" in rep:
            print("    1px 存活率（相对 4px）= %.3f" % rep["hifreq_retention_1px"])
        print("    -> %s" % "；".join(rep.get("hifreq_verdict", [])))

    if "noise_ratio" in rep:
        print("    --- 降噪 ---")
        for level, v in rep["noise"].items():
            print("    灰阶 %-4s  输入σ=%.1f  输出σ=%.3f"
                  % (level, FLAT_NOISE_SIGMA, v["sigma"]))
        print("    平均残留比 = %.3f" % rep["noise_ratio"])
        print("    -> %s" % rep.get("denoise_verdict", ""))

    if "text" in rep:
        print("    --- 文字 ---")
        print("    %s" % rep["text"])
        print("    -> %s" % rep.get("text_verdict", ""))

    if "texture_energy" in rep:
        print("    --- 纹理 ---")
        print("    多尺度能量: %s" % rep["texture_energy"])
        if "texture_fine_coarse_ratio" in rep:
            print("    细/粗比 = %.3f（越高细节保留越好）"
                  % rep["texture_fine_coarse_ratio"])

    if rep.get("note"):
        print("    注: %s" % rep["note"])
    if rep.get("analysis_error"):
        print("    分析出错: %s" % rep["analysis_error"])


def main():
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                      errors="replace")

    ap = argparse.ArgumentParser(description="探针结果分析器")
    ap.add_argument("paths", nargs="*", help="返回图路径")
    ap.add_argument("--dir", help="分析整个目录")
    ap.add_argument("--json", help="把结果写入 JSON")
    args = ap.parse_args()

    targets = list(args.paths)
    if args.dir:
        for ext in ("png", "jpg", "jpeg", "webp", "bmp"):
            targets += glob.glob(os.path.join(args.dir, "*." + ext))
            targets += glob.glob(os.path.join(args.dir, "*." + ext.upper()))
    targets = sorted(set(targets))

    if not targets:
        print("没有输入。用法： python analyze.py --dir result")
        print("或：   python analyze.py result/p1_resample.png")
        return 1

    reports = []
    print("分析 %d 个文件" % len(targets))
    for path in targets:
        rep = analyze_file(path)
        reports.append(rep)
        print_report(rep)

    print("=" * 74)
    print("### 汇总")
    for rep in reports:
        kind = rep.get("probe") or "?"
        verdict = (rep.get("latent_verdict") or rep.get("denoise_verdict")
                   or rep.get("text_verdict") or "")
        if not verdict and rep.get("tone_verdict"):
            verdict = "；".join(rep["tone_verdict"])
        if not verdict and rep.get("hifreq_verdict"):
            verdict = "；".join(rep["hifreq_verdict"])
        if not verdict and rep.get("texture_fine_coarse_ratio") is not None:
            verdict = "细/粗比 = %.3f" % rep["texture_fine_coarse_ratio"]
        if not verdict and rep.get("edge_verdict"):
            verdict = "；".join(rep["edge_verdict"])
        print("  %-6s %-24s %s" % (kind, rep["file"], verdict[:70]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(reports, fh, ensure_ascii=False, indent=1)
        print("\n完整结果已写入 %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
