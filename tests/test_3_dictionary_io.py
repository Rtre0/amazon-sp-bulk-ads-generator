# -*- coding: utf-8 -*-
"""v6 验收测试三：本地纯净名词词库的持久化与一键导入。

全部使用独立临时词库，零污染正式 data/ja_zh_dict.json。
"""
import json
import os
import shutil
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

from core.translator import DEFAULT_NOUN_DICT, NON_NOUN_BLACKLIST, NounDictionaryMatcher  # noqa: E402

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


TMP = tempfile.mkdtemp(prefix="v6dict_")
DICT = os.path.join(TMP, "ja_zh_dict.json")

print("=" * 78)
print("v6 验收三：词库持久化 / 导入 / 管理")
print("=" * 78)
print(f"临时词库: {DICT}")

# ---------------------------------------------------------------- 1. 首次初始化
print("\n[1] 首次初始化（无文件）-> 写入种子词库")
m = NounDictionaryMatcher(dict_path=DICT)
check("文件已自动创建", os.path.exists(DICT))
check("条目数 = 种子词数", len(m.dictionary) == len(DEFAULT_NOUN_DICT),
      f"{len(m.dictionary)} / {len(DEFAULT_NOUN_DICT)}")
check("种子词可命中", m.match_first_noun("ステンレス包丁立て") == "菜刀架")

# ---------------------------------------------------------------- 2. 一键导入
print("\n[2] 一键导入 AI 清洗后的纯净词库")
IMPORT = {
    "陳列架子": "陈列架",
    "店舗用陳列架子": "陈列架",
    "包丁立て": "菜刀架",
    "カッティングボード": "砧板",
    "オットマン": "脚凳",
    "スツール": "凳子",
    "折りたたみテーブル": "折叠桌",
    "テーブル": "桌子",
    "オフィスチェア": "办公椅",
    "生豆ロースター": "生豆烘焙器",
    "止水板": "挡水板",
}
res = m.update_dictionary(IMPORT)
print(f"     imported={res['imported']}  purged={res['purged']}  total={res['total']}")
check("导入计数 > 0", res["imported"] > 0, str(res["imported"]))
check("新词条已生效", m.match_first_noun("生豆ロースター 業務用") == "生豆烘焙器")
check("导入结果已持久化",
      json.load(open(DICT, encoding="utf-8")).get("生豆ロースター") == "生豆烘焙器")

# ---------------------------------------------------------------- 3. 黑名单拦截
print("\n[3] 非纯名词黑名单拦截")
BAD = {"折りたたみ": "折叠", "大型": "大型", "収納": "收纳", "キャンプ": "露营用品", "ペット": "宠物"}
res2 = m.update_dictionary(BAD)
print(f"     imported={res2['imported']}  purged={sorted(res2['purged'])}")
check("全部 5 个非纯名词被拒绝", sorted(res2["purged"]) == sorted(BAD.keys()), str(sorted(res2["purged"])))
check("黑名单词未写入内存", all(k not in m.dictionary for k in BAD))
check("黑名单词未写入磁盘",
      all(k not in json.load(open(DICT, encoding="utf-8")) for k in BAD))

# ---------------------------------------------------------------- 4. 增删改
print("\n[4] 单条增删改")
m.upsert("テスト名詞", "测试名词")
check("upsert 生效", m.match_first_noun("テスト名詞 セット") == "测试名词")
check("upsert 已持久化", json.load(open(DICT, encoding="utf-8")).get("テスト名詞") == "测试名词")
check("delete 返回 True", m.delete("テスト名詞") is True)
check("delete 后不再命中", m.match_first_noun("テスト名詞") != "测试名词")
check("delete 不存在的键返回 False", m.delete("存在しない") is False)

# ---------------------------------------------------------------- 5. 跨实例 / 热加载
print("\n[5] 跨实例复用 + 手工编辑热加载")
m2 = NounDictionaryMatcher(dict_path=DICT)
check("新实例读到相同条目", len(m2.dictionary) == len(m.dictionary), f"{len(m2.dictionary)}")
check("新实例可命中导入词", m2.match_first_noun("生豆ロースター") == "生豆烘焙器")

data = json.load(open(DICT, encoding="utf-8"))
data["オットマン"] = "脚凳（人工改名）"
json.dump(data, open(DICT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
time.sleep(1.1)  # 越过 mtime 节流
m2._cache.clear()
check("手工改 JSON 后热加载生效", m2.match_first_noun("オットマン") == "脚凳（人工改名）",
      m2.match_first_noun("オットマン"))

# ---------------------------------------------------------------- 6. reload
print("\n[6] reload 从磁盘重载")
n = m.reload()
check("reload 返回条目数", n == len(m.dictionary) and n > 0, str(n))

# ---------------------------------------------------------------- 7. 原子写入 / 容错
print("\n[7] 原子写入与容错")
check("无残留 .tmp 文件", not os.path.exists(DICT + ".tmp"))
# 损坏的 JSON -> 回落种子词，不抛异常
bad = os.path.join(TMP, "broken.json")
open(bad, "w", encoding="utf-8").write("{ this is not json ")
m3 = NounDictionaryMatcher(dict_path=bad)
check("损坏 JSON 回落种子词", len(m3.dictionary) == len(DEFAULT_NOUN_DICT), str(len(m3.dictionary)))
# 空 JSON 对象 -> 回落种子词
empty = os.path.join(TMP, "empty.json")
open(empty, "w", encoding="utf-8").write("{}")
m4 = NounDictionaryMatcher(dict_path=empty)
check("空 JSON 回落种子词", len(m4.dictionary) == len(DEFAULT_NOUN_DICT), str(len(m4.dictionary)))
# 非 dict 导入 -> 抛错
try:
    m.update_dictionary(["not", "a", "dict"])
    check("非 dict 导入应报错", False, "未抛异常")
except ValueError as exc:
    check("非 dict 导入抛 ValueError", True, str(exc)[:40])

# ---------------------------------------------------------------- 8. 导出
print("\n[8] 导出当前词库")
exported = m.export_dictionary()
check("导出内容与内存一致", exported == m.dictionary, f"{len(exported)} 条")
check("导出可被 JSON 序列化", isinstance(json.dumps(exported, ensure_ascii=False), str))

shutil.rmtree(TMP, ignore_errors=True)

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
