# -*- coding: utf-8 -*-
"""把本地数据库 / Cookie / 词库迁移到项目外的数据目录。

背景
----
项目要上传 GitHub，但 ``data/``（SQLite）和 ``config/``（赛狐登录态）只属于本机。
靠 ``.gitignore`` 排除是「规则约束」，物理隔离更保险：
数据放到 ``SELLFOX_HOME`` 指向的目录，项目文件夹里就只剩纯代码。

用法
----
::

    # 预览（只打印，不动文件）
    python tools/migrate_to_external.py --dry-run

    # 复制到默认目录 %USERPROFILE%\\SellfoxAdsData，校验后删除项目内副本
    python tools/migrate_to_external.py --move

    # 指定别的位置
    python tools/migrate_to_external.py --target D:\\SellfoxData --move

校验
----
复制完成后会对比源/目标的表行数（shops / products / ad_search_terms_daily /
ad_search_asin_daily）和 Cookie 账号数，**不一致就中止，不删任何文件**。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SRC_DATA = os.path.join(BASE_DIR, "data")
SRC_CONFIG = os.path.join(BASE_DIR, "config")

DB_NAME = "local_store.db"
TABLES = ["shops", "products", "ad_search_terms_daily", "ad_search_asin_daily"]
# 需要搬走的 data/ 内容（chrome 调试 profile 体积大且可再生，不搬）
DATA_ITEMS = [
    DB_NAME,
    DB_NAME + "-wal",
    DB_NAME + "-shm",
    "scheduler_config.json",
    "ja_zh_dict.json",
]
CONFIG_ITEMS = ["sf_cookies.json", "sf_accounts.json"]


def default_target() -> str:
    return os.path.join(os.path.expanduser("~"), "SellfoxAdsData")


def table_counts(db_path: str) -> dict:
    if not os.path.exists(db_path):
        return {}
    try:
        conn = sqlite3.connect(db_path)
        out = {}
        for t in TABLES:
            try:
                out[t] = conn.execute(f"select count(*) from {t}").fetchone()[0]
            except sqlite3.Error:
                out[t] = -1
        conn.close()
        return out
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def account_count(cookie_path: str) -> int:
    if not os.path.exists(cookie_path):
        return -1
    try:
        import json

        with open(cookie_path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and "accounts" in d:
            return len(d.get("accounts") or [])
        return 1 if d else 0
    except Exception:  # noqa: BLE001
        return -1


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移本地数据到项目外目录")
    ap.add_argument("--target", default=default_target(), help="目标目录（默认 %%USERPROFILE%%\\SellfoxAdsData）")
    ap.add_argument("--move", action="store_true", help="校验通过后删除项目内副本")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不动文件")
    args = ap.parse_args()

    target = os.path.abspath(os.path.expanduser(args.target))
    dst_data = os.path.join(target, "data")
    dst_conf = os.path.join(target, "config")

    print(f"源项目: {BASE_DIR}")
    print(f"目标  : {target}")
    print()

    src_db = os.path.join(SRC_DATA, DB_NAME)
    src_cookie = os.path.join(SRC_CONFIG, "sf_cookies.json")

    src_counts = table_counts(src_db)
    src_accounts = account_count(src_cookie)

    if not src_counts:
        print("❌ 项目内没有找到数据库，无需迁移。")
        return 1
    print("源数据库行数:")
    for k, v in src_counts.items():
        print(f"   {k:<24} {v}")
    print(f"   源账号数: {src_accounts}")
    print()

    if args.dry_run:
        print("（dry-run）将复制：")
        for name in DATA_ITEMS:
            p = os.path.join(SRC_DATA, name)
            if os.path.exists(p):
                print(f"   data/{name}  ({os.path.getsize(p)/1048576:.1f} MB)")
        for name in CONFIG_ITEMS:
            p = os.path.join(SRC_CONFIG, name)
            if os.path.exists(p):
                print(f"   config/{name}")
        print(f"\n目标目录已存在内容: {os.path.exists(dst_data)}")
        return 0

    os.makedirs(dst_data, exist_ok=True)
    os.makedirs(dst_conf, exist_ok=True)

    copied = []
    for name in DATA_ITEMS:
        src = os.path.join(SRC_DATA, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst_data, name))
            copied.append(f"data/{name}")
    for name in CONFIG_ITEMS:
        src = os.path.join(SRC_CONFIG, name)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dst_conf, name))
            copied.append(f"config/{name}")

    print("已复制:", ", ".join(copied) or "(无)")
    print()

    # ---- 校验 ----
    dst_counts = table_counts(os.path.join(dst_data, DB_NAME))
    dst_accounts = account_count(os.path.join(dst_conf, "sf_cookies.json"))
    ok = True
    print("校验:")
    for t in TABLES:
        s, d = src_counts.get(t, -1), dst_counts.get(t, -2)
        flag = "OK " if s == d else "❌ "
        if s != d:
            ok = False
        print(f"   {flag}{t:<24} 源 {s} / 目标 {d}")
    flag = "OK " if src_accounts == dst_accounts else "❌ "
    if src_accounts != dst_accounts:
        ok = False
    print(f"   {flag}账号数 {src_accounts} / {dst_accounts}")

    if not ok:
        print("\n❌ 校验不通过，已中止，项目内文件未删除。")
        return 2

    print("\n✅ 校验通过，目标数据完整。")

    if args.move:
        for name in DATA_ITEMS:
            p = os.path.join(SRC_DATA, name)
            if os.path.exists(p):
                os.remove(p)
        for name in CONFIG_ITEMS:
            p = os.path.join(SRC_CONFIG, name)
            if os.path.exists(p):
                os.remove(p)
        print("已删除项目内副本（data/ config/ 现在是空目录）。")
    else:
        print("未删除项目内副本（加 --move 才会删除）。")

    print()
    print("下一步：用「启动（数据外置）.bat」启动，或手动设置环境变量：")
    print(f'   set SELLFOX_HOME={target}')
    print("   venv\\Scripts\\python.exe main.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
