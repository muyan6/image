# -*- coding: utf-8 -*-
import numpy as np
from PIL import Image

orig = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\20260119_173331.jpg").convert("RGB")
res  = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\2.jpg").convert("RGB")
print("原图尺寸:", orig.size, "| 结果尺寸:", res.size)
W, H = 1536, 864
o = np.asarray(orig.resize((W, H), Image.LANCZOS), float)
r = np.asarray(res.resize((W, H), Image.LANCZOS) if res.size != (W, H) else res, float)

ga, gb = o.mean(2), r.mean(2)
fa = np.fft.rfft2(ga - ga.mean()); fb = np.fft.rfft2(gb - gb.mean())
corr = np.fft.irfft2(fa * np.conj(fb))
dy, dx = np.unravel_index(np.argmax(corr), corr.shape)
dy = dy if dy < H // 2 else dy - H; dx = dx if dx < W // 2 else dx - W
ga2, gb2 = ga - ga.mean(), gb - gb.mean()
cc = (ga2 * gb2).sum() / np.sqrt((ga2 ** 2).sum() * (gb2 ** 2).sum())
print(f"配准偏移: dx={dx}, dy={dy} | 全图相关: {cc:.4f}")

def sat(img):
    mx, mn = img.max(2), img.min(2)
    return ((mx - mn) / (mx + 1e-6)).mean()
def lap(g):
    return (((np.roll(g,1,0)+np.roll(g,-1,0)+np.roll(g,1,1)+np.roll(g,-1,1))-4*g)**2).mean()
# 天空区域取右上角 300x200
sky_o = o[50:250, W-350:W-50]; sky_r = r[50:250, W-350:W-50]
print(f"平均饱和度: 原图 {sat(o):.3f} -> 结果 {sat(r):.3f} ({(sat(r)/sat(o)-1)*100:+.0f}%)")
print(f"天空平均B通道(越蓝越高): {sky_o[:,:,2].mean():.0f} -> {sky_r[:,:,2].mean():.0f}")
print(f"清晰度(拉普拉斯能量): {lap(ga):.1f} -> {lap(gb):.1f}")

bs = 96
rows, cols = H // bs, W // bs
print("\n分块相关热图(1.0=结构原样保留, 低=被重绘):")
print("      " + "".join(f"{c:>6}" for c in range(cols)))
for i in range(rows):
    line = ""
    for j in range(cols):
        pa = o[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
        pb = r[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
        pa -= pa.mean(); pb -= pb.mean()
        den = np.sqrt((pa**2).sum() * (pb**2).sum()) + 1e-6
        line += f"{(pa*pb).sum()/den:6.3f}"
    print(f"row{i:2}:{line}")

def save_pair(name, box):
    x0, y0, x1, y1 = box
    ca = orig.resize((W, H), Image.LANCZOS).crop(box)
    cb = res.crop(box)
    w, h = ca.size
    canvas = Image.new("RGB", (w, h*2+8), (255, 0, 0))
    canvas.paste(ca, (0, 0)); canvas.paste(cb, (0, h+8))
    canvas.save(rf"F:\Project\image\reverse\leaf_{name}.png")
save_pair("leaves", (100, 300, 700, 700))    # 左侧树叶区
save_pair("trunk", (900, 500, 1400, 864))    # 主干与人物
print("\n对比图已存 leaf_leaves.png / leaf_trunk.png")
