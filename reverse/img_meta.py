import sys
from PIL import Image
from PIL.ExifTags import TAGS

def info(path, tag):
    im = Image.open(path)
    print(f"===== {tag} =====")
    print("format:", im.format, im.format_description)
    print("size:", im.size, "mode:", im.mode)
    print("info keys:", {k: (v if k not in ('exif','icc_profile','xmp') else f"<{len(v)} bytes>") for k,v in im.info.items()})
    # quantization tables -> JPEG quality fingerprint
    if hasattr(im, "quantization"):
        q = im.quantization
        print("quant tables:", {i: list(v[:8]) + ['...'] for i,v in q.items()})
    # EXIF
    try:
        ex = im.getexif()
        for tid, val in ex.items():
            name = TAGS.get(tid, tid)
            print(f"EXIF {name}({tid}): {str(val)[:120]}")
        # XMP raw
        if 'xmp' in im.info:
            print("XMP:", im.info['xmp'][:600])
    except Exception as e:
        print("exif err:", e)
    print()

info(r"C:\Users\lenovo\OneDrive - Jazee\桌面\20260926_102958.jpg", "ORIGINAL")
info(r"C:\Users\lenovo\OneDrive - Jazee\桌面\13434979729187826.jpeg", "RESULT")
