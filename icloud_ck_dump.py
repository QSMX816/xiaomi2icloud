"""旁听 iCloud 照片 CloudKit 同步响应，提取已入库文件名（不截图，只截 API JSON）
输出: icloud-ck-dump.json
"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE = BASE / "icloud-profile"
COOKIE_FILE = BASE / "icloud-cookies.json"
OUT = BASE / "icloud-ck-dump.json"

captured = []

def on_response(resp):
    u = resp.url
    if any(k in u for k in ("changes/zone", "records/query", "records/lookup", "records/changes")):
        try:
            body = resp.text()
            captured.append({"url": u.split("?")[0], "body": body[:2_000_000]})
        except Exception:
            pass

def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=True, viewport={"width": 1280, "height": 900},
            locale="zh-CN", args=["--password-store=basic"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if COOKIE_FILE.exists():
                ctx.add_cookies(json.loads(COOKIE_FILE.read_text()))
            page.on("response", on_response)
            page.goto("https://www.icloud.com.cn/photos/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(20)
            # 强制刷新再等一轮，促使全量同步
            page.reload(wait_until="domcontentloaded", timeout=90000)
            time.sleep(25)

            print(f"捕获 {len(captured)} 条 CK 响应")
            # 汇总所有疑似文件名
            names = set()
            pat = re.compile(r"(IMG_\d+[-_]\d+|VID_\d+[-_]\d+|[A-Za-z0-9_-]{6,}\.(?:jpg|jpeg|png|heic|heif|mp4|mov))", re.I)
            for c in captured:
                for m in pat.findall(c["body"]):
                    names.add(m)
            print(f"响应中出现的文件名样 token: {len(names)}")
            for n in sorted(names)[:40]:
                print(" ", n)
            Path(OUT).write_text(json.dumps({"captured": captured, "names": sorted(names)}, ensure_ascii=False))
            print(f"完整响应已存 {OUT} ({Path(OUT).stat().st_size/1e6:.1f}MB)")
            return 0
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())
