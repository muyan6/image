import numpy as np
from PIL import Image
orig = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\20260926_102958.jpg").convert("RGB")
res  = Image.open(r"C:\Users\lenovo\OneDrive - Jazee\桌面\13434979729187826.jpeg").convert("RGB")
orig_s = orig.resize((1536,864), Image.LANCZOS)
for name, box in [("fist",(790,620,1050,864)), ("bracelet",(580,530,900,760)), ("phone",(620,180,1000,420))]:
    ca = orig_s.crop(box); cb = res.crop(box)
    w,h = ca.size
    canvas = Image.new("RGB",(w*2+8, h),(255,0,0))
    canvas.paste(ca,(0,0)); canvas.paste(cb,(w+8,0))
    canvas = canvas.resize(((w*2+8)*2, h*2), Image.LANCZOS)
    canvas.save(rf"F:\Project\image\reverse\cmp2_{name}.png")
print("saved")
