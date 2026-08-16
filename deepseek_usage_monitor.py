#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek API 用量实时监控 —— 桌面悬浮窗（多 API + 系统托盘）
================================================================

实时显示 platform.deepseek.com/usage 的核心用量信息：
    充值余额 / 所选时间范围消费金额 / 请求次数 / Token 量

特性：
  · 支持多个 API Key：按「DS账号名称」分组，托盘菜单两层切换；窗口下拉切换账号总量/各 key
  · 从平台响应动态发现模型，可按模型筛选消费、请求次数和 Token 用量
  · 支持今日、昨日、近 7 天、近 30 天、本月、上月六种时间维度
  · 最小化到系统托盘（需 pystray + Pillow；未安装时退回普通窗口行为）
  · 每 15 秒自动刷新，窗口置顶、可拖动

数据源（公开/前端同款内部接口，多个开源项目验证可用）：
  1. 余额   GET https://api.deepseek.com/user/balance              （需 API Key）
  2. 用量   GET https://platform.deepseek.com/api/v0/usage/*       （需平台 userToken，可选）

凭证说明：
  - API Key：在 platform.deepseek.com → API Keys 页面创建。
  - userToken（可选但推荐）：Chrome 登录 platform.deepseek.com 后按 F12，
    在控制台执行  JSON.parse(localStorage.getItem('userToken')).value  复制结果填入配置。
    没有 userToken 时，今日消费按"当日余额差值"估算。

依赖：仅标准库；托盘功能可选安装  pip install pystray pillow
运行：python deepseek_usage_monitor.py   （打包见 README）
"""

import base64
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import hashlib
from datetime import datetime, date, timedelta, timezone
import tkinter as tk
from tkinter import messagebox, ttk

try:
    import winreg
except ImportError:                         # 非 Windows 环境仅用于源码检查/测试
    winreg = None

try:                                    # 可选：系统托盘
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except Exception:
    HAS_TRAY = False

APP_NAME = "DSAPI-Monitor"
VERSION = "V1.4.1"
LANG_ZH = "zh_CN"
LANG_EN = "en_US"
DEFAULT_LANG = LANG_ZH
VALID_LANGUAGES = (LANG_ZH, LANG_EN)
ALL_LABELS = {LANG_ZH: "账号总量", LANG_EN: "Account Total"}
ALL_MODELS_LABELS = {LANG_ZH: "所有模型", LANG_EN: "All Models"}
LANGUAGE_MENU_LABEL = "language"
LANGUAGE_MENU_OPTIONS = (("中文", LANG_ZH), ("English", LANG_EN))
TIME_TODAY = "today"
TIME_YESTERDAY = "yesterday"
TIME_LAST_7_DAYS = "last_7_days"
TIME_LAST_30_DAYS = "last_30_days"
TIME_THIS_MONTH = "this_month"
TIME_LAST_MONTH = "last_month"
DEFAULT_TIME_DIMENSION = TIME_TODAY
TIME_DIMENSIONS = (
    (TIME_TODAY, "time_today"),
    (TIME_YESTERDAY, "time_yesterday"),
    (TIME_LAST_7_DAYS, "time_last_7_days"),
    (TIME_LAST_30_DAYS, "time_last_30_days"),
    (TIME_THIS_MONTH, "time_this_month"),
    (TIME_LAST_MONTH, "time_last_month"),
)
VALID_TIME_DIMENSIONS = tuple(key for key, _ in TIME_DIMENSIONS)
CN_TZ = timezone(timedelta(hours=8))   # 默认 GMT+8
TIMEZONES = {"GMT+8": CN_TZ, "UTC+0": timezone.utc}   # 可切换的时区
DEFAULT_TZ = "GMT+8"
BG = "#000000"        # 窗口底色（纯黑）
COMBO_BG = "#4a6984"  # clam 主题原生的只读聚焦灰蓝色
COMBO_FG = "#ffffff"
COMBO_SELECT_BG = "#5b7d99"
MIN_WIDTH = 300       # 最小窗口宽度，保证字符显示完整
# 程序文件目录只用于首次迁移旧版配置；V1.1 起用户数据统一放 AppData。
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
BUNDLE_DIR = getattr(sys, "_MEIPASS", HERE)
APP_ICON_SOURCE = os.path.join(BUNDLE_DIR, "app_icon_source_v2.png")
APP_DATA_DIR = os.path.join(
    os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"),
    APP_NAME,
)
CONFIG_FILE = os.path.join(APP_DATA_DIR, "dsm_config.json")
STATE_FILE = os.path.join(APP_DATA_DIR, "dsm_state.json")   # 余额差值估算的当日基准
LEGACY_CONFIG_FILE = os.path.join(HERE, "dsm_config.json")
LEGACY_STATE_FILE = os.path.join(HERE, "dsm_state.json")
SECRET_PREFIX = "dpapi:"
STARTUP_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_REG_NAME = APP_NAME

DEFAULT_CONFIG = {
    "accounts": [
        {"ds_account": "默认", "name": "默认", "api_key": "", "platform_token": ""},
    ],
    "refresh_seconds": 30,            # 刷新间隔（秒，最小 5）
    "timezone": DEFAULT_TZ,
    "pin_corner": True,
    "language": DEFAULT_LANG,
}

PLATFORM_BASE = "https://platform.deepseek.com/api/v0"
PLATFORM_HEADERS = {
    "Accept": "application/json",
    "x-app-version": "1.0.0",
    "Origin": "https://platform.deepseek.com",
    "Referer": "https://platform.deepseek.com/usage",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"),
}
TIMEOUT = 20

TEXTS = {
    LANG_ZH: {
        "ds_account": "DS账号名称：", "model": "模型：", "balance": "充值余额", "granted": "赠送",
        "today_cost": "今日消费", "today_requests": "今日API请求次数",
        "today_tokens": "今日Tokens", "starting": "启动中…",
        "show_hide": "显示 / 隐藏", "refresh": "立即刷新", "switch_account": "切换账号",
        "settings": "设置账号信息", "timezone": "时区", "time_dimension": "时间维度",
        "time_today": "今日", "time_yesterday": "昨日", "time_last_7_days": "近 7 天",
        "time_last_30_days": "近 30 天", "time_this_month": "本月", "time_last_month": "上月",
        "range_cost": "{range}消费", "range_requests": "{range}API请求次数",
        "range_tokens": "{range}Tokens", "topmost": "窗口置顶",
        "pin_corner": "固定右下角", "startup": "开机自启",
        "minimize_tray": "最小化到托盘", "exit": "退出",
        "token_help": "userToken 获取帮助", "open_config": "打开配置目录",
        "open_config_error": "无法打开配置目录：{error}",
        "startup_error": "开机自启设置失败：{error}",
        "tray_hint": "已最小化到系统托盘。\n双击托盘图标可恢复窗口；要彻底退出请用托盘菜单或右键菜单的「退出」。",
        "official_tag": "[官方]", "estimate_tag": "[≈估算]", "mixed_tag": "[混合口径]",
        "unknown_tag": "[口径未知]", "stale_tag": "[上次数据]",
        "fetch_no_data": "[获取失败/无数据]", "need_token": "[需 userToken 显示官方数据]",
        "input": "输入", "output": "输出", "cache_hit": "缓存命中",
        "coverage": "用量覆盖 {covered}/{total} 个平台账号",
        "official_mode": "官方数据", "estimate_mode": "估算模式", "mixed_mode": "混合口径",
        "waiting": "等待数据", "last_success": "上次成功值", "api_count": "共 {count} 个API",
        "updated": "更新 {time}", "api_tooltip_count": "（{count} 个API）",
        "settings_title": "设置账号信息", "show_secrets": "显示凭证", "global": "全局",
        "search_hint": "🔍 搜索：DS账号名称 / API Key名称（模糊匹配）", "account_list": "账户列表",
        "add": "＋ 添加", "delete": "－ 删除", "move_up": "↑ 上移", "move_down": "↓ 下移",
        "ds_account_field": "DS账号名称（显示用）",
        "key_name_field": "API Key名称（显示用）", "api_key_field": "API Key（必填，余额）",
        "token_field": "平台 userToken（可留空）", "refresh_field": "刷新间隔（秒，最小5）",
        "pin_field": "固定在桌面右下角（固定后不可拖动）", "saved": "已保存 ✓",
        "save": "保存", "cancel": "取消", "keep_one": "至少保留一个账户",
        "refresh_number": "刷新间隔必须是数字", "save_failed": "配置文件写入失败",
        "default": "默认", "new_key": "新Key", "warning_join": "；",
        "multi_token": "同一 DS账号 配置了多个不同 userToken；已按多个平台账号合计",
        "stale_warning": "{name}: 显示当前时间范围的上次成功数据", "balance_error": "{name}: 余额: {error}",
        "missing_key": "{name}: 未配置 API Key", "usage_error": "{name}: 所选范围用量: {error}",
        "no_key_detail": "当前接口未返回该 Key 的可验证明细",
        "cannot_split": "当前无法可靠拆分这把 Key 的用量",
        "no_usage_data": "用量接口无数据（token 可能已过期）",
        "no_data": "无数据", "internal_error": "内部错误: {error}",
    },
    LANG_EN: {
        "ds_account": "DS Account: ", "model": "Model: ", "balance": "Top-up Balance", "granted": "Granted",
        "today_cost": "Today's Cost", "today_requests": "Today's API Requests",
        "today_tokens": "Today's Tokens", "starting": "Starting…",
        "show_hide": "Show / Hide", "refresh": "Refresh Now", "switch_account": "Switch Account",
        "settings": "Account Settings", "timezone": "Timezone", "time_dimension": "Time Range",
        "time_today": "Today", "time_yesterday": "Yesterday", "time_last_7_days": "Last 7 Days",
        "time_last_30_days": "Last 30 Days", "time_this_month": "This Month",
        "time_last_month": "Last Month", "range_cost": "{range} Cost",
        "range_requests": "{range} API Requests", "range_tokens": "{range} Tokens",
        "topmost": "Always on Top",
        "pin_corner": "Pin to Bottom-right", "startup": "Start with Windows",
        "minimize_tray": "Minimize to Tray", "exit": "Exit",
        "token_help": "How to Get userToken", "open_config": "Open Config Folder",
        "open_config_error": "Could not open config folder: {error}",
        "startup_error": "Could not update Windows startup setting: {error}",
        "tray_hint": "Minimized to the system tray.\nDouble-click the tray icon to restore the window. Use Exit in the tray or context menu to quit.",
        "official_tag": "[Official]", "estimate_tag": "[≈Estimated]", "mixed_tag": "[Mixed]",
        "unknown_tag": "[Unknown Source]", "stale_tag": "[Previous Data]",
        "fetch_no_data": "[Failed / No Data]", "need_token": "[userToken Required]",
        "input": "Input", "output": "Output", "cache_hit": "Cache Hit",
        "coverage": "Usage Coverage {covered}/{total} Accounts",
        "official_mode": "Official Data", "estimate_mode": "Estimated Mode", "mixed_mode": "Mixed Sources",
        "waiting": "Waiting for Data", "last_success": "Last Successful Value", "api_count": "{count} APIs",
        "updated": "Updated {time}", "api_tooltip_count": " ({count} APIs)",
        "settings_title": "Account Settings", "show_secrets": "Show Credentials", "global": "Global",
        "search_hint": "🔍 Search: DS Account / API Key Name", "account_list": "Accounts",
        "add": "+ Add", "delete": "− Delete", "move_up": "↑ Move Up",
        "move_down": "↓ Move Down", "ds_account_field": "DS Account Name",
        "key_name_field": "API Key Name", "api_key_field": "API Key (required for balance)",
        "token_field": "Platform userToken (optional)", "refresh_field": "Refresh Interval (seconds, min 5)",
        "pin_field": "Pin to desktop bottom-right (dragging is disabled while pinned)", "saved": "Saved ✓",
        "save": "Save", "cancel": "Cancel", "keep_one": "Keep at least one account",
        "refresh_number": "Refresh interval must be a number", "save_failed": "Could not write config file",
        "default": "Default", "new_key": "New Key", "warning_join": "; ",
        "multi_token": "This DS Account has multiple userTokens; totals include multiple platform accounts",
        "stale_warning": "{name}: showing the last successful value for this time range",
        "balance_error": "{name}: balance: {error}", "missing_key": "{name}: API Key is not configured",
        "usage_error": "{name}: selected-range usage: {error}",
        "no_key_detail": "The API did not return verifiable details for this key",
        "cannot_split": "Usage for this key cannot be separated reliably",
        "no_usage_data": "No usage data (the token may have expired)",
        "no_data": "No data", "internal_error": "Internal error: {error}",
    },
}


def tr(lang, key, **kwargs):
    table = TEXTS.get(lang) or TEXTS[DEFAULT_LANG]
    value = table.get(key) or TEXTS[DEFAULT_LANG].get(key) or key
    return value.format(**kwargs) if kwargs else value


def usertoken_help(lang):
    if lang == LANG_EN:
        return (
            "How to get the platform userToken (for official cost and token usage):\n\n"
            "1. Open https://platform.deepseek.com/usage in Chrome and sign in.\n"
            "2. Press F12 and open the Console tab.\n"
            "3. Run:\n     JSON.parse(localStorage.getItem('userToken')).value\n"
            "4. Copy the result into the userToken field in this app.\n\n"
            "The token is equivalent to your signed-in web session. Do not share it."
        )
    return (
        "如何获取平台 userToken（用于官方今日消费/Tokens 数据）：\n\n"
        "1. 用 Chrome 打开 https://platform.deepseek.com/usage 并登录\n"
        "2. 按 F12 打开开发者工具，切到「控制台(Console)」\n"
        "3. 执行：\n     JSON.parse(localStorage.getItem('userToken')).value\n"
        "4. 复制输出并粘贴到本程序的 userToken 输入框\n\n"
        "该 token 等同于平台登录态，请勿泄露给他人。"
    )


# ─────────────────────────── 基础工具 ───────────────────────────

def startup_command():
    """返回当前程序写入 HKCU Run 的启动命令。"""
    if getattr(sys, "frozen", False):
        args = [os.path.abspath(sys.executable)]
    else:
        args = [os.path.abspath(sys.executable), os.path.abspath(__file__)]
    return subprocess.list2cmdline(args)


def is_startup_enabled():
    """仅当当前程序路径已登记时才显示为已启用，避免旧路径误报。"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, STARTUP_REG_NAME)
        return os.path.normcase(str(value).strip()) == os.path.normcase(startup_command())
    except OSError:
        return False


def set_startup_enabled(enabled):
    """为当前 Windows 用户添加或删除开机登录启动项。"""
    if winreg is None:
        raise OSError("Windows registry is unavailable")
    if enabled:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH) as key:
            winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, startup_command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, STARTUP_REG_NAME)
    except FileNotFoundError:
        pass


def usage_date_range(time_dimension=DEFAULT_TIME_DIMENSION, tz=CN_TZ, now=None):
    """返回所选时间维度的闭区间 (开始日期, 结束日期)。近 N 天包含今天。"""
    if time_dimension not in VALID_TIME_DIMENSIONS:
        time_dimension = DEFAULT_TIME_DIMENSION
    if now is None:
        current = datetime.now(tz)
    elif isinstance(now, datetime):
        current = now.replace(tzinfo=tz) if now.tzinfo is None else now.astimezone(tz)
    else:
        current = datetime.combine(now, datetime.min.time(), tzinfo=tz)
    today = current.date()
    if time_dimension == TIME_YESTERDAY:
        day = today - timedelta(days=1)
        return day, day
    if time_dimension == TIME_LAST_7_DAYS:
        return today - timedelta(days=6), today
    if time_dimension == TIME_LAST_30_DAYS:
        return today - timedelta(days=29), today
    if time_dimension == TIME_THIS_MONTH:
        return today.replace(day=1), today
    if time_dimension == TIME_LAST_MONTH:
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    return today, today


def months_in_range(start_date, end_date):
    """按顺序列出日期闭区间覆盖到的 (year, month)。"""
    months = []
    cursor = start_date.replace(day=1)
    last = end_date.replace(day=1)
    while cursor <= last:
        months.append((cursor.year, cursor.month))
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def time_dimension_label(lang, time_dimension):
    labels = dict(TIME_DIMENSIONS)
    return tr(lang, labels.get(time_dimension, labels[DEFAULT_TIME_DIMENSION]))

def to_float(v, default=0.0):
    """宽容地把字符串/数字转 float。"""
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def to_int(v):
    return int(to_float(v))


def short_err(e):
    s = str(e).strip()
    return s[:140] if s else type(e).__name__


def money_symbol(currency):
    c = (currency or "").upper()
    return {"CNY": "¥", "USD": "$", "EUR": "€", "JPY": "¥"}.get(c, (c + " ") if c else "¥")


def fmt_money(v, currency=None, digits=4):
    """金额格式化：自动去尾零；None 显示 —。"""
    if v is None:
        return "—"
    s = f"{v:,.{digits}f}".rstrip("0").rstrip(".")
    return f"{money_symbol(currency)}{s}"


def fmt_multi(group, digits=4):
    """按币种分组的金额 → "¥12.34 + $5.00" 形式；空组显示 —。"""
    if not group:
        return "—"
    return " + ".join(fmt_money(group[cur], cur, digits) for cur in sorted(group))


def fmt_tokens(v):
    return "—" if v is None else f"{v:,}"


def work_area(root):
    """桌面工作区（排除任务栏）→ (left, top, right, bottom)。"""
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return 0, 0, root.winfo_screenwidth(), max(0, root.winfo_screenheight() - 60)


# ─────────────────────────── 配置/状态 ───────────────────────────

def atomic_write_json(path, data, indent=None):
    """同目录临时文件 + os.replace，避免异常退出留下半截 JSON。"""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dsm-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _dpapi_crypt(raw, protect=True):
    """使用当前 Windows 用户的 DPAPI 加/解密；密文换电脑或换用户后不可用。"""
    if os.name != "nt":
        return raw
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_byte))]

    src = ctypes.create_string_buffer(raw)
    src_blob = DATA_BLOB(len(raw), ctypes.cast(src, ctypes.POINTER(ctypes.c_byte)))
    dst_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = fn(ctypes.byref(src_blob), None, None, None, None, 0x1,
                ctypes.byref(dst_blob))
    else:
        ok = fn(ctypes.byref(src_blob), None, None, None, None, 0x1,
                ctypes.byref(dst_blob))
    if not ok:
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 处理失败")
    try:
        return ctypes.string_at(dst_blob.pbData, dst_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst_blob.pbData)


def protect_secret(value):
    value = (value or "").strip()
    if not value:
        return ""
    if os.name != "nt":
        return value
    raw = _dpapi_crypt(value.encode("utf-8"), True)
    return SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


def unprotect_secret(value):
    value = value or ""
    if not value.startswith(SECRET_PREFIX):       # V1.0 明文或非 Windows 配置
        return value
    raw = base64.b64decode(value[len(SECRET_PREFIX):], validate=True)
    return _dpapi_crypt(raw, False).decode("utf-8")

def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _config_for_disk(cfg):
    out = {
        "accounts": [],
        "refresh_seconds": max(5, int(to_float(cfg.get("refresh_seconds"), 30))),
        "timezone": cfg.get("timezone") if cfg.get("timezone") in TIMEZONES else DEFAULT_TZ,
        "pin_corner": bool(cfg.get("pin_corner", True)),
        "language": cfg.get("language") if cfg.get("language") in VALID_LANGUAGES else DEFAULT_LANG,
    }
    for acc in cfg.get("accounts") or []:
        out["accounts"].append({
            "ds_account": (acc.get("ds_account") or "默认").strip() or "默认",
            "name": (acc.get("name") or "默认").strip() or "默认",
            "api_key_protected": protect_secret(acc.get("api_key")),
            "platform_token_protected": protect_secret(acc.get("platform_token")),
        })
    return out


def load_config():
    source = CONFIG_FILE
    if not os.path.exists(source) and os.path.isfile(LEGACY_CONFIG_FILE):
        source = LEGACY_CONFIG_FILE
    cfg = load_json(source)
    migrate = source != CONFIG_FILE

    # v1/v2 迁移：旧版单账号配置（api_key 在顶层）或缺少 ds_account 的旧账户
    if not isinstance(cfg.get("accounts"), list) or not cfg["accounts"]:
        cfg["accounts"] = [{
            "ds_account": "默认",
            "name": "默认",
            "api_key": (cfg.get("api_key") or "").strip(),
            "platform_token": (cfg.get("platform_token") or "").strip(),
        }]
        migrate = True

    accounts = []
    seen = set()
    for acc in cfg["accounts"]:
        if not isinstance(acc, dict):
            continue
        name = (acc.get("name") or "默认").strip() or "默认"
        i = 2
        base = name
        while name in seen:                    # 名称去重，避免快照覆盖
            name = f"{base}({i})"
            i += 1
        seen.add(name)
        try:
            api_key = unprotect_secret(acc.get("api_key_protected") or acc.get("api_key") or "")
            platform_token = unprotect_secret(
                acc.get("platform_token_protected") or acc.get("platform_token") or "")
        except (OSError, ValueError, UnicodeError):
            api_key = ""
            platform_token = ""
        if "api_key" in acc or "platform_token" in acc:
            migrate = True
        accounts.append({
            "ds_account": (acc.get("ds_account") or "").strip() or "默认",
            "name": name,
            "api_key": api_key.strip(),
            "platform_token": platform_token.strip(),
        })
    if not accounts:
        accounts = [dict(DEFAULT_CONFIG["accounts"][0])]
    cfg["accounts"] = accounts
    cfg.setdefault("refresh_seconds", 30)
    cfg.setdefault("timezone", DEFAULT_TZ)   # 时区：GMT+8 / UTC+0
    cfg.setdefault("pin_corner", True)
    if cfg.get("language") not in VALID_LANGUAGES:
        cfg["language"] = DEFAULT_LANG
    if migrate:
        migrated = save_config(cfg)
        if migrated and source == LEGACY_CONFIG_FILE and source != CONFIG_FILE:
            # 清除程序目录中的旧版明文凭证，同时留下可读的迁移位置提示。
            atomic_write_json(source, {"migrated_to": CONFIG_FILE}, indent=2)
    return cfg


def save_config(cfg):
    try:
        return atomic_write_json(CONFIG_FILE, _config_for_disk(cfg), indent=2)
    except (OSError, ValueError):
        return False


def load_state():
    source = STATE_FILE
    if not os.path.exists(source) and os.path.isfile(LEGACY_STATE_FILE):
        source = LEGACY_STATE_FILE
    try:
        with open(source, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            if "accounts" not in d and d.get("date"):     # v1 单账户状态迁移
                d = {"accounts": {"默认": d}}
            if "accounts" not in d:
                d = {"accounts": {}}
            return d
    except (OSError, ValueError):
        pass
    return {"accounts": {}}


def save_state(st):
    atomic_write_json(STATE_FILE, st)


def update_day_state(account, balance_total, tz=CN_TZ):
    """维护某账户"当日开盘余额"，返回当日消费估算 max(0, opening - 当前)。"""
    today = datetime.now(tz).date().isoformat()
    st = load_state()
    accts = st.get("accounts") or {}
    rec = accts.get(account)
    if rec and rec.get("date") == today:
        opening = rec["opening"]
    else:
        opening = rec["last"] if rec and rec.get("last") is not None else balance_total
    accts[account] = {"date": today, "opening": opening, "last": balance_total}
    save_state({"accounts": accts})
    return max(0.0, opening - balance_total)


def prune_state(valid_names):
    """清理已改名/删除账户的残留状态。"""
    try:
        st = load_state()
        accts = st.get("accounts") or {}
        valid = set(valid_names)
        stale = [n for n in accts if n not in valid]
        if stale:
            for n in stale:
                accts.pop(n, None)
            save_state({"accounts": accts})
    except Exception:
        pass


# ─────────────────────────── HTTP ───────────────────────────

def http_get_json(url, headers, timeout=TIMEOUT):
    # urllib.request.urlopen 会缓存首次创建的全局 opener，其中也包含当时的
    # Windows 系统代理。用户运行期间开关 Clash/VPN 后，旧 opener 仍会访问
    # 已关闭的本地代理，必须重启程序才恢复。每次请求新建 opener，确保读取
    # 当前代理设置；重试时也会再次刷新。
    req = urllib.request.Request(url, headers=headers)
    proxies = urllib.request.getproxies()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_get_retry(url, headers, retries=2, timeout=TIMEOUT):
    """带重试的 GET（余额等官方公开接口用）；凭证类错误(401/403)不重试。"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return http_get_json(url, headers, timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise
            last_err = e
            if attempt < retries:
                time.sleep(2.5 + attempt)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2.5 + attempt)
    raise last_err


def fetch_balance(api_key):
    """GET /user/balance → {currency,total,topped_up,granted,available}"""
    data = http_get_retry(
        "https://api.deepseek.com/user/balance",
        {"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    infos = data.get("balance_infos") or []
    if not infos:
        return None
    chosen = None
    for cur in ("CNY", "USD"):            # 优先 CNY / USD
        for info in infos:
            if info.get("currency") == cur:
                chosen = info
                break
        if chosen:
            break
    if chosen is None:
        for info in infos:                # 其次取有余额的
            if to_float(info.get("total_balance")) > 0:
                chosen = info
                break
    if chosen is None:
        chosen = infos[0]
    return {
        "currency": chosen.get("currency") or "CNY",
        "total": to_float(chosen.get("total_balance")),
        "topped_up": to_float(chosen.get("topped_up_balance")),
        "granted": to_float(chosen.get("granted_balance")),
        "available": bool(data.get("is_available", True)),
    }


def platform_get(token, path, params, retries=2):
    """请求平台内部接口；对瞬时失败（429/5xx/网络抖动）自动重试并间隔。"""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{PLATFORM_BASE}/{path}?{qs}"
    headers = dict(PLATFORM_HEADERS)
    headers["Authorization"] = "Bearer " + token
    last_err = None
    for attempt in range(retries + 1):
        try:
            return http_get_json(url, headers)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):      # 凭证类错误不重试
                raise
            last_err = e
            if attempt < retries:
                time.sleep(2.5 + attempt)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2.5 + attempt)
    raise last_err


def get_biz(data):
    """{code:0, data:{biz_code:0, biz_data:...}} → biz_data；失败返回 None。"""
    if not isinstance(data, dict) or data.get("code") != 0:
        return None
    d = data.get("data")
    if not isinstance(d, dict) or d.get("biz_code") != 0:
        return None
    return d.get("biz_data")


# ─────────────────────────── 用量解析 ───────────────────────────

def classify(t):
    t = (t or "").upper()
    if t == "RESPONSE_TOKEN":
        return "output"
    if t == "PROMPT_CACHE_HIT_TOKEN":
        return "cache_hit"
    if t == "PROMPT_CACHE_MISS_TOKEN":
        return "cache_miss"
    if t == "PROMPT_TOKEN":
        return "prompt"
    if t == "REQUEST":
        return "request"
    if t.endswith("_TOKEN"):
        return "prompt"          # 其他 token 类型一律按输入计
    return None


USAGE_FIELDS = ("cost", "tokens", "prompt", "output", "cache_hit", "cache_miss", "requests")


def empty_usage_agg(currency=None):
    return {"cost": 0.0, "tokens": 0, "prompt": 0, "output": 0,
            "cache_hit": 0, "cache_miss": 0, "requests": 0,
            "currency": currency, "cost_available": False,
            "usage_available": False}


def merge_usage_agg(dst, src):
    """合并两份金额/Token/请求统计；currency 采用首个非空值。"""
    for key in USAGE_FIELDS:
        dst[key] = dst.get(key, 0) + src.get(key, 0)
    dst["currency"] = dst.get("currency") or src.get("currency")
    dst["cost_available"] = bool(dst.get("cost_available") or src.get("cost_available"))
    dst["usage_available"] = bool(dst.get("usage_available") or src.get("usage_available"))
    return dst


def model_name(record):
    """从平台响应条目中提取模型名；兼容字符串与常见对象字段。"""
    if not isinstance(record, dict):
        return None
    value = (record.get("model") or record.get("model_name")
             or record.get("model_id") or record.get("modelName"))
    if isinstance(value, dict):
        value = (value.get("name") or value.get("model_name") or value.get("id")
                 or value.get("model") or value.get("value"))
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _flatten_usage(data):
    """把 day/bucket 的 data 列表展开成 usage 明细列表。
    两种形态都可能出现：
      [{model:..., usage:[{type,amount},...]}]   → 展开 usage
      [{type:..., amount:...}, ...]              → 原样
    """
    out = []
    for e in data or []:
        if not isinstance(e, dict):
            continue
        usage = e.get("usage") or e.get("usages")
        if isinstance(usage, list):
            out.extend(u for u in usage if isinstance(u, dict))
        elif "type" in e:
            out.append(e)
    return out


def sum_usage(entries, mode):
    """汇总一条 usage 列表。
    mode='tokens'：只累计 TOKEN 类型（REQUEST 单独计数）；
    mode='cost'  ：金额型接口，累计全部 amount。
    返回 {cost, tokens, prompt, output, cache_hit, cache_miss, requests}
    prompt 为全部输入侧 token（未命中 + 缓存命中）。
    """
    agg = {"cost": 0.0, "tokens": 0, "prompt": 0, "output": 0,
           "cache_hit": 0, "cache_miss": 0, "requests": 0}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        amt = to_float(e.get("amount"))
        if mode == "cost":
            agg["cost"] += amt
            continue
        kind = classify(e.get("type"))
        if kind == "request":
            agg["requests"] += to_int(amt)
        elif kind == "output":
            n = to_int(amt)
            agg["tokens"] += n
            agg["output"] += n
        elif kind == "cache_hit":
            n = to_int(amt)
            agg["tokens"] += n
            agg["cache_hit"] += n
            agg["prompt"] += n
        elif kind == "cache_miss":
            n = to_int(amt)
            agg["tokens"] += n
            agg["cache_miss"] += n
            agg["prompt"] += n
        elif kind == "prompt":
            n = to_int(amt)
            agg["tokens"] += n
            agg["prompt"] += n
    return agg


def bucket_date(v):
    """把 bucket 的 time/date/timestamp 归一成 YYYY-MM-DD。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = float(v)
        if n <= 0:
            return None
        sec = n / 1000 if n > 1e12 else n
        try:
            return datetime.utcfromtimestamp(sec).strftime("%Y-%m-%d")
        except (ValueError, OverflowError, OSError):
            return None
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(v).strip())
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def classic_day_maps(biz):
    """经典按月份接口：biz 为 {days:[{date,data:[...]}], total:[...], currency} 或数组。
    返回 (day_map, model_day_maps, month_total_entries, currency)。"""
    container = biz if isinstance(biz, dict) else (biz[0] if isinstance(biz, list) and biz else {})
    if not isinstance(container, dict):
        return {}, {}, None, None
    day_map = {}
    model_maps = {}
    for d in container.get("days") or []:
        if isinstance(d, dict) and d.get("date"):
            normalized = bucket_date(d["date"])
            if normalized:
                records = d.get("data") or []
                day_map.setdefault(normalized, []).extend(_flatten_usage(records))
                for record in records:
                    name = model_name(record)
                    if not name:
                        continue
                    entries = _flatten_usage([record])
                    if entries:
                        model_maps.setdefault(name, {}).setdefault(normalized, []).extend(entries)
    return day_map, model_maps, container.get("total"), container.get("currency")


def classic_day_map(biz):
    """兼容旧调用：返回不分模型的经典按日数据。"""
    day_map, _, total, currency = classic_day_maps(biz)
    return day_map, total, currency


def series_day_map(biz):
    """by_api_key 接口：biz 为 {series:[{model?, buckets:[{time,usage:[...]}]}]}。
    返回 (day_map: {date: [entries]}, None, None)"""
    day_map = {}
    if isinstance(biz, list) and biz:
        biz = biz[0]
    if not isinstance(biz, dict):
        return day_map, None, None
    for item in biz.get("series") or []:
        if not isinstance(item, dict):
            continue
        buckets = item.get("buckets") or item.get("bucket") or item.get("data") or []
        if not isinstance(buckets, list):
            continue
        for b in buckets:
            if not isinstance(b, dict):
                continue
            d = bucket_date(b.get("time") or b.get("date") or b.get("day") or b.get("timestamp"))
            if not d:
                continue
            usage = _flatten_usage(b.get("usage") or b.get("usages"))
            if usage:
                day_map.setdefault(d, []).extend(usage)
    return day_map, None, None


def aggregate_day_map(day_map, mode):
    """把 {date: entries} 汇总成总量 dict（sum_usage 结构）。"""
    total = {"cost": 0.0, "tokens": 0, "prompt": 0, "output": 0,
             "cache_hit": 0, "cache_miss": 0, "requests": 0}
    for entries in day_map.values():
        a = sum_usage(entries, mode)
        for k in total:
            total[k] += a[k]
    return total


# ─────────────────────────── 平台数据抓取 ───────────────────────────

def key_matches(api_key_obj, key_filter):
    """series 条目里的 api_key 是否匹配配置的完整 key。
    脱敏格式形如 sk-xxxxx***yyyy：按前 7 位 + 后 4 位匹配。"""
    if not key_filter:
        return True
    if not isinstance(api_key_obj, dict):
        return False
    sid = api_key_obj.get("sensitive_id") or ""
    return bool(sid) and sid.startswith(key_filter[:7]) and sid.endswith(key_filter[-4:])


def _bykey_agg(cost_biz, amt_biz, key_filter):
    """解析 by_api_key 接口的 cost + amount 响应，汇总为
    {cost, tokens, prompt, output, cache_hit, cache_miss, requests, currency}。
    返回 (agg, matched_any)。cost 的 series 在 data[].series、桶字段为 cost(字符串)；
    amount 的 series 在顶层、桶的 usage 是 {类型: 数量} 字典。"""
    agg = empty_usage_agg()
    agg["by_model"] = {}
    matched_any = False

    cb = cost_biz[0] if isinstance(cost_biz, list) and cost_biz else cost_biz
    if isinstance(cb, dict):
        for d in cb.get("data") or []:
            if not isinstance(d, dict):
                continue
            if d.get("currency"):
                agg["currency"] = d["currency"]
            for s in d.get("series") or []:
                if not isinstance(s, dict) or not key_matches(s.get("api_key"), key_filter):
                    continue
                matched_any = True
                series_name = model_name(s)
                for b in s.get("buckets") or []:
                    if isinstance(b, dict):
                        name = series_name or model_name(b)
                        model_agg = None
                        if name:
                            model_agg = agg["by_model"].setdefault(
                                name, empty_usage_agg(agg.get("currency")))
                            model_agg["cost_available"] = True
                        value = to_float(b.get("cost"))
                        agg["cost"] += value
                        if model_agg is not None:
                            model_agg["cost"] += value

    ab = amt_biz[0] if isinstance(amt_biz, list) and amt_biz else amt_biz
    if isinstance(ab, dict):
        for s in ab.get("series") or []:
            if not isinstance(s, dict) or not key_matches(s.get("api_key"), key_filter):
                continue
            matched_any = True
            series_name = model_name(s)
            for b in s.get("buckets") or []:
                u = b.get("usage") if isinstance(b, dict) else None
                if not isinstance(u, dict):
                    continue
                name = series_name or model_name(b)
                model_agg = None
                if name:
                    model_agg = agg["by_model"].setdefault(
                        name, empty_usage_agg(agg.get("currency")))
                    model_agg["usage_available"] = True
                for typ, val in u.items():
                    n = to_int(val)
                    kind = classify(typ)
                    if kind == "request":
                        agg["requests"] += n
                        if model_agg is not None:
                            model_agg["requests"] += n
                    elif kind == "output":
                        agg["tokens"] += n
                        agg["output"] += n
                        if model_agg is not None:
                            model_agg["tokens"] += n
                            model_agg["output"] += n
                    elif kind == "cache_hit":
                        agg["tokens"] += n
                        agg["cache_hit"] += n
                        agg["prompt"] += n
                        if model_agg is not None:
                            model_agg["tokens"] += n
                            model_agg["cache_hit"] += n
                            model_agg["prompt"] += n
                    elif kind == "cache_miss":
                        agg["tokens"] += n
                        agg["cache_miss"] += n
                        agg["prompt"] += n
                        if model_agg is not None:
                            model_agg["tokens"] += n
                            model_agg["cache_miss"] += n
                            model_agg["prompt"] += n
                    elif kind == "prompt":
                        agg["tokens"] += n
                        agg["prompt"] += n
                        if model_agg is not None:
                            model_agg["tokens"] += n
                            model_agg["prompt"] += n
    for item in agg["by_model"].values():
        item["currency"] = item.get("currency") or agg.get("currency")
    return agg, matched_any


def fetch_platform_usage(token, api_keys=None, tz=CN_TZ, lang=DEFAULT_LANG,
                         time_dimension=DEFAULT_TIME_DIMENSION, now=None):
    """返回 {ok, cost, tokens, prompt, output, cache_hit, cache_miss,
             requests, currency, date, range_start, range_end, by_key, err}。
    api_keys 可传一把或多把完整 key；每个 key 仅在确实匹配到脱敏 ID 时返回明细。
    时间范围按指定时区（默认 GMT+8）的自然日统计；优先 by_api_key
    （可按 key 过滤、含 per-key 明细），回退经典按月接口（仅账户级）。"""
    if isinstance(api_keys, str):
        api_keys = [api_keys]
    api_keys = [k for k in (api_keys or []) if k]
    if time_dimension not in VALID_TIME_DIMENSIONS:
        time_dimension = DEFAULT_TIME_DIMENSION
    start_date, end_date = usage_date_range(time_dimension, tz, now)
    result = {"ok": False, "cost": None, "tokens": None, "prompt": 0, "output": 0,
              "cache_hit": 0, "cache_miss": 0, "requests": 0,
              "currency": None, "date": end_date.isoformat(),
              "range_key": time_dimension, "range_start": start_date.isoformat(),
              "range_end": end_date.isoformat(),
              "mine": None, "by_key": {}, "by_model": {}, "err": None}

    def try_by_api_key():
        t0 = int(datetime(start_date.year, start_date.month, start_date.day,
                          tzinfo=tz).timestamp())
        day_after_end = end_date + timedelta(days=1)
        t1 = int(datetime(day_after_end.year, day_after_end.month, day_after_end.day,
                          tzinfo=tz).timestamp())
        p = {"start": t0, "end": t1, "tz": 0}
        cost_biz = get_biz(platform_get(token, "usage/by_api_key/cost", p))
        time.sleep(0.8)                  # 拉开两个请求的间隔，规避突发拦截
        amt_biz = get_biz(platform_get(token, "usage/by_api_key/amount", p))
        if cost_biz is None or amt_biz is None:
            return False
        all_agg, any_series = _bykey_agg(cost_biz, amt_biz, None)
        if not any_series:
            return False                 # 空响应 → 交给经典接口兜底
        by_key = {}
        for api_key in api_keys:
            mine_agg, matched = _bykey_agg(cost_biz, amt_biz, api_key)
            by_key[api_key] = mine_agg if matched else None
        mine = by_key.get(api_keys[0]) if len(api_keys) == 1 else None
        result.update(
            ok=True, cost=all_agg["cost"], tokens=all_agg["tokens"],
            prompt=all_agg["prompt"], output=all_agg["output"],
            cache_hit=all_agg["cache_hit"], cache_miss=all_agg["cache_miss"],
            requests=all_agg["requests"], currency=all_agg["currency"],
            date=end_date.isoformat(), mine=mine, by_key=by_key, err=None,
            by_model=all_agg.get("by_model") or {},
        )
        return True

    def try_classic():
        # 经典接口按月查询；近 7/30 天跨月时拉取覆盖到的每个月，再按日期过滤。
        amt_map, cost_map, currency = {}, {}, None
        amt_models, cost_models = {}, {}
        for year, month in months_in_range(start_date, end_date):
            p = {"month": month, "year": year}
            amt_biz = get_biz(platform_get(token, "usage/amount", p))
            time.sleep(0.8)              # 拉开两个请求的间隔，规避突发拦截
            cost_biz = get_biz(platform_get(token, "usage/cost", p))
            if amt_biz is None or cost_biz is None:
                return False
            month_amt, month_amt_models, _, _ = classic_day_maps(amt_biz)
            month_cost, month_cost_models, _, month_currency = classic_day_maps(cost_biz)
            amt_map.update(month_amt)
            cost_map.update(month_cost)
            for name, day_map in month_amt_models.items():
                amt_models.setdefault(name, {}).update(day_map)
            for name, day_map in month_cost_models.items():
                cost_models.setdefault(name, {}).update(day_map)
            currency = month_currency or currency
        if not amt_map and not cost_map:
            return False               # 空响应 → 交给 by_api_key 兜底
        selected_amt = {d: entries for d, entries in amt_map.items()
                        if start_date.isoformat() <= d <= end_date.isoformat()}
        selected_cost = {d: entries for d, entries in cost_map.items()
                         if start_date.isoformat() <= d <= end_date.isoformat()}
        a = aggregate_day_map(selected_amt, "tokens")
        c = aggregate_day_map(selected_cost, "cost")
        by_model = {}
        for name in sorted(set(amt_models) | set(cost_models), key=str.casefold):
            model_amt = {d: entries for d, entries in amt_models.get(name, {}).items()
                         if start_date.isoformat() <= d <= end_date.isoformat()}
            model_cost = {d: entries for d, entries in cost_models.get(name, {}).items()
                          if start_date.isoformat() <= d <= end_date.isoformat()}
            model_agg = empty_usage_agg(currency)
            merge_usage_agg(model_agg, aggregate_day_map(model_amt, "tokens"))
            merge_usage_agg(model_agg, aggregate_day_map(model_cost, "cost"))
            model_agg["usage_available"] = name in amt_models
            model_agg["cost_available"] = name in cost_models
            by_model[name] = model_agg
        result.update(
            ok=True, cost=c["cost"], tokens=a["tokens"], prompt=a["prompt"],
            output=a["output"], cache_hit=a["cache_hit"], cache_miss=a["cache_miss"],
            requests=a["requests"], currency=currency, date=end_date.isoformat(),
            mine=None, by_model=by_model, err=None,  # 经典接口无 per-key 明细
        )
        return True

    try:
        if not try_by_api_key() and not try_classic():
            result["err"] = tr(lang, "no_usage_data")
    except Exception as e:
        result["err"] = short_err(e)
    return result


# ─────────────────────────── 聚合（多账户合计） ───────────────────────────

def snap_to_money(snap, cost_key="today_cost"):
    """单个账户快照 → 按币种分组的金额 dict：
    {topped_up:{cur:val}, granted:{}, today:{}}
    cost_key 选择用账户级("today_cost")还是单 key("today_cost_my")的今日消费。"""
    money = {"topped_up": {}, "granted": {}, "today": {}}
    bal = snap.get("balance")
    if bal:
        cur = bal["currency"]
        money["topped_up"][cur] = money["topped_up"].get(cur, 0) + bal["topped_up"]
        money["granted"][cur] = money["granted"].get(cur, 0) + bal["granted"]
    if snap.get(cost_key) is not None:
        cur = snap.get("currency") or (bal or {}).get("currency") or "CNY"
        money["today"][cur] = money["today"].get(cur, 0) + snap[cost_key]
    return money


def merge_money(dst, src):
    for key in dst:
        for cur, v in src[key].items():
            dst[key][cur] = dst[key].get(cur, 0) + v
    return dst


def merge_model_usage(dst, raw_by_model, fallback_currency=None):
    """把接口的按模型统计合并为适合界面筛选的多币种结构。"""
    for name, stats in (raw_by_model or {}).items():
        if not name or not isinstance(stats, dict):
            continue
        item = dst.setdefault(name, {
            "costs": {}, "tokens": 0,
            "cost_available": False, "usage_available": False,
            "detail": {"prompt": 0, "output": 0, "cache_hit": 0,
                       "cache_miss": 0, "requests": 0},
        })
        currency = stats.get("currency") or fallback_currency or "CNY"
        cost_available = stats.get("cost_available", True)
        usage_available = stats.get("usage_available", True)
        if cost_available:
            item["cost_available"] = True
            item["costs"][currency] = item["costs"].get(currency, 0) + stats.get("cost", 0)
        if usage_available:
            item["usage_available"] = True
            item["tokens"] += stats.get("tokens", 0)
            for key in item["detail"]:
                item["detail"][key] += stats.get(key, 0)
    return dst


def aggregate_view(acc_snaps, ts, lang=DEFAULT_LANG):
    """多个账户快照 → 「账号总量」渲染视图。
    同一 DS账号 下的多把 key 视为同一平台账户；若出现多个不同 userToken，
    则按 token 分组汇总并给出提示。每组的账户余额/用量只计一次。"""
    money = {"topped_up": {}, "granted": {}, "today": {}}
    tokens_total = None
    detail = {"prompt": 0, "output": 0, "cache_hit": 0, "requests": 0}
    warnings = []
    model_usage = {}
    dates = set()
    range_keys = {s.get("range_key") for s in acc_snaps if s.get("range_key")}
    range_starts = {s.get("range_start") for s in acc_snaps if s.get("range_start")}
    range_ends = {s.get("range_end") for s in acc_snaps if s.get("range_end")}
    token_ids = {s.get("token_id") for s in acc_snaps if s.get("token_id")}
    groups = {}
    for i, s in enumerate(acc_snaps):
        # 只有一个已知 token 时，无 token 的其他 key 也归入同一 DS 账号，避免重复余额。
        if len(token_ids) == 1:
            gid = next(iter(token_ids))
        else:
            gid = s.get("token_id") or "no-token"
        groups.setdefault(gid, []).append(s)
        warnings.extend(s.get("warnings") or [])

    if len(token_ids) > 1:
        warnings.append(tr(lang, "multi_token"))

    sources = set()
    token_groups = 0
    for snaps in groups.values():
        # 同账户多把 key 的余额共享；优先采用本轮成功的任意一把。
        bal_snap = next((s for s in snaps if s.get("balance")), None)
        if bal_snap:
            bal = bal_snap["balance"]
            cur = bal["currency"]
            money["topped_up"][cur] = money["topped_up"].get(cur, 0) + bal["topped_up"]
            money["granted"][cur] = money["granted"].get(cur, 0) + bal["granted"]

        official = next((s for s in snaps if s.get("today_cost_src") == "官方"
                         and s.get("today_cost") is not None), None)
        estimate = next((s for s in snaps if s.get("today_cost_src") == "估算"
                         and s.get("today_cost") is not None), None)
        usage_snap = official or estimate
        if not usage_snap:
            continue
        sources.add(usage_snap["today_cost_src"])
        if usage_snap.get("today_date"):
            dates.add(usage_snap["today_date"])
        cur = usage_snap.get("currency") or (usage_snap.get("balance") or {}).get("currency") or "CNY"
        money["today"][cur] = money["today"].get(cur, 0) + usage_snap["today_cost"]
        merge_model_usage(model_usage, usage_snap.get("by_model"), cur)
        if usage_snap.get("tokens") is not None:
            token_groups += 1
            tokens_total = (tokens_total or 0) + usage_snap["tokens"]
            d = usage_snap.get("detail") or {}
            for k in detail:
                detail[k] += d.get(k, 0)

    # 去重提示，避免同 token 的每把 key 重复显示相同错误。
    warnings = list(dict.fromkeys(warnings))
    if len(sources) > 1:
        source = "混合"
    elif sources:
        source = next(iter(sources))
    else:
        source = None
    group_count = len(groups)
    return {
        "money": money,
        "tokens": tokens_total,
        "detail": detail,
        "model_usage": model_usage,
        "today_cost_src": source,
        "today_date": dates.pop() if len(dates) == 1 else None,
        "range_key": range_keys.pop() if len(range_keys) == 1 else DEFAULT_TIME_DIMENSION,
        "range_start": range_starts.pop() if len(range_starts) == 1 else None,
        "range_end": range_ends.pop() if len(range_ends) == 1 else None,
        "token_configured": any(s.get("token_configured") for s in acc_snaps),
        "token_coverage": (token_groups, group_count),
        "data_stale": any(s.get("data_stale") for s in acc_snaps),
        "warnings": warnings,
        "ts": ts,
        "n_accounts": len(acc_snaps),
        "is_all": True,
    }


def make_app_image(size=64):
    """加载与 EXE 一致的黑蓝 DS 图标；资源异常时生成同配色备用图标。"""
    try:
        with Image.open(APP_ICON_SOURCE) as source:
            return source.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    except Exception:
        pass

    from PIL import ImageFont
    img = Image.new("RGBA", (size, size), (0, 0, 0, 255))
    d = ImageDraw.Draw(img)
    text = "DS"
    font = None
    for fp in (r"C:\Windows\Fonts\bahnschrift.ttf",
               r"C:\Windows\Fonts\arialbd.ttf",
               r"C:\Windows\Fonts\msyhbd.ttc",
               r"C:\Windows\Fonts\segoeuib.ttf"):
        try:
            font = ImageFont.truetype(fp, max(10, int(size * 0.56)))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]),
           text, font=font, fill=(94, 200, 255, 255), stroke_width=max(0, size // 96))
    return img


# ─────────────────────────── 界面 ───────────────────────────

class MonitorApp:
    def __init__(self, root):
        self.root = root
        initial_cfg = load_config()
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.wake = threading.Event()          # 手动"立即刷新"
        self.snap = None                       # 最近一次完整快照（多账户）
        self._lang = initial_cfg.get("language") if initial_cfg.get("language") in VALID_LANGUAGES else DEFAULT_LANG
        self.selected = tk.StringVar(value=ALL_LABELS[self._lang])
        self._selected = ALL_LABELS[self._lang]            # 纯属性镜像，供托盘菜单线程读取
        self.selected_model = tk.StringVar(value=ALL_MODELS_LABELS[self._lang])
        self._selected_model = ALL_MODELS_LABELS[self._lang]
        self.selected_ds = tk.StringVar(value="")         # 当前 DS账号名称（窗口文本）
        self._selected_ds = ""                 # 纯属性镜像
        self._account_groups = {}              # DS账号名称 -> [API Key名称]
        self._topmost = True
        self._topmost_var = tk.BooleanVar(value=True)
        self._time_dimension = DEFAULT_TIME_DIMENSION
        self._time_var = tk.StringVar(value=self._time_dimension)
        self._pin_corner = bool(initial_cfg.get("pin_corner", True))
        self._pin_var = tk.BooleanVar(value=self._pin_corner)
        self._startup_enabled = is_startup_enabled()
        self._positioned = False
        self._tz_name = DEFAULT_TZ             # 当前时区（托盘菜单勾选）
        self._tray_hint_shown = False

        root.title(APP_NAME)
        root.overrideredirect(True)            # 去掉标题栏（缩小/关闭那一栏），也不占任务栏
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        root.resizable(False, False)
        self._drag = None
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._build_context_menu()
        self._setup_tray()
        self._fit_pin()

        threading.Thread(target=self._worker, daemon=True).start()
        self.root.after(150, self._poll)

    def _t(self, key, **kwargs):
        return tr(self._lang, key, **kwargs)

    def _all_label(self):
        return ALL_LABELS[self._lang]

    def _all_models_label(self):
        return ALL_MODELS_LABELS[self._lang]

    def _time_label(self):
        return time_dimension_label(self._lang, self._time_dimension)

    def _usage_label(self, kind):
        return self._t(f"range_{kind}", range=self._time_label())

    def _show_usage_placeholders(self):
        self.lbl_today.config(text=f"{self._usage_label('cost')}  —")
        self.lbl_today_req.config(text=f"{self._usage_label('requests')}  —")
        self.lbl_tokens.config(text=f"{self._usage_label('tokens')}  —")
        self.lbl_tokens_sub.config(text="")
        self.lbl_status.config(text=self._t("starting"))
        self._fit_pin()

    # ── UI（黑色悬浮卡片，仅标题 + 3 行数据 + 状态）──
    def _build_ui(self):
        bg = BG
        fg = "#e8edf3"
        dim = "#8a94a3"
        accent = "#5ec8ff"
        warn = "#ffb74d"
        ok = "#69db7c"

        pad = dict(padx=12, pady=1)

        titlebar = tk.Frame(self.root, bg=bg)
        titlebar.pack(fill="x", padx=(12, 5), pady=(4, 0))
        self.lbl_title = tk.Label(titlebar, text=f"{APP_NAME} {VERSION}", bg=bg, fg=accent,
                                  font=("Microsoft YaHei UI", 10, "bold"), anchor="w")
        self.lbl_title.pack(side="left", fill="x", expand=True)
        tk.Button(titlebar, text="×", command=self._on_close, bg=bg, fg=dim,
                  activebackground="#252a32", activeforeground="#ffffff",
                  relief="flat", bd=0, padx=6, pady=0,
                  font=("Microsoft YaHei UI", 11)).pack(side="right")
        for widget in (titlebar, self.lbl_title):
            widget.bind("<Button-1>", self._on_press)
            widget.bind("<B1-Motion>", self._on_drag)
            widget.bind("<ButtonRelease-1>", self._on_release)

        # DS账号行：DS账号名称：XXX + 下拉（账号总量 / 各 key）
        try:
            style = ttk.Style(self.root)
            style.theme_use("clam")
            style.configure("DSM.TCombobox",
                            fieldbackground=COMBO_BG, background=COMBO_BG,
                            foreground=COMBO_FG, arrowcolor=COMBO_FG,
                            selectbackground=COMBO_SELECT_BG,
                            selectforeground=COMBO_FG,
                            bordercolor=COMBO_BG, lightcolor=COMBO_BG,
                            darkcolor=COMBO_BG, padding=2)
            style.map(
                "DSM.TCombobox",
                fieldbackground=[("readonly", COMBO_BG), ("focus", COMBO_BG),
                                 ("!disabled", COMBO_BG)],
                background=[("readonly", COMBO_BG), ("active", COMBO_BG),
                            ("pressed", COMBO_BG)],
                foreground=[("readonly", COMBO_FG), ("focus", COMBO_FG),
                            ("!disabled", COMBO_FG)],
                arrowcolor=[("readonly", COMBO_FG), ("active", COMBO_FG)],
            )
            # ttk Combobox 展开后使用原生 Listbox，需通过 option database 单独设色。
            self.root.option_add("*TCombobox*Listbox.background", COMBO_BG)
            self.root.option_add("*TCombobox*Listbox.foreground", COMBO_FG)
            self.root.option_add("*TCombobox*Listbox.selectBackground", COMBO_SELECT_BG)
            self.root.option_add("*TCombobox*Listbox.selectForeground", COMBO_FG)
        except Exception:
            pass
        head = tk.Frame(self.root, bg=bg)
        head.pack(fill="x", padx=12, pady=(6, 0))
        self.lbl_ds_caption = tk.Label(head, text=self._t("ds_account"), bg=bg, fg=accent,
                                       font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_ds_caption.pack(side="left")
        self.lbl_ds = tk.Label(head, text="—", bg=bg, fg=fg,
                               font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_ds.pack(side="left")
        self.combo = ttk.Combobox(head, textvariable=self.selected, state="readonly",
                                  width=11, style="DSM.TCombobox", font=("Microsoft YaHei UI", 8))
        self.combo.pack(side="right")
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._render_all(self.snap))

        model_head = tk.Frame(self.root, bg=bg)
        model_head.pack(fill="x", padx=12, pady=(3, 2))
        self.lbl_model_caption = tk.Label(
            model_head, text=self._t("model"), bg=bg, fg=accent,
            font=("Microsoft YaHei UI", 9, "bold"))
        self.lbl_model_caption.pack(side="left")
        self.model_combo = ttk.Combobox(
            model_head, textvariable=self.selected_model, state="readonly",
            width=20, style="DSM.TCombobox", font=("Microsoft YaHei UI", 8))
        self.model_combo["values"] = [self._all_models_label()]
        self.model_combo.pack(side="right")
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_selected)

        self.lbl_balance = tk.Label(self.root, text=f"{self._t('balance')}  —", bg=bg, fg=fg,
                                    font=("Microsoft YaHei UI", 14, "bold"), anchor="w")
        self.lbl_balance.pack(fill="x", **pad)
        self.lbl_balance_sub = tk.Label(self.root, text="", bg=bg, fg=dim,
                                        font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_balance_sub.pack(fill="x", padx=12)

        self.lbl_today = tk.Label(self.root, text=f"{self._usage_label('cost')}  —", bg=bg, fg=fg,
                                  font=("Microsoft YaHei UI", 10), anchor="w")
        self.lbl_today.pack(fill="x", **pad)
        self.lbl_today_req = tk.Label(self.root, text=f"{self._usage_label('requests')}  —", bg=bg, fg=fg,
                                      font=("Microsoft YaHei UI", 10), anchor="w")
        self.lbl_today_req.pack(fill="x", **pad)
        self.lbl_tokens = tk.Label(self.root, text=f"{self._usage_label('tokens')}  —", bg=bg, fg=fg,
                                   font=("Microsoft YaHei UI", 10), anchor="w")
        self.lbl_tokens.pack(fill="x", **pad)
        self.lbl_tokens_sub = tk.Label(self.root, text="", bg=bg, fg=dim,
                                       font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_tokens_sub.pack(fill="x", padx=12)

        self.lbl_status = tk.Label(self.root, text=self._t("starting"), bg=bg, fg=dim,
                                   font=("Microsoft YaHei UI", 8), anchor="w")
        self.lbl_status.pack(fill="x", side="bottom", padx=12, pady=(1, 5))

        self._colors = {"dim": dim, "accent": accent, "warn": warn, "ok": ok, "fg": fg}

    def _fit_pin(self, force=False):
        """按内容自适应尺寸（保证不截字），并贴回桌面右下角（任务栏上方）。"""
        try:
            self.root.update_idletasks()
            wa_l, wa_t, wa_r, wa_b = work_area(self.root)
            w = max(self.root.winfo_reqwidth(), MIN_WIDTH)
            h = self.root.winfo_reqheight()
            if self._pin_corner or force or not self._positioned:
                x = max(wa_l, wa_r - w - 8)
                y = max(wa_t, wa_b - h - 8)
            else:
                x = max(wa_l, min(self.root.winfo_x(), wa_r - w))
                y = max(wa_t, min(self.root.winfo_y(), wa_b - h))
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self._positioned = True
        except Exception:
            pass

    def _apply_topmost(self):
        self._topmost = bool(self._topmost_var.get())
        self.root.attributes("-topmost", self._topmost)

    def _apply_pin(self):
        self._pin_corner = bool(self._pin_var.get())
        self._drag = None
        cfg = load_config()
        cfg["pin_corner"] = self._pin_corner
        save_config(cfg)
        if self._pin_corner:
            self._fit_pin(force=True)

    def _set_language(self, lang):
        if lang not in VALID_LANGUAGES or lang == self._lang:
            return
        old_all = self._all_label()
        old_all_models = self._all_models_label()
        self._lang = lang
        if self.selected.get() == old_all or self._selected == old_all:
            self.selected.set(self._all_label())
            self._selected = self._all_label()
        if self.selected_model.get() == old_all_models or self._selected_model == old_all_models:
            self.selected_model.set(self._all_models_label())
            self._selected_model = self._all_models_label()
        cfg = load_config()
        cfg["language"] = lang
        save_config(cfg)
        self.wake.set()
        self.lbl_ds_caption.config(text=self._t("ds_account"))
        self.lbl_model_caption.config(text=self._t("model"))
        self._build_context_menu()
        self._rebuild_tray_menu()
        if self.snap is not None:
            self._render_all(self.snap)
        else:
            self.lbl_balance.config(text=f"{self._t('balance')}  —")
            self._show_usage_placeholders()

    def _set_time_dimension(self, time_dimension):
        if time_dimension not in VALID_TIME_DIMENSIONS or time_dimension == self._time_dimension:
            self._time_var.set(self._time_dimension)
            return
        self._time_dimension = time_dimension
        self._time_var.set(time_dimension)
        self.snap = None
        self._show_usage_placeholders()
        self._rebuild_tray_menu()
        self.wake.set()

    def _on_model_selected(self, _event=None):
        self._selected_model = self.selected_model.get()
        if self.snap is not None:
            self._render_all(self.snap)

    def _apply_model_filter(self, view):
        if self._selected_model == self._all_models_label():
            return view
        item = (view.get("model_usage") or {}).get(self._selected_model)
        if item is None:
            return view
        filtered = dict(view)
        money = {key: dict(value) for key, value in view["money"].items()}
        money["today"] = dict(item.get("costs") or {}) if item.get("cost_available") else {}
        filtered["money"] = money
        filtered["tokens"] = item.get("tokens", 0) if item.get("usage_available") else None
        filtered["detail"] = dict(item.get("detail") or {}) if item.get("usage_available") else {}
        filtered["selected_model"] = self._selected_model
        return filtered

    def _build_context_menu(self):
        if getattr(self, "context_menu", None) is not None:
            try:
                self.context_menu.destroy()
            except tk.TclError:
                pass
        menu_style = {
            "bg": COMBO_BG, "fg": COMBO_FG,
            "activebackground": COMBO_SELECT_BG, "activeforeground": COMBO_FG,
            "selectcolor": COMBO_FG, "relief": "flat", "bd": 0,
        }
        m = tk.Menu(self.root, tearoff=False, **menu_style)
        m.add_command(label=self._t("refresh"), command=self._manual_refresh)
        m.add_command(label=self._t("settings"), command=self.open_settings)
        time_menu = tk.Menu(m, tearoff=False, **menu_style)
        for key, text_key in TIME_DIMENSIONS:
            time_menu.add_radiobutton(
                label=self._t(text_key), value=key, variable=self._time_var,
                command=lambda selected=key: self._set_time_dimension(selected))
        m.add_cascade(label=self._t("time_dimension"), menu=time_menu)
        m.add_command(label=self._t("open_config"), command=self._open_config_dir)
        self.context_menu = m
        def bind_menu(widget):
            widget.bind("<Button-3>", self._show_context_menu, add="+")
            for child in widget.winfo_children():
                bind_menu(child)
        if not getattr(self, "_context_bound", False):
            bind_menu(self.root)
            self._context_bound = True

    def _show_context_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _open_config_dir(self):
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            os.startfile(APP_DATA_DIR)
        except OSError as e:
            messagebox.showerror(APP_NAME, self._t("open_config_error", error=short_err(e)), parent=self.root)

    # ── 系统托盘 ──
    def _setup_tray(self):
        self.icon = None
        if not HAS_TRAY:
            return
        try:
            image = make_app_image(64)
            self.icon = pystray.Icon("dsm_usage_monitor", image,
                                     title=APP_NAME, menu=pystray.Menu())
            self._rebuild_tray_menu()
            threading.Thread(target=self.icon.run, daemon=True).start()
        except Exception:
            self.icon = None

    def _rebuild_tray_menu(self):
        """重建托盘菜单：账户 → DS账号（第一层）→ 账号总量/各 key（第二层）。"""
        if self.icon is None:
            return

        def select_action(ds, view):
            def action(icon, item):
                self._tray_select(ds, view)
            return action

        def checked_for(ds, view):
            def checked(item):
                return self._selected_ds == ds and self._selected == view
            return checked

        def ds_checked(ds):
            def checked(item):
                return self._selected_ds == ds
            return checked

        ds_items = []
        all_label = self._all_label()
        for ds, key_names in self._account_groups.items():
            sub = [pystray.MenuItem(all_label, select_action(ds, all_label),
                                    checked=checked_for(ds, all_label))]
            for kn in key_names:
                sub.append(pystray.MenuItem(kn, select_action(ds, kn),
                                            checked=checked_for(ds, kn)))
            ds_items.append(pystray.MenuItem(ds, pystray.Menu(*sub),
                                             checked=ds_checked(ds)))
        def tz_action(name):
            def action(icon, item):
                self._tray_timezone(name)
            return action

        def tz_checked(name):
            def checked(item):
                return self._tz_name == name
            return checked

        tz_items = [pystray.MenuItem(n, tz_action(n), checked=tz_checked(n))
                    for n in ("GMT+8", "UTC+0")]
        def lang_action(code):
            return lambda icon, item: self._tray_language(code)

        def lang_checked(code):
            return lambda item: self._lang == code

        lang_items = [pystray.MenuItem(label, lang_action(code), checked=lang_checked(code))
                      for label, code in LANGUAGE_MENU_OPTIONS]

        def time_action(key):
            return lambda icon, item: self._tray_time_dimension(key)

        def time_checked(key):
            return lambda item: self._time_dimension == key

        time_items = [pystray.MenuItem(self._t(text_key), time_action(key),
                                       checked=time_checked(key), radio=True)
                      for key, text_key in TIME_DIMENSIONS]
        menu = pystray.Menu(
            pystray.MenuItem(self._t("show_hide"), self._tray_toggle, default=True),
            pystray.MenuItem(self._t("refresh"), self._tray_refresh),
            pystray.MenuItem(self._t("switch_account"), pystray.Menu(*ds_items)),
            pystray.MenuItem(self._t("settings"), self._tray_settings),
            pystray.MenuItem(self._t("timezone"), pystray.Menu(*tz_items)),
            pystray.MenuItem(self._t("time_dimension"), pystray.Menu(*time_items)),
            pystray.MenuItem(LANGUAGE_MENU_LABEL, pystray.Menu(*lang_items)),
            pystray.MenuItem(self._t("topmost"), self._tray_topmost,
                             checked=lambda item: self._topmost),
            pystray.MenuItem(self._t("pin_corner"), self._tray_pin,
                             checked=lambda item: self._pin_corner),
            pystray.MenuItem(self._t("startup"), self._tray_startup,
                             checked=lambda item: self._startup_enabled),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._t("exit"), self._tray_quit),
        )
        self.icon.menu = menu

    def _tray_select(self, ds, view):
        def do():
            self._selected_ds = ds
            self.selected_ds.set(ds)
            self.selected.set(view)
            self._selected = view
            self._render_all(self.snap)
        self.root.after(0, do)

    def _tray_timezone(self, name):
        def do():
            cfg = load_config()
            cfg["timezone"] = name
            save_config(cfg)
            self.wake.set()              # 立即用新时区刷新
        self.root.after(0, do)

    def _tray_language(self, lang):
        self.root.after(0, lambda: self._set_language(lang))

    def _tray_time_dimension(self, time_dimension):
        self.root.after(0, lambda: self._set_time_dimension(time_dimension))

    def _tray_toggle(self, icon, item):
        self.root.after(0, self._toggle_window)

    def _tray_refresh(self, icon, item):
        self.root.after(0, self._manual_refresh)

    def _tray_settings(self, icon, item):
        self.root.after(0, self.open_settings)

    def _tray_topmost(self, icon, item):
        def toggle():
            self._topmost_var.set(not self._topmost_var.get())
            self._apply_topmost()
        self.root.after(0, toggle)

    def _tray_pin(self, icon, item):
        def toggle():
            self._pin_var.set(not self._pin_var.get())
            self._apply_pin()
        self.root.after(0, toggle)

    def _tray_startup(self, icon, item):
        def toggle():
            enabled = not self._startup_enabled
            try:
                set_startup_enabled(enabled)
                self._startup_enabled = is_startup_enabled()
                self._rebuild_tray_menu()
            except OSError as e:
                messagebox.showerror(
                    APP_NAME, self._t("startup_error", error=short_err(e)), parent=self.root)
        self.root.after(0, toggle)

    def _tray_quit(self, icon, item):
        self.root.after(0, self._quit)

    def _toggle_window(self):
        if self.root.state() == "withdrawn" or not self.root.winfo_viewable():
            self.root.deiconify()
            self.root.lift()
            self._apply_topmost()
            self._fit_pin()                    # 显示时重新贴回右下角
        else:
            self.root.withdraw()

    def _hide_to_tray(self):
        if self.icon is not None:
            self.root.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                messagebox.showinfo(APP_NAME, self._t("tray_hint"))
        else:
            self._quit()

    def _on_close(self):
        """点窗口 X：有托盘则最小化到托盘，否则退出。"""
        if self.icon is not None:
            self._hide_to_tray()
        else:
            self._quit()

    # ── 拖动 ──
    def _on_press(self, e):
        if self._pin_corner:
            self._drag = None
            return "break"
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _on_drag(self, e):
        if self._pin_corner:
            self._drag = None
            return "break"
        if self._drag:
            self.root.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _on_release(self, _e):
        self._drag = None

    # ── 刷新控制 ──
    def _manual_refresh(self):
        self.wake.set()

    def _quit(self):
        self.stop.set()
        self.wake.set()
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.root.destroy()

    # ── 后台抓取线程 ──
    _USAGE_KEYS = ("today_cost", "today_cost_src", "tokens", "detail", "currency",
                   "est_today_cost", "today_cost_my", "tokens_my", "detail_my",
                   "by_model", "by_model_my", "per_key_available", "per_key_error")

    def _worker(self):
        prev = {}           # 账户名 -> 上次快照
        fail_streak = 0     # 连续失败次数（用于网络异常时自动降频）
        while not self.stop.is_set():
            cfg = load_config()
            time_dimension = self._time_dimension
            try:
                snap = self._fetch_all(cfg, time.time(), prev, time_dimension)
                unhealthy = any(s.get("fetch_failed") for s in (snap.get("accounts") or {}).values())
                fail_streak = fail_streak + 1 if unhealthy else 0
                self.queue.put(snap)
                prev = snap.get("accounts") or {}
            except Exception as e:            # 兜底，绝不让线程死掉
                fail_streak += 1
                lang = cfg.get("language") if cfg.get("language") in VALID_LANGUAGES else DEFAULT_LANG
                self.queue.put({"ts": time.time(), "ok": False,
                                "warnings": [tr(lang, "internal_error", error=short_err(e))],
                                "accounts": {}, "language": lang,
                                "time_dimension": time_dimension})
            base = max(5, int(to_float(cfg.get("refresh_seconds"), 30)))
            interval = 45 if fail_streak >= 3 else base   # 连续失败时降频，等平台恢复
            self.wake.wait(interval)
            self.wake.clear()

    def _fetch_all(self, cfg, now, prev, time_dimension=DEFAULT_TIME_DIMENSION):
        accounts = cfg.get("accounts") or []
        acc_snaps = {}
        warnings = []
        tz = TIMEZONES.get(cfg.get("timezone") or DEFAULT_TZ, CN_TZ)
        tz_name = cfg.get("timezone") or DEFAULT_TZ
        lang = cfg.get("language") if cfg.get("language") in VALID_LANGUAGES else DEFAULT_LANG
        if time_dimension not in VALID_TIME_DIMENSIONS:
            time_dimension = DEFAULT_TIME_DIMENSION
        fetch_now = datetime.fromtimestamp(now, tz)
        range_start, range_end = usage_date_range(time_dimension, tz, fetch_now)

        # 同一个网页登录态只请求一次，再从同一响应拆分各 API Key，避免 N 倍请求和 429。
        keys_by_token = {}
        for acc in accounts:
            token = (acc.get("platform_token") or "").strip()
            api_key = (acc.get("api_key") or "").strip()
            if token:
                keys_by_token.setdefault(token, [])
                if api_key and api_key not in keys_by_token[token]:
                    keys_by_token[token].append(api_key)
        usage_by_token = {}
        for token, api_keys in keys_by_token.items():
            usage_by_token[token] = fetch_platform_usage(
                token, api_keys, tz, lang, time_dimension, fetch_now)

        expected_start = range_start.isoformat()
        expected_end = range_end.isoformat()
        for acc in accounts:
            name = acc.get("name") or "默认"
            token = (acc.get("platform_token") or "").strip()
            snap = self._fetch_account(
                acc, name, tz, usage_by_token.get(token), lang, time_dimension, fetch_now)
            p = prev.get(name)               # 瞬时失败时保留同一时间区间的上次数值
            if p is not None:
                if snap.get("balance") is None and p.get("balance") is not None:
                    snap["balance"] = p["balance"]
                    snap["currency"] = snap.get("currency") or p.get("currency")
                    snap["data_stale"] = True
                same_range = (p.get("range_key") == time_dimension
                              and p.get("range_start") == expected_start
                              and p.get("range_end") == expected_end)
                if snap.get("fetch_failed") and same_range:
                    for key in self._USAGE_KEYS:
                        if snap.get(key) is None and p.get(key) is not None:
                            snap[key] = p[key]
                    snap["data_stale"] = True
                if snap.get("data_stale"):
                    snap["warnings"].append(tr(lang, "stale_warning", name=name))
            acc_snaps[name] = snap
            warnings.extend(snap["warnings"])
        prune_state(list(acc_snaps.keys()))
        return {"ts": now, "ok": True, "warnings": warnings,
                "accounts": acc_snaps, "names": [a.get("name") or "默认" for a in accounts],
                "tz_name": tz_name, "language": lang,
                "time_dimension": time_dimension,
                "range_start": expected_start, "range_end": expected_end}

    def _fetch_account(self, acc, name, tz=CN_TZ, td=None, lang=DEFAULT_LANG,
                       time_dimension=DEFAULT_TIME_DIMENSION, now=None):
        api_key = (acc.get("api_key") or "").strip()
        token = (acc.get("platform_token") or "").strip()
        range_start, range_end = usage_date_range(time_dimension, tz, now)
        snap = {"name": name, "ok": True, "warnings": [],
                "ds_account": (acc.get("ds_account") or "").strip() or "默认",
                "balance": None, "today_cost": None, "today_cost_src": None,
                "est_today_cost": None, "today_date": range_end.isoformat(),
                "range_key": time_dimension, "range_start": range_start.isoformat(),
                "range_end": range_end.isoformat(),
                "today_cost_my": None, "tokens_my": None, "detail_my": None,
                "tokens": None, "detail": {}, "by_model": {}, "by_model_my": {},
                "currency": None,
                "token_configured": bool(token), "today_failed": False,
                "balance_failed": False, "fetch_failed": False, "data_stale": False,
                "per_key_available": False, "per_key_error": None,
                "token_id": hashlib.sha1(token.encode()).hexdigest()[:12] if token else None}

        # 1) 余额
        if api_key:
            try:
                bal = fetch_balance(api_key)
                if bal:
                    snap["balance"] = bal
                    snap["currency"] = bal["currency"]
                    # 本地日的实时消耗估算（按余额差值，每次刷新都更新）
                    snap["est_today_cost"] = update_day_state(name, bal["total"], tz)
                    if not token and time_dimension == TIME_TODAY:
                        snap["today_cost"] = snap["est_today_cost"]
                        snap["today_cost_src"] = "估算"
            except Exception as e:
                snap["balance_failed"] = True
                snap["warnings"].append(tr(lang, "balance_error", name=name, error=short_err(e)))
        else:
            snap["warnings"].append(tr(lang, "missing_key", name=name))

        # 2) 今日用量（官方，按所选时区自然日；by_api_key 可细分到单个 key）
        if token:
            if td and td.get("ok"):
                snap["today_cost"] = td["cost"]           # 账户级（全部 key）
                snap["today_cost_src"] = "官方"
                snap["today_date"] = td.get("date") or range_end.isoformat()
                snap["range_key"] = td.get("range_key") or time_dimension
                snap["range_start"] = td.get("range_start") or range_start.isoformat()
                snap["range_end"] = td.get("range_end") or range_end.isoformat()
                snap["tokens"] = td["tokens"]
                snap["detail"] = td
                snap["by_model"] = td.get("by_model") or {}
                by_key = td.get("by_key") or {}
                mine = by_key.get(api_key) if api_key else None
                if mine is not None:
                    snap["today_cost_my"] = mine["cost"]  # 本 key 单独
                    snap["tokens_my"] = mine["tokens"]
                    snap["detail_my"] = mine
                    snap["by_model_my"] = mine.get("by_model") or {}
                    snap["per_key_available"] = True
                elif api_key:
                    snap["per_key_error"] = tr(lang, "no_key_detail")
                if td.get("currency"):
                    snap["currency"] = td["currency"]
            else:
                snap["today_failed"] = True
                err = (td or {}).get("err") or tr(lang, "no_data")
                snap["warnings"].append(tr(lang, "usage_error", name=name, error=err))
        snap["fetch_failed"] = bool(snap["balance_failed"] or snap["today_failed"])
        return snap

    # ── UI 轮询渲染 ──
    def _poll(self):
        try:
            while True:
                snap = self.queue.get_nowait()
                if snap.get("time_dimension", DEFAULT_TIME_DIMENSION) != self._time_dimension:
                    continue                    # 丢弃切换维度前仍在途的旧请求结果
                self.snap = snap
                self._render_all(snap)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _render_all(self, snap):
        if snap is None:
            return
        if snap.get("time_dimension", DEFAULT_TIME_DIMENSION) != self._time_dimension:
            return
        accs = snap.get("accounts") or {}
        self._tz_name = snap.get("tz_name") or DEFAULT_TZ   # 同步时区（托盘勾选/状态栏）

        # 按 DS账号 分组：DS账号名称 -> [API Key名称]
        groups = {}
        for s in accs.values():
            ds = (s.get("ds_account") or "").strip() or "默认"
            groups.setdefault(ds, []).append(s.get("name") or "默认")
        if groups != self._account_groups:
            self._account_groups = groups
            self._rebuild_tray_menu()          # 分组变化 → 重建托盘账户菜单

        # 校验/保持选择
        ds_list = list(groups.keys())
        if self._selected_ds not in groups:
            self._selected_ds = ds_list[0] if ds_list else ""
        self.selected_ds.set(self._selected_ds)
        self.lbl_ds.config(text=self._selected_ds)
        all_label = self._all_label()
        valid_views = [all_label] + groups.get(self._selected_ds, [])
        cur_values = [str(v) for v in (self.combo["values"] or ())]
        if cur_values != valid_views:
            self.combo["values"] = valid_views
        if self.selected.get() not in valid_views:
            self.selected.set(all_label)
        self._selected = self.selected.get()

        group_snaps = [accs[n] for n in groups.get(self._selected_ds, []) if n in accs]
        if self._selected == all_label:
            # 「账号总量」：该 DS账号 下账户级合计（同平台多 key 只计一次）
            view = aggregate_view(group_snaps, snap.get("ts", time.time()), self._lang)
        else:
            single = accs.get(self._selected)
            if single is None:
                view = aggregate_view(group_snaps, snap.get("ts", time.time()), self._lang)
            else:
                # 单 key 视图绝不把多 Key 的账户总量冒充该 Key 明细。
                has_my = bool(single.get("per_key_available"))
                allow_account = len(group_snaps) == 1
                show_usage = has_my or allow_account
                cost_key = "today_cost_my" if has_my else ("today_cost" if allow_account else "_none")
                money = snap_to_money(single, cost_key)
                raw_models = (single.get("by_model_my") if has_my else
                              (single.get("by_model") if allow_account else {}))
                model_usage = {}
                merge_model_usage(
                    model_usage, raw_models,
                    single.get("currency") or (single.get("balance") or {}).get("currency"))
                one_warnings = list(single.get("warnings") or [])
                if not show_usage:
                    one_warnings.append(single.get("per_key_error") or self._t("cannot_split"))
                view = {
                    "money": money,
                    "tokens": (single.get("tokens_my") if has_my else
                               (single.get("tokens") if allow_account else None)),
                    "detail": (single.get("detail_my") if has_my else
                               (single.get("detail") if allow_account else {})) or {},
                    "model_usage": model_usage,
                    "today_cost_src": single.get("today_cost_src") if show_usage else None,
                    "today_date": single.get("today_date"),
                    "range_key": single.get("range_key") or self._time_dimension,
                    "range_start": single.get("range_start"),
                    "range_end": single.get("range_end"),
                    "token_configured": single.get("token_configured"),
                    "token_coverage": (1, 1) if show_usage and single.get("tokens") is not None else (0, 1),
                    "data_stale": single.get("data_stale"),
                    "warnings": one_warnings,
                    "ts": snap.get("ts", time.time()),
                    "n_accounts": 1,
                    "is_all": False,
                }
        # 模型列表完全来自当前接口响应；不在代码中预置具体模型名。
        model_names = sorted((view.get("model_usage") or {}).keys(), key=str.casefold)
        valid_models = [self._all_models_label()] + model_names
        current_model_values = [str(v) for v in (self.model_combo["values"] or ())]
        if current_model_values != valid_models:
            self.model_combo["values"] = valid_models
        if self.selected_model.get() not in valid_models:
            self.selected_model.set(self._all_models_label())
        self._selected_model = self.selected_model.get()
        view = self._apply_model_filter(view)

        self._render_view(view)
        self._update_tray_tooltip(view)
        self._fit_pin()                        # 内容宽度变化时自适应尺寸并保持贴右下角

    def _update_tray_tooltip(self, view):
        if self.icon is None:
            return
        try:
            topped = view["money"]["topped_up"]
            text = f"{APP_NAME} · {self._t('balance')} {fmt_multi(topped, 2)}"
            if view.get("is_all") and view.get("n_accounts", 1) > 1:
                text += self._t("api_tooltip_count", count=view["n_accounts"])
            self.icon.title = text
        except Exception:
            pass

    def _render_view(self, view):
        c = self._colors
        money = view["money"]

        # 充值余额
        topped = money["topped_up"]
        if not topped:
            self.lbl_balance.config(text=f"{self._t('balance')}  —")
            self.lbl_balance_sub.config(text="")
        else:
            self.lbl_balance.config(
                text=f"{self._t('balance')}  {fmt_multi(topped, 2)}",
                fg=c["ok"])
            sub = f"{self._t('granted')} {fmt_multi(money['granted'], 2)}"
            self.lbl_balance_sub.config(text=sub)

        # 所选时间维度消费
        today = money["today"]
        tcs = view.get("today_cost_src")
        today_label = self._usage_label("cost")
        if not today:
            self.lbl_today.config(text=f"{today_label}  —", fg=c["dim"])
        else:
            tags = {"官方": self._t("official_tag"), "估算": self._t("estimate_tag"),
                    "混合": self._t("mixed_tag")}
            tag = "  " + tags.get(tcs, self._t("unknown_tag"))
            if view.get("data_stale"):
                tag += " " + self._t("stale_tag")
            fg = c["fg"] if tcs == "官方" and not view.get("data_stale") else c["warn"]
            self.lbl_today.config(text=f"{today_label}  {fmt_multi(today)}{tag}", fg=fg)

        # 所选时间维度 API 请求次数
        reqs = (view.get("detail") or {}).get("requests", 0)
        if view.get("tokens") is not None:
            self.lbl_today_req.config(text=f"{self._usage_label('requests')}  {reqs:,}", fg=c["fg"])
        else:
            self.lbl_today_req.config(text=f"{self._usage_label('requests')}  —", fg=c["dim"])

        # 所选时间维度 Tokens
        tk_ = view.get("tokens")
        if tk_ is None:
            hint = self._t("fetch_no_data") if view.get("token_configured") else self._t("need_token")
            self.lbl_tokens.config(text=f"{self._usage_label('tokens')}  —  {hint}", fg=c["dim"])
            self.lbl_tokens_sub.config(text="")
        else:
            d = view.get("detail") or {}
            prompt = d.get("prompt", 0)
            cache_rate = (d.get("cache_hit", 0) / prompt * 100) if prompt > 0 else 0
            self.lbl_tokens.config(text=f"{self._usage_label('tokens')}  {fmt_tokens(tk_)}", fg=c["fg"])
            sub = f"{self._t('input')} {prompt:,} · {self._t('output')} {d.get('output', 0):,}"
            if d.get("cache_hit"):
                sub += f" · {self._t('cache_hit')} {cache_rate:.0f}%"
            covered, total = view.get("token_coverage") or (0, 0)
            if total and covered < total:
                sub += " · " + self._t("coverage", covered=covered, total=total)
            self.lbl_tokens_sub.config(text=sub, fg=c["dim"])

        # 状态栏
        ts = datetime.fromtimestamp(view.get("ts", time.time())).strftime("%H:%M:%S")
        warns = view.get("warnings") or []
        if warns:
            status = "⚠ " + self._t("warning_join").join(str(w) for w in warns[:2]) + (" …" if len(warns) > 2 else "")
            fg = c["warn"]
        else:
            mode = {"官方": self._t("official_mode"), "估算": self._t("estimate_mode"),
                    "混合": self._t("mixed_mode")}.get(view.get("today_cost_src"), self._t("waiting"))
            if view.get("data_stale"):
                mode += " · " + self._t("last_success")
            scope = (" · " + self._t("api_count", count=view["n_accounts"])) \
                if view.get("is_all") and view.get("n_accounts", 1) > 1 else ""
            status = f"✓ {mode}{scope} · {self._tz_name} · {self._t('updated', time=ts)}"
            fg = c["dim"]
        self.lbl_status.config(text=status, fg=fg)

    # ── 设置对话框 ──
    def open_settings(self):
        cfg = load_config()
        accounts = cfg["accounts"]

        win = tk.Toplevel(self.root)
        win.title(self._t("settings_title"))
        win.configure(bg="#14161a")
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        show_secrets = tk.BooleanVar(value=False)
        secret_entries = []

        def entry(parent, row, label, var, width=40, secret=False):
            tk.Label(parent, text=label, bg="#14161a", fg="#d8dee6",
                     font=("Microsoft YaHei UI", 9)).grid(row=row, column=0, sticky="w", padx=8, pady=3)
            e = tk.Entry(parent, textvariable=var, width=width, bg="#1f242c", fg="#e8edf3",
                         insertbackground="#e8edf3", relief="flat",
                         show="" if not secret else "•")
            e.grid(row=row, column=1, padx=8, pady=3, sticky="we")
            if secret:
                secret_entries.append(e)
            return e

        # 选中账户的字段变量
        vars_ = {}
        vars_["ds_account"] = tk.StringVar()
        vars_["name"] = tk.StringVar()
        vars_["api_key"] = tk.StringVar()
        vars_["platform_token"] = tk.StringVar()
        g = {}
        g["refresh_seconds"] = tk.StringVar(value=str(cfg.get("refresh_seconds", 30)))
        g["pin_corner"] = tk.BooleanVar(value=bool(cfg.get("pin_corner", True)))

        def fill(idx):
            acc = accounts[idx]
            vars_["ds_account"].set(acc.get("ds_account") or self._t("default"))
            vars_["name"].set(acc["name"])
            vars_["api_key"].set(acc.get("api_key") or "")
            vars_["platform_token"].set(acc.get("platform_token") or "")

        def flush(idx):
            """把输入框写回指定账户。"""
            acc = accounts[idx]
            acc["ds_account"] = vars_["ds_account"].get().strip() or self._t("default")
            acc["name"] = vars_["name"].get().strip() or self._t("default")
            acc["api_key"] = vars_["api_key"].get().strip()
            acc["platform_token"] = vars_["platform_token"].get().strip()

        acc_cur = [0]                      # 当前编辑的账户索引（列表，供闭包修改）
        self._acc_filtered = []            # 当前过滤后显示的账户索引

        def account_label(idx):
            acc = accounts[idx]
            return f"{acc.get('ds_account') or self._t('default')} / {acc['name']}"

        search_trace_suspended = [False]

        def set_search_without_refresh(value):
            """修改搜索框但不触发 trace，避免删除后用旧输入覆盖相邻账户。"""
            search_trace_suspended[0] = True
            try:
                search_var.set(value)
            finally:
                search_trace_suspended[0] = False

        def selected_account_index():
            """以 Listbox 当前高亮项为准，避免 acc_cur 因事件延迟而指向旧账户。"""
            selected = self._acc_list.curselection()
            if selected and selected[0] < len(self._acc_filtered):
                return self._acc_filtered[selected[0]]
            return acc_cur[0]

        def apply_filter(keep_cur=True):
            q = search_var.get().strip().lower()
            if not q:
                self._acc_filtered = list(range(len(accounts)))
            else:
                self._acc_filtered = [i for i, a in enumerate(accounts)
                                      if q in (a.get("ds_account") or "").lower()
                                      or q in (a.get("name") or "").lower()]
            self._acc_list.delete(0, "end")
            for i in self._acc_filtered:
                self._acc_list.insert("end", account_label(i))
            if not self._acc_filtered:
                return
            if keep_cur:
                try:
                    pos = self._acc_filtered.index(acc_cur[0])
                except ValueError:
                    pos = 0
                    acc_cur[0] = self._acc_filtered[0]
            else:
                pos = 0
                acc_cur[0] = self._acc_filtered[0]
            self._acc_list.selection_clear(0, "end")
            self._acc_list.selection_set(pos)
            self._acc_list.see(pos)
            fill(acc_cur[0])

        def on_select(_=None):
            sel = self._acc_list.curselection()
            if sel:
                flush(acc_cur[0])          # 切换前保存当前编辑
                acc_cur[0] = self._acc_filtered[sel[0]]
                fill(acc_cur[0])

        def do_add():
            flush(acc_cur[0])
            accounts.append({"ds_account": self._t("default"), "name": self._t("new_key"),
                             "api_key": "", "platform_token": ""})
            set_search_without_refresh("")  # 清空搜索显示全部
            acc_cur[0] = len(accounts) - 1
            apply_filter(keep_cur=True)     # 添加后选中新账户，而不是跳回第一项

        def do_del():
            if len(accounts) <= 1:
                messagebox.showinfo(APP_NAME, self._t("keep_one"), parent=win)
                return
            target = selected_account_index()
            flush(acc_cur[0])
            accounts.pop(target)
            acc_cur[0] = min(target, len(accounts) - 1)
            set_search_without_refresh("")
            apply_filter(keep_cur=True)

        def do_move(delta):
            target = selected_account_index()
            destination = target + delta
            if destination < 0 or destination >= len(accounts):
                return
            flush(acc_cur[0])
            accounts[target], accounts[destination] = accounts[destination], accounts[target]
            acc_cur[0] = destination
            set_search_without_refresh("")
            apply_filter(keep_cur=True)

        # 搜索框 + 账户列表区
        search_var = tk.StringVar()
        tk.Label(win, text=self._t("search_hint"),
                 bg="#14161a", fg="#7d8794", font=("Microsoft YaHei UI", 8)
                 ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 0))
        search_entry = tk.Entry(win, textvariable=search_var, bg="#1f242c", fg="#e8edf3",
                                insertbackground="#e8edf3", relief="flat",
                                font=("Microsoft YaHei UI", 9))
        search_entry.grid(row=1, column=0, columnspan=3, sticky="we", padx=8, pady=(2, 4))
        tk.Label(win, text=self._t("account_list"), bg="#14161a", fg="#7d8794",
                 font=("Microsoft YaHei UI", 8)).grid(row=2, column=0, columnspan=3, sticky="w", padx=8)
        list_frame = tk.Frame(win, bg="#14161a")
        list_frame.grid(row=3, column=0, columnspan=3, sticky="we", padx=8)
        self._acc_list = tk.Listbox(list_frame, height=8, bg="#1f242c", fg="#e8edf3",
                                    selectbackground="#1f6feb", relief="flat",
                                    font=("Microsoft YaHei UI", 9), exportselection=False)
        self._acc_list.pack(side="left", fill="x", expand=True)
        self._acc_list.bind("<<ListboxSelect>>", on_select)
        btn_col = tk.Frame(list_frame, bg="#14161a")
        btn_col.pack(side="left", padx=(6, 0))
        tk.Button(btn_col, text=self._t("add"), command=do_add,
                  bg="#2a303a", fg="#d8dee6", relief="flat", padx=8, pady=1).pack(fill="x")
        tk.Button(btn_col, text=self._t("delete"), command=do_del,
                  bg="#2a303a", fg="#d8dee6", relief="flat", padx=8, pady=1).pack(fill="x", pady=(3, 0))
        tk.Button(btn_col, text=self._t("move_up"), command=lambda: do_move(-1),
                  bg="#2a303a", fg="#d8dee6", relief="flat", padx=8, pady=1).pack(
                      fill="x", pady=(3, 0))
        tk.Button(btn_col, text=self._t("move_down"), command=lambda: do_move(1),
                  bg="#2a303a", fg="#d8dee6", relief="flat", padx=8, pady=1).pack(
                      fill="x", pady=(3, 0))
        def on_search_change(*_):
            if search_trace_suspended[0]:
                return
            flush(acc_cur[0])             # 搜索前保留当前未保存编辑，避免字段被 fill 覆盖
            apply_filter()
        search_var.trace_add("write", on_search_change)

        # 账户字段 + 全局
        row = 4
        entry(win, row, self._t("ds_account_field"), vars_["ds_account"]); row += 1
        entry(win, row, self._t("key_name_field"), vars_["name"]); row += 1
        entry(win, row, self._t("api_key_field"), vars_["api_key"], secret=True); row += 1
        entry(win, row, self._t("token_field"), vars_["platform_token"], secret=True); row += 1
        tk.Checkbutton(win, text=self._t("show_secrets"), variable=show_secrets,
                       command=lambda: [e.config(show="" if show_secrets.get() else "•")
                                        for e in secret_entries],
                       bg="#14161a", fg="#9aa4b2", selectcolor="#1f242c",
                       activebackground="#14161a", activeforeground="#e8edf3"
                       ).grid(row=row, column=1, sticky="w", padx=8, pady=(0, 3))
        row += 1
        tk.Label(win, text=self._t("global"), bg="#14161a", fg="#7d8794",
                 font=("Microsoft YaHei UI", 8)).grid(row=row, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 0))
        row += 1
        entry(win, row, self._t("refresh_field"), g["refresh_seconds"]); row += 1
        tk.Checkbutton(win, text=self._t("pin_field"),
                       variable=g["pin_corner"], bg="#14161a", fg="#9aa4b2",
                       selectcolor="#1f242c", activebackground="#14161a",
                       activeforeground="#e8edf3").grid(
                           row=row, column=0, columnspan=2, sticky="w", padx=8, pady=3)
        row += 1

        apply_filter()

        saved_after = [None]

        def flash_saved():
            if saved_after[0]:
                win.after_cancel(saved_after[0])
            saved_lbl.config(text=self._t("saved"))
            saved_after[0] = win.after(3000, lambda: saved_lbl.config(text=""))

        def save():
            flush(acc_cur[0])
            try:
                cfg["refresh_seconds"] = max(5, int(float(g["refresh_seconds"].get())))
            except ValueError:
                messagebox.showerror(APP_NAME, self._t("refresh_number"), parent=win)
                return
            cfg["pin_corner"] = bool(g["pin_corner"].get())
            if save_config(cfg):
                # 立即同步左侧列表中的账号名/Key 名，同时保持当前编辑项和搜索状态。
                try:
                    pos = self._acc_filtered.index(acc_cur[0])
                    self._acc_list.delete(pos)
                    self._acc_list.insert(pos, account_label(acc_cur[0]))
                    self._acc_list.selection_clear(0, "end")
                    self._acc_list.selection_set(pos)
                    self._acc_list.see(pos)
                except ValueError:
                    apply_filter(keep_cur=True)
                self._pin_var.set(cfg["pin_corner"])
                self._pin_corner = cfg["pin_corner"]
                if self._pin_corner:
                    self._fit_pin(force=True)
                self.wake.set()          # 立即用新配置刷新
                flash_saved()            # 不关闭窗口，仅提示已保存
            else:
                messagebox.showerror(APP_NAME, self._t("save_failed"), parent=win)

        btns = tk.Frame(win, bg="#14161a")
        btns.grid(row=row + 1, column=0, columnspan=3, pady=10)
        saved_lbl = tk.Label(btns, text="", bg="#14161a", fg="#69db7c",
                             font=("Microsoft YaHei UI", 9))
        saved_lbl.pack(side="left", padx=2)          # 「已保存」在保存按钮左侧
        tk.Button(btns, text=self._t("save"), command=save, bg="#1f6feb", fg="white",
                  relief="flat", padx=16, pady=2).pack(side="left", padx=6)
        tk.Button(btns, text=self._t("token_help"),
                  command=lambda: messagebox.showinfo(APP_NAME, usertoken_help(self._lang), parent=win),
                  bg="#2a303a", fg="#d8dee6", relief="flat", padx=10, pady=2).pack(side="left", padx=6)
        tk.Button(btns, text=self._t("cancel"), command=win.destroy, bg="#2a303a", fg="#d8dee6",
                  relief="flat", padx=10, pady=2).pack(side="left", padx=6)

        win.update_idletasks()
        w, h = win.winfo_reqwidth() + 24, win.winfo_reqheight() + 12
        # 默认居中于桌面
        x = max(0, (win.winfo_screenwidth() - w) // 2)
        y = max(0, (win.winfo_screenheight() - h) // 2)
        win.geometry(f"{w}x{h}+{x}+{y}")


def main():
    try:  # 高分屏清晰显示
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.attributes("-topmost", True)
    app = MonitorApp(root)

    cfg = load_config()
    if not any((a.get("api_key") or "").strip() for a in cfg["accounts"]):
        root.after(400, app.open_settings)   # 首次运行：弹出设置
    root.mainloop()


if __name__ == "__main__":
    main()
