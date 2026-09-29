import numpy as np
from PIL import Image

orig = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\20260926_102958.jpg").convert("L")
res  = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\13434979729187826.jpeg").convert("L")
W,H = 1536,864
a = np.asarray(orig.resize((W,H), Image.LANCZOS), float)/255
b = np.asarray(res, float)/255

def radial_spec(g):
    f = np.abs(np.fft.rfft2(g*np.hanning(H)[:,None]*np.hanning(W)[None,:]))
    fy = np.fft.fftfreq(H)[:f.shape[0]]
    fx = np.fft.rfftfreq(W)
    bins = np.linspace(0, 0.5, 60)
    out = []
    for i in range(len(bins)-1):
        m = (fx[None,:]>=bins[i])&(fx[None,:]<bins[i+1])
        m2 = (fy[:,None]>=bins[i])&(fy[:,None]<bins[i+1])
        sel = m & m2
        if sel.any(): out.append((bins[i], np.log(f[sel].mean()+1e-9)))
    return np.array(out)

sa, sb = radial_spec(a), radial_spec(b)
print("频率   原图(缩放后)  修复图   差值")
for (f0,va),(f1,vb) in zip(sa,sb):
    print(f"{f0:.3f}   {va:7.3f}    {vb:7.3f}   {vb-va:+6.3f}")

# 高频能量占比 (Nyquist 0.25-0.5)
def hf(g):
    f = np.abs(np.fft.rfft2(g))**2
    fy = np.fft.fftfreq(H)[:f.shape[0]]; fx = np.fft.rfftfreq(W)
    tot = f.sum()
    m = (np.abs(fy)[:,None]>0.25)&(fx[None,:]>0.25)
    return f[m].sum()/tot
print(f"\n高频能量占比(>0.25Nyq): 原图 {hf(a):.4f}  修复图 {hf(b):.4f}")
print(f"比值 修复/原图: {hf(b)/hf(a):.3f}  (若从半分辨率放大, 该比值会骤降至<0.1)")
