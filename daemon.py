"""xiaomi2icloud 守护进程：按配置间隔循环执行增量同步（sync.py）

- 首轮立即执行，之后每 interval_minutes 分钟一轮（带随机抖动）
- 单实例：sync.py 自带文件锁，定时器/手动同时触发也不会并发
- 失败通知：任一轮失败时执行 config.json 的 notify_command（如 notify-send）
- Ctrl+C / SIGTERM 后等当前动作结束再退出，进度不丢（所有步骤可断点续传）

用法:
  python daemon.py                # 常驻循环（前台）
  python daemon.py --once         # 只跑一轮（给 systemd timer / cron / 任务计划用）
  python daemon.py --interval 30  # 临时改间隔（分钟）
"""
import argparse
import random
import signal
import subprocess
import sys
import time

from sync import BASE, load_config, log, notify

stop = False

def _handle(sig, _frm):
    global stop
    stop = True
    log(f"收到信号 {sig}，当前动作结束后退出")

def one_pass(cfg):
    rc = subprocess.run([sys.executable, str(BASE / "sync.py")]).returncode
    if rc == 0:
        log("✓ 本轮同步完成")
    elif rc == 75:
        log("上一轮同步仍在进行，跳过")
    else:
        log("✗ 本轮同步存在失败步骤")
        notify(cfg, "xiaomi2icloud 同步出现问题",
               "详见 sync.log 与 icloud-upload.log；若提示登录失效请手动 login 一次")
    return rc

def main():
    ap = argparse.ArgumentParser(description="xiaomi2icloud 自动同步守护进程")
    ap.add_argument("--once", action="store_true", help="只执行一轮后退出")
    ap.add_argument("--interval", type=int, help="覆盖 config.json 的 interval_minutes")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    cfg = load_config()
    if args.once:
        return one_pass(cfg)

    interval = args.interval or int(cfg.get("interval_minutes", 60))
    log(f"守护启动: 每 {interval} 分钟一轮，首轮立即执行（Ctrl+C 退出）")
    while not stop:
        one_pass(cfg)
        jitter = random.uniform(0, min(120, interval * 6))
        end = time.time() + interval * 60 + jitter
        while not stop and time.time() < end:
            time.sleep(5)
    log("守护退出")
    return 0

if __name__ == "__main__":
    sys.exit(main())
