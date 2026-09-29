# -*- coding: utf-8 -*-
"""
2D FFT 网格检测器 —— 三组控制验证
================================

一个检测器有两种失效方式：
  负控失效：对普通图像也报警（假阳性）
  正控失效：对真网格不报警（假阴性）

所以必须同时跑三个组：
  负控 = 恒等基线（纯平滑渐变）+ 纯随机噪声   -> 必须「未命中」
  正控 = 人工注入的 8px / 16px 棋盘           -> 必须「命中」
  正控 = 人工注入的 8px / 16px 纵向条纹       -> 必须「命中」

棋盘型网格在行/列均值上会相互抵消（对固定 y 沿 x 求和，一半 +1
一半 -1），1D 方法看不到它 —— 验证 2D FFT 补上了这个盲区。
条纹型网格反过来：它的能量在轴对齐频率上，只搜对角方向会漏掉它 ——
验证 2D 检测器两个方向都覆盖。
"""
import io
import sys

import numpy as np

sys.path.insert(0, r"F:\Project\image\probes")
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import analyze as A  # noqa: E402

RNG = np.random.default_rng(7)
H, W = 1000, 1400


def smooth():
    """与 p4_latent 同构：纯平滑渐变 + 极低幅度非周期噪声。"""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    img = 128 + 55 * np.sin(xx / 700.0) + 38 * np.cos(yy / 600.0)
    img += RNG.normal(0, 1.2, (H, W))
    return np.clip(img, 0, 255).astype(np.uint8)


def add_checker(gray, period=8, amp=3.0):
    """模拟 VAE 解码网格：棋盘型。"""
    yy, xx = np.indices(gray.shape)
    cb = (((yy // period) + (xx // period)) % 2).astype(np.float32) * 2 - 1
    return np.clip(gray.astype(np.float32) + amp * cb, 0, 255).astype(np.uint8)


def add_stripes(gray, period=8, amp=3.0):
    """条纹型网格：只在 x 方向变化。1D 列均值应能看到。"""
    yy, xx = np.indices(gray.shape)
    st = (((xx // period) % 2).astype(np.float32) * 2 - 1)
    return np.clip(gray.astype(np.float32) + amp * st, 0, 255).astype(np.uint8)


def main():
    base = smooth()
    cases = [
        ("负控  纯平滑渐变（恒等基线）", base, False),
        ("负控  +随机噪声 σ=3（无周期）",
         np.clip(base.astype(np.float32) + RNG.normal(0, 3, base.shape),
                 0, 255).astype(np.uint8), False),
        ("正控  +8px 棋盘 幅度1.0", add_checker(base, 8, 1.0), True),
        ("正控  +8px 棋盘 幅度2.0", add_checker(base, 8, 2.0), True),
        ("正控  +8px 棋盘 幅度3.0", add_checker(base, 8, 3.0), True),
        ("正控  +16px 棋盘 幅度3.0", add_checker(base, 16, 3.0), True),
        ("正控  +8px 纵向条纹 幅度1.0", add_stripes(base, 8, 1.0), True),
        ("正控  +8px 纵向条纹 幅度3.0", add_stripes(base, 8, 3.0), True),
        ("正控  +16px 纵向条纹 幅度3.0",
         add_stripes(base, 16, 3.0), True),
    ]

    print("=" * 78)
    print("2D FFT 网格检测器 —— 三组控制验证")
    print("=" * 78)
    print("阈值：局部背景中位数比值 >= 8.0")

    ok = True
    for name, img, should_hit in cases:
        core = img[int(H * 0.25):int(H * 0.85),
                   int(W * 0.15):int(W * 0.90)].astype(np.float64)
        scores, best = A.grid_2d_score(core)
        hit = bool(best and best[1] >= 8.0)
        good = (hit == should_hit)
        if not good:
            ok = False

        print("\n%-34s 预期=%-6s 实际=%-6s %s"
              % (name, "命中" if should_hit else "未命中",
                 "命中" if hit else "未命中",
                 "OK" if good else "<<< 不符"))
        print("    各周期比值: %s" % scores)
        if best:
            print("    最强: %dpx  比值 %.2f  方向=%s" % best)

        # 顺带看 1D 会不会漏掉棋盘
        col = core.mean(axis=0)
        peaks = A.periodic_peaks(col)
        near8 = [p for p in peaks if 6.0 <= p[0] <= 10.0]
        print("    1D 列均值在 6~10px 的峰: %s" % (near8 or "无"))

    print("\n" + "=" * 78)
    print("RESULT: " + ("三组控制全部通过 —— 负控不误报，棋盘/条纹正控都不遗漏"
                        if ok else "有控制组不符，阈值需调整"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())