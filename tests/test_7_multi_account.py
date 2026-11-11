# -*- coding: utf-8 -*-
"""多账号账号池回归（隔离环境，不碰真实 Cookie / 数据库）。"""
import json
import os
import shutil
import tempfile

from core import sellfox_client as sf
from core import database as db

TMP = tempfile.mkdtemp(prefix="sf_acc_")
try:
    # ---- 隔离 ----
    sf.COOKIE_FILE = os.path.join(TMP, "sf_cookies.json")
    db.DB_DIR = TMP
    db.DB_PATH = os.path.join(TMP, "test.db")
    db._local.conn = None
    db.init_db()

    # ---- 1. 旧版扁平 Cookie 自动迁移 ----
    with open(sf.COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump({"sf_u": "user-1", "JSESSIONID": "abc"}, f)
    assert sf.load_cookies().get("sf_u") == "user-1"
    with open(sf.COOKIE_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw.get("_version") == 2 and len(raw["accounts"]) == 1, raw
    print("MIGRATE_OK", raw["accounts"][0]["id"], raw["accounts"][0]["label"])

    # ---- 2. 追加 3 个账号 → 共 4 个 ----
    for i in (2, 3, 4):
        info = sf.add_account(f"sf_u=user-{i}; JSESSIONID=s{i}", label=f"账号{i}")
        assert info and info["valid"], info
    accs = sf.list_accounts()
    assert len(accs) == 4, [a["label"] for a in accs]
    print("ADD_4_OK", [(a["label"], a["cookie_count"]) for a in accs])

    # ---- 3. 同一账号重复粘贴 → 只更新不新增 ----
    sf.add_account("sf_u=user-2; NEWKEY=1", label="账号2改")
    accs = sf.list_accounts()
    assert len(accs) == 4, len(accs)
    a2 = next(a for a in accs if a["label"] == "账号2改")
    assert a2["cookie_count"] == 3, a2   # sf_u + JSESSIONID + NEWKEY
    print("DEDUP_OK", a2["cookie_count"])

    # ---- 4. use_account 隔离 ----
    a1 = next(a for a in sf.list_accounts() if a["label"] == "默认账号")
    a3 = next(a for a in sf.list_accounts() if a["label"] == "账号3")
    with sf.use_account(a3["id"]):
        assert sf.load_cookies().get("sf_u") == "user-3", sf.load_cookies()
        sf.save_cookies({**sf.load_cookies(), "EXTRA": "x"})
    with sf.use_account(a1["id"]):
        assert sf.load_cookies().get("sf_u") == "user-1", sf.load_cookies()
        assert "EXTRA" not in sf.load_cookies(), "写串账号了"
    with sf.use_account(a3["id"]):
        assert sf.load_cookies().get("EXTRA") == "x"
    print("CTX_ISOLATION_OK")

    # ---- 5. 不存在的账号必须返回空，绝不回退到别的账号 ----
    with sf.use_account("acc_not_exist"):
        assert sf.load_cookies() == {}, sf.load_cookies()
    print("NO_FALLBACK_OK")

    # ---- 6. 切换 active / 删除 ----
    assert sf.set_active_account(a3["id"])
    assert sf.active_account_id() == a3["id"]
    assert sf.delete_account(a3["id"])
    assert sf.active_account_id() != a3["id"], "删掉 active 后应自动切换"
    assert len(sf.list_accounts()) == 3
    print("ACTIVE_DELETE_OK", sf.active_account_id())

    # ---- 7. 店铺归属 ----
    db.upsert_shop("560591", "大成-JP", account_id=a1["id"])
    db.upsert_shop("560601", "泽泽利塔-JP", account_id=a1["id"])
    db.upsert_shop("562589", "利达-JP", account_id="acc_other")
    assert len(db.list_shops(a1["id"])) == 2, db.list_shops(a1["id"])
    assert len(db.list_shops()) == 3
    # 空 account_id 不应抹掉已有归属
    db.upsert_shop("560591", "大成-JP")
    assert db.get_shop("560591")["account_id"] == a1["id"]
    print("SHOP_OWNER_OK")

    # ---- 8. sync_all_accounts 遍历账号且上下文正确 ----
    from core import sync_service

    seen = []

    def fake_search(target_date=None, days=1, account_id=""):
        acc = sf.get_account(account_id or None)
        seen.append(("search", account_id, acc["label"], sf.load_cookies().get("sf_u")))
        return [{"shop_id": "1", "shop_name": "店", "search_terms_inserted": 1}]

    def fake_product(account_id=""):
        acc = sf.get_account(account_id or None)
        seen.append(("product", account_id, acc["label"], sf.load_cookies().get("sf_u")))
        return [{"shop_id": "1", "shop_name": "店", "new_inserted": 1}]

    orig_s, orig_p = sync_service.sync_search_terms_all_shops, sync_service.sync_products_all_shops
    sync_service.sync_search_terms_all_shops = fake_search
    sync_service.sync_products_all_shops = fake_product
    try:
        out = sync_service.sync_all_accounts(kind="both", days=1)
    finally:
        sync_service.sync_search_terms_all_shops = orig_s
        sync_service.sync_products_all_shops = orig_p

    assert len(out["accounts"]) == 3, out["accounts"]
    assert len(seen) == 6, seen   # 3 账号 × 2 任务
    for kind, aid, label, uid in seen:
        acc = next(a for a in sf.list_accounts() if a["id"] == aid)
        assert uid == sf.get_account(aid)["cookies"].get("sf_u"), (aid, uid)
    print("SYNC_ALL_OK", [(k, l, u) for k, _, l, u in seen])

    # ---- 9. 无效账号被跳过 ----
    with open(sf.COOKIE_FILE, encoding="utf-8") as f:
        store = json.load(f)
    store["accounts"][0]["cookies"] = {"foo": "bar"}   # 没有 sf_* → invalid
    with open(sf.COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f)
    out = sync_service.sync_all_accounts(kind="search")
    skipped = [a for a in out["accounts"] if a.get("skipped")]
    assert skipped, out["accounts"]
    print("SKIP_INVALID_OK", skipped[0]["error"])

    print("ALL_ACCOUNT_TESTS_PASS")
finally:
    try:
        db._local.conn.close()
    except Exception:
        pass
    shutil.rmtree(TMP, ignore_errors=True)
