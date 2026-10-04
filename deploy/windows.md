# Windows 部署

## 前置

1. 安装 [Python 3](https://www.python.org/downloads/)（勾选 Add to PATH）
2. 安装 [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) 并加入 PATH
3. `pip install -r requirements.txt && playwright install chromium`
4. 完成两次登录：`python mi_login.py`、`python icloud_login.py`

## 方式一：任务计划程序（每小时单轮同步）

以管理员身份运行 CMD：

```bat
schtasks /Create /TN "xiaomi2icloud" /TR "python C:\tools\xiaomi2icloud\sync.py" /SC HOURLY
```

（把 `C:\tools\xiaomi2icloud` 换成你的克隆路径；`python` 解析不到时用绝对路径如
`C:\Python312\python.exe`。）

管理：`schtasks /Run /TN "xiaomi2icloud"`、`schtasks /Delete /TN "xiaomi2icloud"`

## 方式二：登录自启守护进程

1. `Win+R` 输入 `shell:startup` 回车，打开启动文件夹
2. 在其中新建快捷方式，目标填：

```
C:\Python312\pythonw.exe C:\tools\xiaomi2icloud\daemon.py
```

（`pythonw` 无控制台窗口；间隔在 `config.json` 的 `interval_minutes` 里调。）

注意：iCloud 上传需要真实浏览器窗口（防风控），同步时弹出 Chromium 属正常现象；
彻底后台化需在 `config.json` 里设 `"upload_headless": true`（有账号风控风险，自行权衡）。
