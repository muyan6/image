# -*- coding: utf-8 -*-
import numpy as np
from PIL import Image

A_PATH = r"C:\Users\lenovo\OneDrive - Jazee\桌面\QQ图片20260927174648.jpg"   # 原始原图(4032px)
B_PATH = r"C:\Users\lenovo\OneDrive - Jazee\桌面\3.jpg"                  # 自己的结果(v1提示词)
C_PATH = r"C:\Users\lenovo\OneDrive - Jazee\桌面\13435045936832630.jpeg" # 九命机结果

W, H = 1536, 864
def load(p):
    im = Image.open(p).convert("RGB")
    return im, np.asarray(im.resize((W, H), Image.LANCZOS), float)

imA, A = load(A_PATH); imB, B = load(B_PATH); imC, C = load(C_PATH)
print(f"尺寸: 原图{imA.size} 自己{imB.size} 九命机{imC.size}")

def sat(img):
    mx, mn = img.max(2), img.min(2); return ((mx-mn)/(mx+1e-6)).mean()
def lap(g):
    return (((np.roll(g,1,0)+np.roll(g,-1,0)+np.roll(g,1,1)+np.roll(g,-1,1))-4*g)**2).mean()

def align(a, b):
    ga, gb = a.mean(2), b.mean(2)
    fa = np.fft.rfft2(ga-ga.mean()); fb = np.fft.rfft2(gb-gb.mean())
    c = np.fft.irfft2(fa*np.conj(fb))
    dy, dx = np.unravel_index(np.argmax(c), c.shape)
    dy = dy if dy < H//2 else dy-H; dx = dx if dx < W//2 else dx-W
    ga2, gb2 = ga-ga.mean(), gb-gb.mean()
    cc = (ga2*gb2).sum()/np.sqrt((ga2**2).sum()*(gb2**2).sum())
    return dx, dy, cc

def blockmap(a, b):
    bs = 96; rows, cols = H//bs, W//bs
    dm = np.zeros((rows, cols))
    for i in range(rows):
        for j in range(cols):
            pa = a[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
            pb = b[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
            pa -= pa.mean(); pb -= pb.mean()
            dm[i,j] = (pa*pb).sum()/(np.sqrt((pa**2).sum()*(pb**2).sum())+1e-6)
    return dm

print("\n===== 指标对比 =====")
print(f"{'':14}{'饱和度':>8}{'清晰度':>10}{'亮度':>8}")
print(f"{'原图A':14}{sat(A):8.3f}{lap(A.mean(2)):10.0f}{A.mean():8.1f}")
print(f"{'自己B':14}{sat(B):8.3f}{lap(B.mean(2)):10.0f}{B.mean():8.1f}")
print(f"{'九命机C':14}{sat(C):8.3f}{lap(C.mean(2)):10.0f}{C.mean():8.1f}")

for name, X in [("自己B", B), ("九命机C", C)]:
    dx, dy, cc = align(A, X)
    print(f"\nA->{name}: 偏移(dx={dx},dy={dy}) 结构相关={cc:.4f} 饱和度{(sat(X)/sat(A)-1)*100:+.0f}%")

dx, dy, cc = align(B, C)
print(f"\nB vs C(两个结果互相像吗): 偏移({dx},{dy}) 相关={cc:.4f}")

print("\n===== A->自己B 分块相关 =====")
dmB = blockmap(A, B)
print("      " + "".join(f"{c:>6}" for c in range(dmB.shape[1])))
for i in range(dmB.shape[0]):
    print(f"row{i:2}:" + "".join(f"{v:6.2f}" for v in dmB[i]))
print("中位数: %.3f" % np.median(dmB))
print("\n===== A->九命机C 分块相关 =====")
dmC = blockmap(A, C)
print("      " + "".join(f"{c:>6}" for c in range(dmC.shape[1])))
for i in range(dmC.shape[0]):
    print(f"row{i:2}:" + "".join(f"{v:6.2f}" for v in dmC[i]))
print("中位数: %.3f" % np.median(dmC))

# 三连对比图: 原图 | 自己 | 九命机
canvas = Image.new("RGB", (W, H*3+16), (255,0,0))
canvas.paste(imA.resize((W,H), Image.LANCZOS), (0,0))
canvas.paste(imB.resize((W,H), Image.LANCZOS), (0,H+8))
canvas.paste(imC.resize((W,H), Image.LANCZOS), (0,2*H+16))
canvas.save(r"F:\Project\image\reverse\three_way.png")
print("\n三连图已存 three_way.png")
