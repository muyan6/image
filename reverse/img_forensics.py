import numpy as np
from PIL import Image

orig = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\20260926_102958.jpg").convert("RGB")
res  = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\13434979729187826.jpeg").convert("RGB")
W,H = 1536,864
orig_s = orig.resize((W,H), Image.LANCZOS)
a = np.asarray(orig_s, float); b = np.asarray(res, float)

# 1) 全局相位相关求偏移
ga = a.mean(2); gb = b.mean(2)
fa = np.fft.rfft2(ga-ga.mean()); fb = np.fft.rfft2(gb-gb.mean())
corr = np.fft.irfft2(fa*np.conj(fb))
dy,dx = np.unravel_index(np.argmax(corr), corr.shape)
dy = dy if dy < H//2 else dy-H; dx = dx if dx < W//2 else dx-W
print(f"全局配准偏移: dx={dx}, dy={dy}  (0,0=完全对齐)")
peak = corr.max()/ (np.abs(corr).mean()*corr.size) if np.abs(corr).mean()>0 else 0

# 归一化相关系数
ga2 = ga-ga.mean(); gb2 = gb-gb.mean()
cc = (ga2*gb2).sum()/np.sqrt((ga2**2).sum()*(gb2**2).sum())
print(f"全图相关系数: {cc:.4f}")

# 2) 指标
def lap_var(g): 
    return (((np.roll(g,1,0)+np.roll(g,-1,0)+np.roll(g,1,1)+np.roll(g,-1,1))-4*g)**2).mean()
def sat(img):
    mx=img.max(2); mn=img.min(2); return ((mx-mn)/(mx+1e-6)).mean()
def noise_est(g):  # 中频高频残差估计噪声
    import scipy.ndimage as nd
    return None
print(f"拉普拉斯能量(清晰度) 原图缩放后: {lap_var(ga):.1f}  修复图: {lap_var(gb):.1f}")
print(f"平均饱和度 原图: {sat(a):.3f}  修复图: {sat(b):.3f}")
print(f"亮度均值/标准差 原图: {ga.mean():.1f}/{ga.std():.1f}  修复图: {gb.mean():.1f}/{gb.std():.1f}")

# 3) 分块差异热力图: 找出被改动最大的区域
diff = np.abs(a-b).mean(2)
from PIL import ImageFilter
bs = 96
rows, cols = H//bs, W//bs
dm = np.zeros((rows,cols))
for i in range(rows):
    for j in range(cols):
        blk = diff[i*bs:(i+1)*bs, j*bs:(j+1)*bs]
        # 局部相关,更能反映"重绘"而非单纯亮度偏移
        pa = a[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
        pb = b[i*bs:(i+1)*bs, j*bs:(j+1)*bs].mean(2)
        pa-=pa.mean(); pb-=pb.mean()
        den = np.sqrt((pa**2).sum()*(pb**2).sum())+1e-6
        dm[i,j] = (pa*pb).sum()/den
print("\n分块局部相关系数热图 (1.0=像素级保留, 低=被重绘):")
print("      " + "".join(f"{c:>6}" for c in range(cols)))
for i in range(rows):
    print(f"row{i:2}: " + "".join(f"{dm[i,j]:6.3f}" for j in range(cols)))

# 4) 保存对比 crops
def save_pair(name, box):
    x0,y0,x1,y1 = box
    ca = orig_s.crop(box); cb = res.crop(box)
    w,h = ca.size
    canvas = Image.new("RGB",(w, h*2+8),(255,0,0))
    canvas.paste(ca,(0,0)); canvas.paste(cb,(0,h+8))
    canvas = canvas.resize((w*2, (h*2+8)*2), Image.LANCZOS)
    canvas.save(rf"F:\Project\image\reverse\cmp_{name}.png")

save_pair("topright", (1150,0,1536,300))     # DI 招牌反射
save_pair("phone", (620,180,1000,420))       # 手机
save_pair("mirror_dust", (100,300,600,600))  # 镜面灰尘区
save_pair("arm", (380,500,760,760))          # 手臂皮肤
save_pair("bg_left", (0,550,400,864))        # 背景哑铃
print("\n已保存对比图到 F:\Project\image\reverse\cmp_*.png")
