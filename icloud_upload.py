"""iCloud 上传器 v9
- HEIC/HEIF → converted/<stem>.jpg ; MP4 → converted_mov/<stem>.mov（网页不支持 HEIC/MP4）
- 单批 ≤1000 件（再按字节上限封顶），每批前重载页面清空面板残留并刷新会话
- 完成判定：以面板状态稳定为准；重复项(已在库)计入完成
用法: python icloud_upload.py [--batch N] [--limit N] [--headless] [--wait-login 秒]
  --headless      无头运行（iCloud 风控对人机特征敏感，服务器/守护场景自行权衡）
  --wait-login    等待人工完成登录的秒数（默认 1500；定时/守护场景建议 60-120）
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE = BASE / "icloud-profile"
PHOTOS = BASE / "photos"
CONVERTED = BASE / "converted"
CONVERTED_MOV = BASE / "converted_mov"
STATE = BASE / "icloud-upload-state.json"
COOKIE_FILE = BASE / "icloud-cookies.json"
LOG = BASE / "icloud-upload.log"

MAX_BATCH = 1000
MAX_BATCH_BYTES = 1_500_000_000
SIZE_RE = re.compile(r"^\d+(\.\d+)?\s*(KB|MB|GB|B)$", re.I)
DUP_RE = re.compile(r"重复项目|already")
UNSUP_RE = re.compile(r"不支持|isn.t supported")
ERR_RE = re.compile(r"失败|无法|错误|异常|error")
COUNT_RE = re.compile(r"([\d,]+)\s*张照片[，,]\s*([\d,]+)\s*个视频")

def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"done": {}, "failed": {}, "deferred": {}}

def save_state(st):
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1))

def file_key(p: Path):
    s = p.stat()
    return f"{p.name}:{s.st_size}"

def save_cookies(ctx):
    try:
        COOKIE_FILE.write_text(json.dumps(ctx.cookies(), ensure_ascii=False, indent=1))
    except Exception as e:
        log(f"凭据保存失败: {e}")

def restore_cookies(ctx):
    if COOKIE_FILE.exists():
        try:
            ctx.add_cookies(json.loads(COOKIE_FILE.read_text()))
            log("已注入保存的登录凭据")
        except Exception as e:
            log(f"凭据注入失败: {e}")

def is_logged_in(page):
    names = {c["name"] for c in page.context.cookies()}
    if "X-APPLE-WEBAUTH-USER" in names or "X-APPLE-DSID" in names:
        return True
    for f in page.frames:
        if "photos3" in f.url:
            try:
                if f.locator("input[type=file]").count() > 0:
                    return True
            except Exception:
                continue
    return False

def wait_login(page, timeout=1500):
    end = time.time() + timeout
    while time.time() < end:
        if is_logged_in(page):
            return True
        time.sleep(3)
    return False

def photos_frame(page):
    for _ in range(60):
        f = next((x for x in page.frames if "photos3" in x.url), None)
        if f:
            try:
                if f.locator("input[type=file]").count() > 0:
                    return f
            except Exception:
                pass
        time.sleep(3)
    return None

def open_photos(page):
    try:
        page.goto("https://www.icloud.com.cn/photos/", wait_until="domcontentloaded", timeout=90000)
    except Exception as e:
        log(f"打开页面异常: {e}")
        return None
    f = photos_frame(page)
    if f:
        time.sleep(5)
    return f

def panel_text(frame):
    try:
        return frame.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""

def lib_counts(text):
    m = COUNT_RE.search(text)
    if not m:
        return None
    return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))

def parse_panel(text, submitted):
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    res = {}
    for i, l in enumerate(lines):
        if l in submitted:
            stt = lines[i + 1] if i + 1 < len(lines) else ""
            if stt in submitted:
                stt = ""
            res[l] = stt
    return res

def upload_paths(batch):
    out, trans = [], {}
    for x in batch:
        s = x.suffix.lower()
        if s in (".heic", ".heif"):
            c = CONVERTED / (x.stem + ".jpg")
            out.append(str(c if c.exists() else x)); trans[(c.name if c.exists() else x.name)] = x.name
        elif s == ".mp4":
            c = CONVERTED_MOV / (x.stem + ".mov")
            out.append(str(c if c.exists() else x)); trans[(c.name if c.exists() else x.name)] = x.name
        else:
            out.append(str(x)); trans[x.name] = x.name
    return out, trans

def pack(queue, max_files, max_bytes):
    batches, cur, cb = [], [], 0
    for x in queue:
        sz = x.stat().st_size
        if cur and (len(cur) >= max_files or cb + sz > max_bytes):
            batches.append(cur); cur, cb = [], 0
        cur.append(x); cb += sz
    if cur:
        batches.append(cur)
    return batches

def ensure_frame(page):
    """返回可用的 photos3 frame；必要时重载最多 3 次"""
    for _ in range(3):
        for f in page.frames:
            if "photos3" in f.url:
                try:
                    if f.locator("input[type=file]").count() > 0:
                        return f
                except Exception:
                    pass
        f = open_photos(page)
        if f:
            return f
        log("页面未就绪，重试打开...")
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--batch", type=int, default=MAX_BATCH)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait-login", type=int, default=1500)
    args = ap.parse_args()
    args.batch = min(args.batch, MAX_BATCH)

    st = load_state()
    st.setdefault("deferred", {})
    files = sorted([p for p in PHOTOS.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime)
    if args.limit:
        files = files[: args.limit]
    done, defer = st["done"], st["deferred"]
    queue = [p for p in files if file_key(p) not in done and file_key(p) not in defer]
    batches = pack(queue, args.batch, MAX_BATCH_BYTES)
    log(f"总 {len(files)}，已传 {len(done)}，暂缓 {len(defer)}，本轮 {len(queue)} 件 / {len(batches)} 批")

    if not queue:
        log("没有待传文件，跳过启动浏览器")
        return 0

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), headless=args.headless, viewport={"width": 1400, "height": 950},
            locale="zh-CN", args=["--password-store=basic", "--no-proxy-server"])
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            restore_cookies(ctx)
            frame = open_photos(page)
            if not frame:
                if not wait_login(page, args.wait_login):
                    log("未登录"); return 1
                frame = open_photos(page)
                if not frame:
                    log("无法进入照片页"); return 1
            log("✓ 照片页就绪")
            save_cookies(ctx)

            for bi, batch in enumerate(batches, 1):
                frame = ensure_frame(page)
                if not frame:
                    log("无法进入照片页，等待重新登录...")
                    if not wait_login(page, args.wait_login):
                        return 3
                    frame = ensure_frame(page)
                    if not frame:
                        return 3
                c0 = lib_counts(panel_text(frame))

                paths, trans = upload_paths(batch)
                submitted = set(trans.keys())
                bbytes = sum(x.stat().st_size for x in batch)
                timeout = min(7200, max(900, int(bbytes / (25 * 1024)) + 900))
                log(f"批次#{bi}/{len(batches)} {len(batch)} 件 / {bbytes/1e6:.0f}MB  起始库={c0}")

                try:
                    frame.locator("input[type=file]").first.set_input_files(paths)
                except Exception as e:
                    log(f"  提交失败: {e}")
                    frame = ensure_frame(page)
                    continue

                end = time.time() + timeout
                appeared = False
                res, settled, stable = {}, False, 0
                last_log = 0
                while time.time() < end:
                    time.sleep(5)
                    text = panel_text(frame)
                    res = parse_panel(text, submitted)
                    if res:
                        appeared = True
                    inprog = [n for n, s in res.items() if SIZE_RE.match(s or "")]
                    all_settled = True
                    for nm in submitted:
                        stt = res.get(nm)
                        if stt is None:
                            continue  # 未列出=已成功离开列表
                        if SIZE_RE.match(stt or "") or not (DUP_RE.search(stt) or UNSUP_RE.search(stt) or ERR_RE.search(stt)):
                            all_settled = False
                            break
                    if appeared and all_settled and not inprog:
                        stable += 1
                        if stable >= 6:  # 连续~30秒稳定
                            settled = True
                            break
                    else:
                        stable = 0
                    if time.time() - last_log > 30:
                        last_log = time.time()
                        nfail = sum(1 for s in res.values() if DUP_RE.search(s) or UNSUP_RE.search(s) or ERR_RE.search(s))
                        log(f"    ...已列出{len(res)} 失败{nfail} 进行中{len(inprog)} appeared={appeared}")

                failures = {nm: stt for nm, stt in res.items() if DUP_RE.search(stt) or UNSUP_RE.search(stt) or ERR_RE.search(stt)}
                fail_orig = {trans.get(nm, nm): stt for nm, stt in failures.items()}
                unsup = {x.name for x in batch if x.name in fail_orig and UNSUP_RE.search(fail_orig[x.name])}
                err = {x.name for x in batch if x.name in fail_orig and ERR_RE.search(fail_orig[x.name])}
                dup = {x.name for x in batch if x.name in fail_orig and DUP_RE.search(fail_orig[x.name])}

                newly = 0
                if settled:
                    for x in batch:
                        if x.name in unsup or x.name in err:
                            st["failed"][file_key(x)] = "unsupported_or_error"
                        else:
                            done[file_key(x)] = {"name": x.name, "size": x.stat().st_size}
                            newly += 1
                    save_state(st); save_cookies(ctx)
                    log(f"  ✓ 完成 新增{newly} 重复{len(dup)} 不支持{len(unsup)} 错误{len(err)} 累计 {len(done)}/{len(files)}")
                else:
                    # 超时：只把「重复」记为完成，其余留在队列，避免误标
                    for x in batch:
                        if x.name in dup:
                            done[file_key(x)] = {"name": x.name, "size": x.stat().st_size}
                            newly += 1
                    save_state(st); save_cookies(ctx)
                    log(f"  ⚠ 超时 已列出{len(res)} 重复{len(dup)} 进行中{len(inprog)} 累计 {len(done)}/{len(files)}（未完成的留待下轮）")
                    if newly == 0 and not inprog:
                        for x in batch[:5]:
                            st["failed"][file_key(x)] = "no_progress"
                        save_state(st)
                        log(f"  跳过前 5 个")
                    frame = ensure_frame(page)

            save_cookies(ctx)
            log(f"结束: 完成度 {len(done)}/{len(files)}")
            return 0
        finally:
            try:
                ctx.close()
            except Exception:
                pass

if __name__ == "__main__":
    sys.exit(main())
