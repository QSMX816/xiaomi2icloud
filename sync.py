"""增量同步一轮：小米云相册 → 本地(下载/转码) → iCloud(上传)

每轮步骤（全部幂等、可断点续传、可单独重跑）：
1. 下载   列举小米云端全部条目，与 manifest.jsonl 已知清单按 ID/SHA1 对比，只下载新增/变更
2. 转码   HEIC → JPEG、MP4 → MOV（已有产物自动跳过）
3. 上传   驱动 icloud_upload.py（其状态文件自动跳过已传；无待传时不启动浏览器）
4. 续签   任一端会话过期时自动无头续签一次；仍失败则通知人工
          （小米云短信验证码 / Apple ID 双重认证无法自动化）

用法: python sync.py [--dry-run] [--batch N] [--workers N]
配置: config.json（缺省用默认值，字段见 config.example.json）
退出码: 0=正常  1=存在失败步骤  75=已有同步在运行
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mi_download import AuthError, MiCloud

BASE = Path(__file__).parent
PHOTOS = BASE / "photos"
CONVERTED = BASE / "converted"
CONVERTED_MOV = BASE / "converted_mov"
MANIFEST = BASE / "manifest.jsonl"
UPSTATE = BASE / "icloud-upload-state.json"
CONFIG = BASE / "config.json"
LOG = BASE / "sync.log"
LOCK = BASE / ".sync.lock"

DEFAULTS = {
    "interval_minutes": 60,
    "download_workers": 3,
    "upload_batch": 1000,
    "upload_headless": False,
    "login_wait_seconds": 90,
    "mi_album_id": "1",
    "notify_command": [],
}

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")

def load_config():
    cfg = dict(DEFAULTS)
    if CONFIG.exists():
        try:
            cfg.update(json.loads(CONFIG.read_text()))
        except Exception as e:
            log(f"config.json 解析失败，使用默认值: {e}")
    return cfg

def notify(cfg, title, body):
    log(f"🔔 {title} —— {body}")
    cmd = cfg.get("notify_command") or []
    if not cmd:
        return
    try:
        subprocess.run([c.format(title=title, body=body) for c in cmd],
                       timeout=30, capture_output=True)
    except Exception as e:
        log(f"通知命令执行失败: {e}")

# ---------- 单实例锁 ----------

def acquire_lock():
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {time.time()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        if time.time() - LOCK.stat().st_mtime > 6 * 3600:  # 超 6 小时视为残留
            LOCK.unlink(missing_ok=True)
            return acquire_lock()
        return False

def release_lock():
    LOCK.unlink(missing_ok=True)

# ---------- 第 1 步：下载（增量） ----------

def _record(it, status):
    return {"id": it.get("id"), "fileName": it.get("fileName"), "sha1": it.get("sha1"),
            "size": it.get("size"), "type": it.get("type"), "mimeType": it.get("mimeType"),
            "dateTaken": it.get("dateTaken"), "sortTime": it.get("sortTime"),
            "createTime": it.get("createTime"), "exifInfo": it.get("exifInfo"), "status": status}

def known_items():
    """manifest.jsonl → {id: 最后一条记录}（与 mi_download.py 写入格式一致）"""
    last = {}
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("id") is not None:
                last[rec["id"]] = rec
    return last

def append_manifest(rec):
    with open(MANIFEST, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def mi_refresh():
    log("小米云会话过期，尝试无头续签...")
    rc = subprocess.run([sys.executable, str(BASE / "mi_login.py"), "--refresh"]).returncode
    log(f"mi_login --refresh 退出码 {rc}")
    return rc == 0

def download_all(cloud, todo, workers):
    """并发下载 todo，逐条落 manifest。返回 (ok, fail, auth_dead)"""
    cnt = {"ok": 0, "fail": 0}
    auth_dead = threading.Event()
    mlock = threading.Lock()

    def one(it):
        if auth_dead.is_set():
            return
        try:
            cloud.download(it, PHOTOS)
            rec = _record(it, "ok")
        except AuthError:
            auth_dead.set()
            rec = _record(it, "fail")
        except Exception as e:
            log(f"✗ {it.get('fileName')}: {e}")
            rec = _record(it, "fail")
        with mlock:
            append_manifest(rec)
            cnt["fail" if rec["status"] == "fail" else "ok"] += 1

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(one, todo))
    return cnt["ok"], cnt["fail"], auth_dead.is_set()

def phase_download(cfg, dry):
    if not (BASE / "cookies.json").exists():
        log("⚠ 尚未登录小米云（缺 cookies.json）。请先运行: python mi_login.py")
        return bool(dry)  # dry-run 只提示不算失败

    cloud = MiCloud()
    items = None
    for attempt in (1, 2):
        try:
            items = cloud.list_all(album_id=str(cfg.get("mi_album_id", "1")))
            break
        except AuthError:
            if attempt == 2 or not mi_refresh():
                log("✗ 小米云会话无法续签，需要人工登录: python mi_login.py")
                notify(cfg, "xiaomi2icloud 需要重新登录小米云",
                       "在项目目录运行 python mi_login.py 完成短信验证，之后自动恢复")
                return False
            cloud = MiCloud()
    if items is None:
        return False

    known = known_items()
    todo = [it for it in items
            if it["id"] not in known
            or known[it["id"]].get("sha1") != it.get("sha1")
            or known[it["id"]].get("status") == "fail"]
    log(f"[下载] 云端 {len(items)} 项，已知 {len(known)}，新增/变更 {len(todo)}")
    if dry:
        for it in todo[:10]:
            log(f"  [dry] 将下载 {it.get('fileName')}")
        if len(todo) > 10:
            log(f"  ...等共 {len(todo)} 项")
        return True
    if not todo:
        return True

    PHOTOS.mkdir(exist_ok=True)
    ok = fail = 0
    for attempt in (1, 2):
        n_ok, n_fail, dead = download_all(cloud, todo, int(cfg.get("download_workers", 3)))
        ok += n_ok
        fail += n_fail
        if not dead:
            break
        if attempt == 2 or not mi_refresh():
            log("✗ 下载中途会话失效且无法续签，剩余项留待下轮")
            break
        cloud = MiCloud()
        done_ids = set(known_items())
        todo = [it for it in todo if it["id"] not in done_ids]
    log(f"[下载] 完成: 新增 {ok}，失败 {fail}")
    return fail == 0

# ---------- 第 2 步：转码（增量） ----------

def phase_convert(dry):
    files = [p for p in PHOTOS.iterdir() if p.is_file()] if PHOTOS.exists() else []
    heics = sorted([p for p in files if p.suffix.lower() in (".heic", ".heif")])
    mp4s = sorted([p for p in files if p.suffix.lower() == ".mp4"])
    h_todo = [p for p in heics if not (CONVERTED / (p.stem + ".jpg")).exists()]
    m_todo = [p for p in mp4s if not (CONVERTED_MOV / (p.stem + ".mov")).exists()]
    log(f"[转码] HEIC 待转 {len(h_todo)}/{len(heics)}，MP4 待转 {len(m_todo)}/{len(mp4s)}")
    if dry:
        for p in (h_todo + m_todo)[:10]:
            log(f"  [dry] 将转码 {p.name}")
        return True
    if not h_todo and not m_todo:
        return True

    try:
        from heic_convert import convert_one as heic_one
    except ImportError:
        log("✗ 缺少转码依赖（Pillow / pillow-heif），请先 pip install -r requirements.txt")
        return False
    from mov_convert import convert_one as mov_one

    CONVERTED.mkdir(exist_ok=True)
    CONVERTED_MOV.mkdir(exist_ok=True)
    ok = skip = fail = 0
    for p in h_todo + m_todo:
        fn = heic_one if p.suffix.lower() in (".heic", ".heif") else mov_one
        try:
            r = fn(p)
            ok += r == "ok"
            skip += r == "skip"
        except Exception as e:
            fail += 1
            log(f"✗ {p.name}: {e}")
    log(f"[转码] 完成: ok={ok} skip={skip} fail={fail}")
    return fail == 0

# ---------- 第 3 步：上传（增量） ----------

def upload_queue():
    files = sorted([p for p in PHOTOS.iterdir() if p.is_file()],
                   key=lambda p: p.stat().st_mtime) if PHOTOS.exists() else []
    st = {"done": {}, "deferred": {}}
    if UPSTATE.exists():
        try:
            loaded = json.loads(UPSTATE.read_text())
            st["done"] = loaded.get("done", {})
            st["deferred"] = loaded.get("deferred", {})
        except Exception:
            pass

    def key(p):
        s = p.stat()
        return f"{p.name}:{s.st_size}"

    return [p for p in files if key(p) not in st["done"] and key(p) not in st["deferred"]]

def icloud_refresh():
    log("iCloud 会话失效，尝试无头续签...")
    rc = subprocess.run([sys.executable, str(BASE / "icloud_login.py"), "--refresh"]).returncode
    log(f"icloud_login --refresh 退出码 {rc}")
    return rc == 0

def phase_upload(cfg, dry):
    queue = upload_queue()
    log(f"[上传] 待上传 {len(queue)} 件")
    if dry:
        for p in queue[:10]:
            log(f"  [dry] 将上传 {p.name}")
        return True
    if not queue:
        return True

    cmd = [sys.executable, str(BASE / "icloud_upload.py"),
           "--batch", str(cfg.get("upload_batch", 1000)),
           "--wait-login", str(cfg.get("login_wait_seconds", 90))]
    if cfg.get("upload_headless"):
        cmd.append("--headless")

    rc = subprocess.run(cmd).returncode
    if rc in (1, 3):
        if icloud_refresh():
            log("续签成功，重试上传...")
            rc = subprocess.run(cmd).returncode
    if rc != 0:
        log(f"✗ 上传退出码 {rc}")
        notify(cfg, "xiaomi2icloud 上传失败 / 需要登录 iCloud",
               "在项目目录运行 python icloud_login.py 完成双重认证，之后自动恢复；详情见 icloud-upload.log")
        return False
    return True

# ---------- 主流程 ----------

def main():
    ap = argparse.ArgumentParser(description="增量同步一轮：小米云 → 本地 → iCloud")
    ap.add_argument("--dry-run", action="store_true", help="只报告将做什么，不联网、不改动")
    ap.add_argument("--batch", type=int, help="覆盖 upload_batch 配置")
    ap.add_argument("--workers", type=int, help="覆盖 download_workers 配置")
    args = ap.parse_args()

    cfg = load_config()
    if args.batch:
        cfg["upload_batch"] = args.batch
    if args.workers:
        cfg["download_workers"] = args.workers

    if not acquire_lock():
        log("已有同步在运行，退出")
        return 75
    try:
        if args.dry_run:
            log("===== dry-run（只报告，不执行）=====")
        t0 = time.time()
        results = {
            "下载": phase_download(cfg, args.dry_run),
            "转码": phase_convert(args.dry_run),
            "上传": phase_upload(cfg, args.dry_run),
        }
        bad = [k for k, v in results.items() if not v]
        if bad:
            log(f"本轮结束（{time.time() - t0:.0f}s）: ✗ 失败步骤 {','.join(bad)}")
            return 1
        log(f"本轮结束（{time.time() - t0:.0f}s）: ✓ 全部完成")
        return 0
    finally:
        release_lock()

if __name__ == "__main__":
    sys.exit(main())
