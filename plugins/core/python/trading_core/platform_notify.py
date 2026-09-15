"""可选桌面通知：探测平台命令，缺失返回 None（YAGNI：不发邮件/短信）。"""
import shutil


def detect():
    for cmd in ("osascript", "notify-send", "kdialog"):
        if shutil.which(cmd):
            return cmd
    return None
