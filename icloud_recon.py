"""对账 v3：主 profile + CDP 清 IndexedDB(保 cookie) 强制重新拉全库，滚动旁听查询解文件名"""
import base64
import json
import sys
import time
from collections import Counter
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE = BASE / "icloud-profile"
COOKIE_FILE = BASE / "icloud-cookies.json"
OUT = BASE / "icloud-names.json"

names = {}
counts = []
statuses = []

def on_resp(r):
    u = r.url
    if "ckdatabasews" in u and ("query" in u or "lookup" in u):
        statuses.append(r.status)
        if r.status != 200:
            return
        try:
            data = json.loads(r.text())
        except Exception:
            return
        for rec in data.get("records", []):
            rt = rec.get("recordType")
            if rt == "IndexCountResult":
                cm = rec.get("fields", {}).get("itemCount", {}).get("value")
                if cm:
                    counts.append(cm)
            elif rt == "CPLMaster":
                fe = (rec.get("fields", {}).get("filenameEnc") or {}).get("value", "")
                if fe:
                    try:
                        nm = base64.b64decode(fe).decode("utf-8", "replace")
                    except Exception:
                        continue
                    if nm:
                        names[nm] = (rec.get("fields", {}).get("importDate") or {}).get("value")

def log(*a):
    print(*a, flush=True)

def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=False, viewport={"width": 1400, "height": 950},
            locale="zh-CN", args=["--password-store=basic", "--no-proxy-server"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if COOKIE_FILE.exists():
                ctx.add_cookies(json.loads(COOKIE_FILE.read_text()))
            page.goto("https://www.icloud.com.cn/photos/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(10)

            # 清 IndexedDB/CacheStorage（保留 cookie），强制重新同步
            try:
                cdp = ctx.new_cdp_session(page)
                cdp.send("Storage.clearDataForOrigin", {
                    "origin": "https://www.icloud.com.cn",
                    "storageTypes": "indexeddb,cache_storage,local_storage",
                })
                log("已清 IndexedDB/CacheStorage/localStorage")
            except Exception as e:
                log("清缓存失败:", e)

            page.on("response", on_resp)
            page.reload(wait_until="domcontentloaded", timeout=90000)
            time.sleep(15)

            frame = None
            for _ in range(30):
                frame = next((f for f in page.frames if "photos3" in f.url), None)
                if frame:
                    break
                time.sleep(3)
            if not frame:
                log("photos3 未出现")
                return 1
            log("应用就绪，开始滚动...")

            empty = 0
            for i in range(400):
                before = len(names)
                for f in list(page.frames):
                    if "photos3" in f.url:
                        try:
                            f.mouse.wheel(0, 6000)
                        except Exception:
                            pass
                time.sleep(1.2)
                if len(names) == before:
                    empty += 1
                    if empty >= 30:
                        break
                else:
                    empty = 0
                if i % 10 == 0:
                    log(f"  round {i}: names={len(names)} counts={counts[-3:]}")

            OUT.write_text(json.dumps(names, ensure_ascii=False))
            log(f"\n云端文件名总数: {len(names)}")
            if counts:
                log(f"图库计数 max={max(counts)}")
            log(f"查询状态分布: {dict(Counter(statuses))}")
            local = {x.name for x in (BASE / "photos").iterdir() if x.is_file()}
            hit = local & set(names)
            log(f"本地 {len(local)}，重合(已在库) {len(hit)}，仍需上传 {len(local - set(names))}")
            return 0
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())
