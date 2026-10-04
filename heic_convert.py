"""HEIC/HEIF → JPEG 转换器：保留 EXIF(含拍摄时间)与 mtime
输出: converted/<原名>.jpg
用法: python heic_convert.py [--limit N]
"""
import argparse
import os
import sys
import time
from pathlib import Path

from PIL import Image
import pillow_heif

pillow_heif.register_heif_opener()

BASE = Path(__file__).parent
PHOTOS = BASE / "photos"
OUT = BASE / "converted"
LOG = BASE / "convert.log"

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

KEEP_TAGS = {
    "0th": {271, 272, 274, 282, 283, 296, 306},           # Make Model Orientation X/YRes DateTime
    "Exif": {33434, 33437, 34850, 34855, 36867, 36868, 40962, 41989},  # 曝光 光圈 ISO 快门 DateTimeOriginal/Digitized 等
    "GPS": set(range(0, 32)),
}

def trim_exif(raw: bytes) -> bytes:
    import piexif
    try:
        ex = piexif.load(raw)
        out = {}
        for ifd in ("0th", "Exif", "GPS"):
            keep = KEEP_TAGS.get(ifd, set())
            out[ifd] = {k: v for k, v in (ex.get(ifd) or {}).items() if k in keep}
        out["1st"] = {}
        out["thumbnail"] = None
        return piexif.dump(out)
    except Exception:
        return b""

def convert_one(src: Path):
    dst = OUT / (src.stem + ".jpg")
    if dst.exists() and dst.stat().st_size > 0:
        return "skip"
    img = Image.open(src)
    raw_exif = img.info.get("exif") or b""
    icc = img.info.get("icc_profile")
    rgb = img.convert("RGB")
    tmp = dst.with_suffix(".jpg.part")
    save_kwargs = {"quality": 95}
    if icc:
        save_kwargs["icc_profile"] = icc
    try:
        if raw_exif:
            save_kwargs["exif"] = raw_exif
        rgb.save(tmp, "JPEG", **save_kwargs)
    except Exception:
        trimmed = trim_exif(raw_exif) if raw_exif else b""
        save_kwargs.pop("exif", None)
        rgb.save(tmp, "JPEG", **save_kwargs)
        if trimmed:
            import piexif
            piexif.insert(trimmed, str(tmp))
    tmp.rename(dst)
    st = src.stat()
    os.utime(dst, (st.st_mtime, st.st_mtime))
    return "ok"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    srcs = sorted([p for p in PHOTOS.iterdir() if p.suffix.lower() in (".heic", ".heif")])
    if args.limit:
        srcs = srcs[: args.limit]
    log(f"待转换 {len(srcs)} 个 HEIC/HEIF")
    ok = skip = fail = 0
    for i, s in enumerate(srcs, 1):
        try:
            r = convert_one(s)
            if r == "ok":
                ok += 1
            else:
                skip += 1
        except Exception as e:
            fail += 1
            log(f"✗ {s.name}: {e}")
        if i % 50 == 0:
            log(f"进度 {i}/{len(srcs)} ok={ok} skip={skip} fail={fail}")
    log(f"完成: ok={ok} skip={skip} fail={fail}")
    return 0 if fail == 0 else 1

if __name__ == "__main__":
    sys.exit(main())
