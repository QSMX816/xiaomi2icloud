"""MP4 → MOV 无损重封装（ffmpeg 流复制，不重编码）：保留 mtime
iCloud 网页上传不支持 MP4，需转成 MOV 容器（视频流原样拷贝）
输出: converted_mov/<原名>.mov
用法: python mov_convert.py [--limit N]
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
PHOTOS = BASE / "photos"
OUT = BASE / "converted_mov"
LOG = BASE / "mov_convert.log"

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def convert_one(src: Path):
    dst = OUT / (src.stem + ".mov")
    if dst.exists() and dst.stat().st_size > 0:
        return "skip"
    tmp = dst.with_suffix(".mov.part")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-c", "copy", "-movflags", "+faststart", "-f", "mov", str(tmp)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr else f"ffmpeg 退出码 {r.returncode}")
    tmp.rename(dst)
    st = src.stat()
    os.utime(dst, (st.st_mtime, st.st_mtime))
    return "ok"

def main():
    if not shutil.which("ffmpeg"):
        print("未找到 ffmpeg，请先安装（apt install ffmpeg / brew install ffmpeg）")
        return 1
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    srcs = sorted([p for p in PHOTOS.iterdir() if p.suffix.lower() == ".mp4"])
    if args.limit:
        srcs = srcs[: args.limit]
    log(f"待转换 {len(srcs)} 个 MP4")
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
