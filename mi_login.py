"""打开浏览器处理小米云登录并保存 cookies.json
用法: python login.py [--refresh]
  --refresh  无头静默续签(passToken 有效时无需人工); 失效则退出码 1
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
COOKIE_FILE = BASE / "cookies.json"
PROFILE = BASE / "browser-profile"

def account_ok(cookies):
    names = {c["name"] for c in cookies}
    return bool(names & {"userId", "cUserId"}) and "passToken" in names

def has_login(cookies):
    return account_ok(cookies) and any(c["name"] == "serviceToken" for c in cookies)

def main():
    refresh = "--refresh" in sys.argv
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE),
            headless=refresh,
            viewport={"width": 1100, "height": 800},
            locale="zh-CN",
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://i.mi.com/", wait_until="domcontentloaded", timeout=60000)
            if not refresh:
                print("浏览器已打开，请在窗口中完成登录（短信验证码自行输入）...", flush=True)

            deadline = time.time() + (120 if refresh else 900)
            while time.time() < deadline:
                try:
                    cookies = ctx.cookies()
                except Exception as e:
                    print(f"cookie 读取异常(重试): {e}", flush=True)
                    time.sleep(2)
                    continue
                if account_ok(cookies) and not has_login(cookies):
                    try:
                        page.goto("https://i.mi.com/gallery/h5", wait_until="domcontentloaded", timeout=30000)
                    except Exception:
                        pass
                    time.sleep(3)
                    cookies = ctx.cookies()
                if has_login(cookies):
                    COOKIE_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=1))
                    print(f"登录成功 ✓ 已保存 {len(cookies)} 条 cookie → {COOKIE_FILE}", flush=True)
                    return 0
                time.sleep(2)

            print("超时未检测到登录态", flush=True)
            return 1
        finally:
            try:
                ctx.close()
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main())
