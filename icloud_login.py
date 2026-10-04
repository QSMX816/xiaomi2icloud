"""打开浏览器让用户登录 iCloud，检测到登录态后保存 icloud-cookies.json
用法: python icloud_login.py [--refresh]
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
COOKIE_FILE = BASE / "icloud-cookies.json"
PROFILE = BASE / "icloud-profile"

def account_ok(cookies):
    names = {c["name"] for c in cookies}
    return bool(names & {"X-APPLE-WEBAUTH-USER", "X-APPLE-DSID"}) and "X-APPLE-WEBAUTH-HSA-TRUST" in names

def main():
    refresh = "--refresh" in sys.argv
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=refresh,
            viewport={"width": 1200, "height": 850},
            locale="zh-CN",
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.icloud.com/photos/", wait_until="domcontentloaded", timeout=90000)
            if not refresh:
                print("浏览器已打开，请登录 Apple ID（含双重认证验证码）...", flush=True)

            deadline = time.time() + (150 if refresh else 1200)
            while time.time() < deadline:
                try:
                    cookies = ctx.cookies()
                except Exception as e:
                    print(f"cookie 读取异常(重试): {e}", flush=True)
                    time.sleep(3)
                    continue
                if account_ok(cookies):
                    try:
                        if "/photos" not in page.url:
                            page.goto("https://www.icloud.com/photos/", wait_until="domcontentloaded", timeout=60000)
                            time.sleep(3)
                            cookies = ctx.cookies()
                    except Exception:
                        pass
                    if account_ok(cookies):
                        COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=1))
                        print(f"iCloud 登录成功 ✓ 已保存 {len(cookies)} 条 cookie → {COOKIE_FILE}", flush=True)
                        return 0
                time.sleep(3)

            print("超时未检测到登录态", flush=True)
            return 1
        finally:
            try:
                ctx.close()
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main())
