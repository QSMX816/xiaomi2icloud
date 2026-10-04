"""深度 DOM 文本提取：找 iCloud 照片网格中的文件名/日期文本（不截图）"""
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE = BASE / "icloud-profile"
COOKIE_FILE = BASE / "icloud-cookies.json"

EXTRACT_JS = r"""
() => {
  const out = new Set();
  const pat = /(IMG_|VID_|PXL_|Screenshot|\.jpg|\.jpeg|\.png|\.heic|\.heif|\.mp4|\.mov)/i;
  const walk = (el) => {
    for (const a of el.attributes || []) {
      const v = a.value || '';
      if (pat.test(v)) out.add(a.name + '=' + v.slice(0, 160));
    }
    for (const n of el.childNodes) {
      if (n.nodeType === 3) {
        const t = (n.textContent || '').trim();
        if (t && pat.test(t)) out.add('text=' + t.slice(0, 160));
      } else if (n.nodeType === 1) walk(n);
    }
  };
  walk(document.body);
  return [...out];
}
"""

def main():
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=True, viewport={"width": 1280, "height": 900},
            locale="zh-CN", args=["--password-store=basic"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            if COOKIE_FILE.exists():
                ctx.add_cookies(json.loads(COOKIE_FILE.read_text()))
            page.goto("https://www.icloud.com.cn/photos/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(12)
            frame = next((f for f in page.frames if "photos3" in f.url), None)
            if frame is None:
                print("no photos3 frame")
                return 1

            seen = set()
            for round_ in range(8):
                for it in frame.evaluate(EXTRACT_JS):
                    seen.add(it)
                try:
                    frame.mouse.wheel(0, 5000)
                except Exception:
                    pass
                time.sleep(2)
                print(f"round {round_}: 累计 {len(seen)}")

            Path(BASE / "icloud-verify.json").write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1))
            for s in sorted(seen)[:50]:
                print(" ", s[:120])
            return 0
        finally:
            ctx.close()

if __name__ == "__main__":
    sys.exit(main())
