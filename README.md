# xiaomi2icloud

把**小米云相册**同步到 **iCloud 照片**的一站式工具链：既支持**一次性全量迁移**，也支持**守护进程自动增量同步**（新增照片自动下载、转码、上传，可 systemd / launchd / 任务计划常驻）。

全程本地运行，不经过任何第三方服务器；两端的登录态只保存在本地 cookie 文件里。

## 功能

- **全量迁移**：登录 → 下载（SHA1 逐文件校验）→ HEIC/MP4 转码 → 分批上传 iCloud → CloudKit 对账
- **自动增量同步**：`daemon.py` 定时轮询小米云，新增照片自动走完整流水线；**只增不删**（绝不删 iCloud 里的任何内容）
- **断点续传**：每一步幂等可重跑，中断了直接重启，进度都在
- **自动续签 + 通知**：会话过期自动无头续签；彻底失效（需要短信/双重验证码）时通过 `notify_command` 提醒人工登录一次，之后自动恢复
- **跨平台**：Linux / macOS / Windows（Python + Playwright + ffmpeg），各平台开机常驻方案见 `deploy/`

## 为什么需要它

- 小米云相册免费空间有限、且不提供官方的全量导出 + 迁移到 iCloud 的途径；
- iCloud（中国区 `icloud.com.cn`）没有公开的照片上传 API，`pyicloud` 等库不支持国区；
- 网页端手动拖几万张照片不现实——浏览器会崩、会断、断点无从谈起。

## 流水线

```
        ┌──────────────────────────── daemon.py（定时循环）──────────────────────────┐
        │                                                                            │
        │   ①登录(一次)      ②增量下载           ③转码                ④分批上传        │
        │  mi_login.py   mi_download/sync   heic/mov_convert    icloud_upload.py     │
        │  icloud_login  （按ID/SHA1对比）   （HEIC→JPG,MP4→MOV） （面板状态判定）      │
        │                                                                            │
        │                          ⑤对账: icloud_recon.py（手动）                      │
        └────────────────────────────────────────────────────────────────────────────┘
```

## 安装

```bash
git clone https://github.com/QSMX816/xiaomi2icloud.git
cd xiaomi2icloud
pip install -r requirements.txt
playwright install chromium

# MP4 → MOV 需要 ffmpeg
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
```

## 快速开始：先完成一次登录

```bash
python mi_login.py        # 弹出浏览器 → 小米账号 + 短信验证码
python icloud_login.py    # 弹出浏览器 → Apple ID + 双重认证验证码
```

两个登录各只需一次；之后 cookie 过期由同步流程自动无头续签。

## 用法 A：自动定时同步（推荐）

```bash
cp config.example.json config.json   # 按需修改（间隔、并发、通知命令）
python sync.py --dry-run             # 检查将做什么，不联网不改动
python daemon.py                     # 前台常驻，Ctrl+C 退出
```

首轮会执行完整迁移（数量大时以天计，可随时中断重跑），之后每轮只处理增量。
同步期间弹出 Chromium 窗口属正常现象（iCloud 上传/续签需要真实浏览器）。

### 配置说明（config.json）

| 字段 | 默认 | 说明 |
|---|---|---|
| `interval_minutes` | 60 | 每轮间隔（分钟） |
| `download_workers` | 3 | 下载并发线程 |
| `upload_batch` | 1000 | 上传单批文件数上限（网页端上限 1000） |
| `upload_headless` | false | 上传浏览器无头模式（**风控敏感，慎开**） |
| `login_wait_seconds` | 90 | 等待人工完成登录的秒数 |
| `mi_album_id` | "1" | 小米云相册 ID（"1" 为全部照片） |
| `notify_command` | [] | 失败/需登录时的通知命令，支持 `{title}` `{body}` 占位 |

通知命令示例：

```json
"notify_command": ["notify-send", "{title}", "{body}"]
"notify_command": ["osascript", "-e", "display notification \"{body}\" with title \"{title}\""]
```

### 开机常驻

| 系统 | 方案 | 文件 |
|---|---|---|
| Linux | systemd 用户服务（守护模式，推荐） | `deploy/xiaomi2icloud.service` |
| Linux | systemd timer（每小时单轮，替代方案） | `deploy/xiaomi2icloud-sync.service` + `deploy/xiaomi2icloud.timer` |
| macOS | launchd | `deploy/com.qsmx816.xiaomi2icloud.plist` |
| Windows | 任务计划 / 启动文件夹 | 见 `deploy/windows.md` |

Linux systemd 示例：

```bash
mkdir -p ~/.config/systemd/user
cp deploy/xiaomi2icloud.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now xiaomi2icloud
journalctl --user -u xiaomi2icloud -f      # 看日志
```

### 会话过期怎么办

- 小米云 `passToken`、iCloud 登录态在有效期内都能**自动无头续签**，无需人工；
- 彻底失效时（需短信验证码 / 双重认证，无法自动化），同步会通过 `notify_command` 提醒你：
  到项目目录手动跑一次 `python mi_login.py` 或 `python icloud_login.py`，之后守护自动恢复。

## 用法 B：手动分步（一次性迁移 / 排查）

### 1. 全量下载小米云相册

```bash
python mi_download.py                # 全量
python mi_download.py --workers 5    # 并发 5 线程（默认 3）
```

SHA1 逐文件校验、按云端拍摄时间回写 mtime、JPEG 缺 `DateTimeOriginal` 时补写；
清单写入 `manifest.jsonl`，失败项汇总 `failures.json`；会话过期以退出码 2 结束，重跑即续传。

### 2. 转码（iCloud 网页上传不支持 HEIC / MP4）

```bash
python heic_convert.py    # photos/*.HEIC → converted/*.jpg（保留 EXIF/GPS/ICC/mtime）
python mov_convert.py     # photos/*.mp4  → converted_mov/*.mov（ffmpeg 无损重封装）
```

### 3. 上传 iCloud

```bash
python icloud_upload.py              # 单批 ≤1000 件，自动做转码路径替换
python icloud_upload.py --batch 200  # 网络差时调小批次
```

Playwright 驱动真实 Chromium；完成判定以上传面板状态稳定为准；“重复项目”计为完成；
已传清单在 `icloud-upload-state.json`，重跑自动跳过；超时批次留待下轮，不会误标。

### 4. 对账验证（可选）

```bash
python icloud_recon.py
```

清空 iCloud 页面 IndexedDB（保留 cookie）强制全量重同步，旁听 CloudKit 响应解出云端
全部文件名，与本地 `photos/` 对账，报告“本地 N 张、已在云 M 张、仍需上传 K 张”。

诊断工具：`icloud_ck_dump.py`（旁听保存 CloudKit 原始响应）、`icloud_verify.py`（DOM 抽样核对）。

## 注意事项

- **凭据安全**：`cookies.json` / `icloud-cookies.json` / `browser-profile/` / `icloud-profile*/` 等于两端账号登录态，**不要提交、不要截图、不要发给别人**（`.gitignore` 已排除）。
- **只增不删**：同步方向为小米云 → iCloud 单向追加；小米端删除照片不会同步，也绝不会删除 iCloud 里的内容。
- **限速现实**：上传速度取决于 iCloud 网页端，几万张照片请按天计耐心；所有步骤可断点续传，中断直接重跑。
- 本项目为个人数据同步用途的非官方工具，与小米/Apple 无关；使用即自担相应服务条款风险。

## 技术笔记

- 小米云相册走 `i.mi.com/gallery` 的 H5 API：`user/galleries` 分页列举、`gallery/storage` 换取 JSONP 下载直链（POST `meta=` 换签名 URL），下载 URL 短时效、需带 meta 重签；
- iCloud 中国区网页端照片是 CloudKit JS 应用（iframe `photos3`），上传走 `photosupload/putAsset`；实测网页上传单批上限 1000 件，且 HEIC/MP4 会被拒收；
- 网页上传接口不提供指定“添加日期”的入口（曾实验拦截 `putAsset` 注入 `addedDate`，未得到可复现的结果）；照片在云端的归档日期取决于自身元数据——所以本工具链拼命保留 EXIF 与 mtime，JPEG 缺失的 `DateTimeOriginal` 还会按小米云端的记录补写。

## License

[MIT](LICENSE)
