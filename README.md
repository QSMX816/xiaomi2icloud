# xiaomi2icloud

把**小米云相册**全量迁移到 **iCloud 照片**的一站式工具链：登录 → 全量下载（校验+断点续传）→ HEIC/MP4 转码 → 分批上传 iCloud → 云端对账验证。

全程本地运行，不经过任何第三方服务器；两端的登录态只保存在本地 cookie 文件里。

## 为什么需要它

- 小米云相册免费空间有限、且不提供官方的全量导出 + 迁移到 iCloud 的途径；
- iCloud（中国区 `icloud.com.cn`）没有公开的照片上传 API，`pyicloud` 等库不支持国区；
- 网页端手动拖几万张照片不现实——浏览器会崩、会断、断点无从谈起。

本工具链把整件事拆成 5 个可断点续传的步骤，每一步都可以单独重跑。

## 迁移流水线

```
┌─────────────┐   ┌──────────────┐   ┌───────────────┐   ┌─────────────┐   ┌──────────────┐
│ 1. 登录小米云 │ → │ 2. 全量下载   │ → │ 3. 转码        │ → │ 4. 登录iCloud │ → │ 5. 分批上传    │
│  mi_login.py │   │ mi_download.py│   │ heic/mov_convert│  │ icloud_login │   │ icloud_upload │
└─────────────┘   └──────────────┘   └───────────────┘   └─────────────┘   └──────────────┘
                                                                  ↓
                                                    6. 对账: icloud_recon.py
```

## 安装

```bash
pip install -r requirements.txt
playwright install chromium

# MP4 → MOV 需要 ffmpeg
sudo apt install ffmpeg        # Debian/Ubuntu
brew install ffmpeg            # macOS
```

## 使用

所有脚本都在项目根目录下运行，数据也都落在根目录（`photos/`、`converted/` 等）。

### 1. 登录小米云（人工一次）

```bash
python mi_login.py
```

弹出浏览器，手机号 + 短信验证码登录 `i.mi.com`，检测到登录态后自动保存 `cookies.json`。
之后 cookie 过期时可用 `python mi_login.py --refresh` 无头静默续签（passToken 仍有效时无需人工）。

### 2. 全量下载小米云相册

```bash
python mi_download.py                # 全量
python mi_download.py --workers 5    # 并发 5 线程（默认 3）
```

- SHA1 逐文件校验，重跑自动跳过已下载且校验通过的文件；
- 按云端的拍摄时间回写文件 mtime；JPEG 缺 `DateTimeOriginal` 时按云端 EXIF 补写；
- 下载清单写入 `manifest.jsonl`，失败项汇总到 `failures.json`；
- 会话过期时以退出码 2 结束，续签后直接重跑即可续传。

### 3. 转码（iCloud 网页上传不支持 HEIC / MP4）

```bash
python heic_convert.py    # photos/*.HEIC → converted/*.jpg（保留 EXIF/GPS/ICC/mtime）
python mov_convert.py     # photos/*.mp4  → converted_mov/*.mov（ffmpeg 无损重封装）
```

### 4. 登录 iCloud（人工一次）

```bash
python icloud_login.py
```

弹出浏览器，登录 Apple ID（含双重认证验证码），保存 `icloud-cookies.json`。
同样支持 `--refresh` 静默续签。

### 5. 分批上传 iCloud 照片

```bash
python icloud_upload.py              # 默认每批 ≤1000 件且 ≤3GB
python icloud_upload.py --batch 200  # 网络差时调小批次
```

工作原理（Playwright 驱动真实 Chromium）：

- 自动做 HEIC→`converted/*.jpg`、MP4→`converted_mov/*.mov` 的路径替换，队列里直接放原始文件即可；
- 每批提交前重载页面，清掉面板残留并刷新会话；
- **完成判定以图库计数增量为准**（“xx 张照片，xx 个视频”），面板文本里的“重复项目/不支持/失败”单独归类；
- 已上传清单记在 `icloud-upload-state.json`，重跑自动跳过；超时的批次留待下轮，不会误标完成。

上传器是有界面的（`headless=False`）：iCloud 风控对人机特征敏感，保留真实窗口最稳。

### 6. 对账验证（可选）

```bash
python icloud_recon.py
```

清空 iCloud 页面的 IndexedDB（保留 cookie）强制重新全量同步，旁听 CloudKit 查询响应，解出云端全部文件名，与本地 `photos/` 对账，报告“本地 N 张、已在云 M 张、仍需上传 K 张”。

诊断工具：

| 脚本 | 用途 |
|---|---|
| `icloud_ck_dump.py` | 旁听并保存 iCloud 照片的 CloudKit 同步响应（看原始 API） |
| `icloud_verify.py` | 滚动照片网格，从 DOM 里提取文件名做抽样核对 |

## 注意事项

- **凭据安全**：`cookies.json` / `icloud-cookies.json` / `browser-profile/` / `icloud-profile*/` 等于两端的账号登录态，**不要提交、不要截图、不要发给别人**（`.gitignore` 已排除）。
- **限速现实**：上传速度取决于 iCloud 网页端，几万张照片请按天计耐心；工具全部可断点续传，中断了直接重跑。
- **重复照片**：iCloud 按“重复项目”提示去重，上传器把重复计为完成，不会死循环。
- 本项目为个人数据迁移用途的非官方工具，与小米/Apple 无关；使用即自担相应服务条款风险。

## 技术笔记

- 小米云相册走 `i.mi.com/gallery` 的 H5 API：`user/galleries` 分页列举、`gallery/storage` 换取 JSONP 下载直链（POST `meta=` 换签名 URL），下载 URL 短时效、需带 meta 重签；
- iCloud 中国区网页端照片是 CloudKit JS 应用（iframe `photos3`），上传走 `photosupload/putAsset`；实测网页上传单批上限 1000 件，且 HEIC/MP4 会被拒收；
- 网页上传接口不提供指定“添加日期”的入口（曾实验拦截 `putAsset` 注入 `addedDate`，未得到可复现的结果）；照片在云端的归档日期取决于自身元数据——所以本工具链拼命保留 EXIF 与 mtime，JPEG 缺失的 `DateTimeOriginal` 还会按小米云端的记录补写。

## License

[MIT](LICENSE)
