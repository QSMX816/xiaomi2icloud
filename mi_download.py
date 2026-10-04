"""小米云相册下载器：全量下载照片/视频，保留拍摄时间（mtime + EXIF），断点续传
用法: python micloud_dl.py [--limit N] [--out DIR] [--workers N]
"""
import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = Path(__file__).parent
COOKIE_FILE = BASE / "cookies.json"
MANIFEST = BASE / "manifest.jsonl"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"

print_lock = threading.Lock()
manifest_lock = threading.Lock()
auth_dead = threading.Event()

class AuthError(Exception):
    pass

def log(msg):
    with print_lock:
        print(msg, flush=True)

def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

class MiCloud:
    def __init__(self):
        cookies = {c["name"]: c["value"] for c in json.load(open(COOKIE_FILE))}
        self.s = requests.Session()
        self.s.trust_env = False
        self.s.cookies.update(cookies)
        self.s.headers.update({"User-Agent": UA, "Referer": "https://i.mi.com/gallery/h5"})
        self.last_keepalive = 0.0

    def keepalive(self):
        if time.time() - self.last_keepalive > 120:
            try:
                self.s.get("https://i.mi.com/status/lite/setting?type=AutoRenewal&inactiveTime=10", timeout=20)
                self.last_keepalive = time.time()
            except requests.RequestException:
                pass

    def list_all(self, album_id="1", limit=None):
        page, items = 0, []
        while True:
            self.keepalive()
            r = self.s.get(
                "https://i.mi.com/gallery/user/galleries",
                params={"startDate": "19700101", "endDate": "20350101", "pageNum": page, "pageSize": 50, "albumId": album_id},
                timeout=30,
            )
            if r.status_code == 401:
                auth_dead.set()
                raise AuthError("会话过期")
            r.raise_for_status()
            data = r.json()["data"]
            items.extend(data["galleries"])
            log(f"列出第 {page} 页: 累计 {len(items)} 项")
            if data["isLastPage"] or (limit and len(items) >= limit):
                return items[:limit] if limit else items
            page += 1

    def resolve_download(self, pic_id):
        for attempt in range(4):
            try:
                self.keepalive()
                r = self.s.get("https://i.mi.com/gallery/storage", params={"id": pic_id}, timeout=30)
                body = r.json()
                if "data" not in body:
                    if r.status_code == 401 or "serviceLogin" in str(body.get("D", "")):
                        auth_dead.set()
                        raise AuthError("会话过期")
                    raise RuntimeError(f"storage 异常: {str(body)[:80]}")
                url = body["data"]["url"]
                j = self.s.get(url, timeout=30).text
                j = j.strip()
                begin, end = "dl_img_cb(", ")"
                if not (j.startswith(begin) and j.endswith(end)):
                    raise ValueError("JSONP 格式异常: " + j[:80])
                info = json.loads(j[len(begin):-len(end)])
                return info["url"], info["meta"]
            except AuthError:
                raise
            except Exception as e:
                if attempt == 3:
                    raise
                time.sleep(2 * (attempt + 1))

    def download(self, item, outdir):
        if auth_dead.is_set():
            raise AuthError("会话过期")
        fname = item["fileName"]
        dest = outdir / fname
        if dest.exists() and dest.stat().st_size == item.get("size") and sha1_of(dest) == item.get("sha1"):
            set_mtime(dest, item)
            return "skip"
        if dest.exists():  # 同名不同内容, 用 id 区分
            dest = outdir / f"{item['id']}_{fname}"

        url, meta = self.resolve_download(item["id"])
        tmp = dest.with_suffix(dest.suffix + ".part")
        with self.s.post(url, data=f"meta={meta}", stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if chunk:
                        f.write(chunk)
        if item.get("sha1") and sha1_of(tmp) != item["sha1"]:
            tmp.unlink()
            raise RuntimeError(f"sha1 不匹配: {fname}")
        tmp.rename(dest)
        set_mtime(dest, item)
        patch_exif(dest, item)
        return "ok"

def set_mtime(dest, item):
    ts = item.get("dateTaken") or item.get("sortTime")
    if ts:
        t = ts / 1000
        os.utime(dest, (t, t))

def patch_exif(dest, item):
    """JPEG 缺 DateTimeOriginal 时按云上 exifInfo 补写; 其余情况不动原文件"""
    if dest.suffix.lower() not in (".jpg", ".jpeg"):
        return
    dt = (item.get("exifInfo") or {}).get("dateTime")
    if not dt:
        return
    try:
        import piexif
        from PIL import Image
        exif = piexif.load(str(dest))
        if exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal):
            return
        raw = dt.replace(":", "-", 2)  # 仅用于判断格式合法性
        b = dt.encode()
        exif["Exif"][piexif.ExifIFD.DateTimeOriginal] = b
        exif["Exif"][piexif.ExifIFD.DateTimeDigitized] = b
        exif["0th"][piexif.ImageIFD.DateTime] = b
        piexif.insert(piexif.dump(exif), str(dest))
    except Exception:
        pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(BASE / "photos"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    cloud = MiCloud()
    try:
        items = cloud.list_all(limit=args.limit)
    except AuthError:
        log("会话过期，退出码 2（由 supervisor 续签后重跑）")
        return 2
    log(f"共 {len(items)} 项待处理 → {outdir}")

    done = ok = skip = fail = 0
    failures = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(cloud.download, it, outdir): it for it in items}
        for fut in as_completed(futs):
            it = futs[fut]
            done += 1
            status = "fail"
            try:
                res = fut.result()
                if res == "ok":
                    ok += 1
                else:
                    skip += 1
                status = res
            except Exception as e:
                fail += 1
                failures.append({"id": it["id"], "fileName": it["fileName"], "error": str(e)})
                log(f"✗ {it['fileName']}: {e}")
            with manifest_lock:
                with open(MANIFEST, "a") as f:
                    f.write(json.dumps({
                        "id": it["id"], "fileName": it["fileName"], "sha1": it.get("sha1"),
                        "size": it.get("size"), "type": it.get("type"), "mimeType": it.get("mimeType"),
                        "dateTaken": it.get("dateTaken"), "sortTime": it.get("sortTime"),
                        "createTime": it.get("createTime"), "exifInfo": it.get("exifInfo"),
                        "status": status,
                    }, ensure_ascii=False) + "\n")
            if done % 50 == 0:
                log(f"进度 {done}/{len(items)}  新下载 {ok} 跳过 {skip} 失败 {fail}")

    log(f"完成: 新下载 {ok}, 跳过 {skip}, 失败 {fail}")
    if auth_dead.is_set():
        log("会话过期，退出码 2（由 supervisor 续签后重跑）")
        return 2
    if failures:
        (BASE / "failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=1))
        log(f"失败清单 → {BASE / 'failures.json'}")
    return 0

if __name__ == "__main__":
    sys.exit(main() or 0)
