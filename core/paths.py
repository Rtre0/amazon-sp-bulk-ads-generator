# -*- coding: utf-8 -*-
"""统一的「本地数据 / 配置」目录解析。

为什么要这一层
--------------
``data/``（SQLite 数据库）和 ``config/``（赛狐登录态）都是**只属于本机**的东西，
绝不该进 Git。靠 ``.gitignore`` 排除只是「规则约束」，一旦有人 ``git add -f``
或者换了工具就可能泄露。

这里改成**物理隔离**：只要设置了环境变量 ``SELLFOX_HOME``，数据库和 Cookie 就
落在项目目录之外，项目文件夹只剩纯代码，``git push`` 零风险。

优先级
------
1. 环境变量 ``SELLFOX_HOME``（推荐，指向项目外的本地数据目录）
2. 未设置 → 回退到项目根目录（保持老用法不变，向后兼容）

目录布局（``SELLFOX_HOME`` 指向哪，下面就在哪）
---------------------------------------------
::

    <SELLFOX_HOME>/
      data/
        local_store.db          # 商品库 + 每日搜索词/ASIN 报告
        scheduler_config.json   # 定时任务时间
        ja_zh_dict.json         # 日中名词词典
        chrome-login-profile/   # 半自动登录用的持久化浏览器配置
      config/
        sf_cookies.json         # 赛狐登录态（多账号）
        sf_accounts.json        # 「记住密码」的账号密码（可选）
"""

from __future__ import annotations

import os
from typing import Any, Dict

# 项目根目录（本文件在 core/ 下）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_ENV_RAW = (os.environ.get("SELLFOX_HOME") or "").strip()

#: 是否启用了外部数据目录
EXTERNAL = bool(_ENV_RAW)

#: 数据根目录：环境变量优先，否则项目根目录
HOME_DIR = os.path.abspath(_ENV_RAW) if _ENV_RAW else BASE_DIR

DATA_DIR = os.path.join(HOME_DIR, "data")
CONFIG_DIR = os.path.join(HOME_DIR, "config")

# ---- 具体文件 ----
DB_PATH = os.path.join(DATA_DIR, "local_store.db")
COOKIE_FILE = os.path.join(CONFIG_DIR, "sf_cookies.json")
CREDENTIALS_FILE = os.path.join(CONFIG_DIR, "sf_accounts.json")
SCHEDULER_CONFIG_FILE = os.path.join(DATA_DIR, "scheduler_config.json")
DICT_FILE = os.path.join(DATA_DIR, "ja_zh_dict.json")
#: 半自动登录用的持久化浏览器 profile（保留设备信任，减少短信二次验证）
LOGIN_PROFILE_DIR = os.path.join(DATA_DIR, "chrome-login-profile")


def ensure_dirs() -> None:
    """确保 data/ 与 config/ 存在。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)


def describe() -> Dict[str, Any]:
    """给前端/自检用的路径说明（不含任何凭据内容）。"""
    return {
        "external": EXTERNAL,
        "home": HOME_DIR,
        "data_dir": DATA_DIR,
        "config_dir": CONFIG_DIR,
        "db_path": DB_PATH,
        "db_exists": os.path.exists(DB_PATH),
        "db_size": os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0,
        "cookie_file": COOKIE_FILE,
        "cookie_exists": os.path.exists(COOKIE_FILE),
    }
