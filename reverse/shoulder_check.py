# -*- coding: utf-8 -*-
"""验证:九命机结果里肩膀/衣服褶皱是否被生成式重绘(轮廓位移)。"""
import numpy as np
from PIL import Image

W, H = 1536, 864
def load(p):
    return np.asarray(Image.open(p).convert("RGB").resize((W, H), Image.LANCZOS), float)

A = load(r"C:\Users\lenovo\OneDrive - Jazee\桌面\QQ图片20260927174648.jpg")
B = load(r"C:\Users\lenovo\OneDrive - Jazee\桌面\3.jpg")
C = load(r"C:\Users\lenovo\OneDrive - Jazee\桌面\13435045936832630.jpeg")

# 肩线区域与衣服褶皱区域裁片: A/B/C 三连
def stack(name, box, scale=2):
    x0, y0, x1, y1 = box
    w, h = x1-x0, y1-y0
    cv = Image.new("RGB", (w*scale, (h*3+16)*scale), (255, 0, 0))
    for k, arr in enumerate([A, B, C]):
        im = Image.fromarray(arr[y0:y1, x0:x1].astype(np.uint8)).resize((w*scale, h*scale), Image.LANCZOS)
        cv.paste(im, (0, k*(h+8)*scale))
    cv.save(rf"F:\Project\image\reverse\cmp3_{name}.png")

stack("shoulder", (480, 30, 1130, 330))   # 肩线与上胸
stack("folds", (560, 250, 1060, 640))     # 衣服褶皱与手机

# 量化:躯干区边缘图重合度。梯度幅值图的相关性对"轮廓位移"极敏感
def edge(g):
    gx = np.gradient(g, axis=1); gy = np.gradient(g, axis=0)
    return np.sqrt(gx**2 + gy**2)

region = (slice(30, 640), slice(480, 1130))  # 整个人物躯干
for name, X in [("自己B", B), ("九命机C", C)]:
    ea, ex = edge(A[region].mean(2)), edge(X[region].mean(2))
    ea -= ea.mean(); ex -= ex.mean()
    cc = (ea*ex).sum()/(np.sqrt((ea**2).sum()*(ex**2).sum())+1e-6)
    print(f"A->{name} 躯干区边缘结构相关: {cc:.4f}")
ea, ec = edge(B[region].mean(2)), edge(C[region].mean(2))
ea -= ea.mean(); ec -= ec.mean()
print(f"B vs C 躯干区边缘结构相关: {(ea*ec).sum()/(np.sqrt((ea**2).sum()*(ec**2).sum())+1e-6):.4f}")
print("对比图已存 cmp3_shoulder.png / cmp3_folds.png (顺序: 上=原图 中=自己 下=九命机)")
