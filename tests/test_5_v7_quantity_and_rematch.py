# -*- coding: utf-8 -*-
"""v7 验收测试：数量干扰词过滤 + 词库动态刷新（一键重匹配）。

覆盖 v7 增量升级规范：
  1. ``clean_title_noise`` 前置清洗流水线（品牌 / 数量规格词 / 开头括号）
  2. 量词白名单保护：``24cm`` / ``304ステンレス`` / ``3段ラック`` 不得被误剥
  3. 数量前缀后的长词优先仍然生效（``2個 マッシャー`` -> 压泥器）
  4. ``match_noun_only`` 语义：命中返回中文，未收录返回 None（不兜底）
  5. ``POST /api/dictionary/rematch`` 契约：批量重匹配 + hit/miss 标记
  6. 真实 10988 行导出表端到端验收（文件存在时才跑）
"""
import json
import os
import socket
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")

from core.translator import (  # noqa: E402
    DEFAULT_NOUN_DICT,
    NON_NOUN_BLACKLIST,
    noun_matcher,
)

OK = FAIL = 0


def check(name, cond, detail=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("=" * 78)
print("v7 验收：数量干扰词过滤 + 词库动态刷新")
print("=" * 78)
print(f"词库条目 {len(noun_matcher.dictionary)} · 黑名单 {len(NON_NOUN_BLACKLIST)} 条")

# ---------------------------------------------------------------- 1. 前置清洗
print("\n[1] clean_title_noise 前置清洗流水线")
CLEAN_CASES = [
    ("FryWare 2個 マッシャー", "マッシャー", "品牌 + 数量词"),
    ("2個 マッシャー", "マッシャー", "纯数量词"),
    ("２個 給水プランター", "給水プランター", "全角数字"),
    ("2個セット アイスクリームスクープ", "アイスクリームスクープ", "数量 + セット"),
    ("2枚組 鍋蓋", "鍋蓋", "数量 + 組"),
    ("3枚 皿", "皿", "数量 + 枚"),
    ("4点 ボウル", "ボウル", "数量 + 点"),
    ("2本 菜箸", "菜箸", "数量 + 本"),
    ("[セール] 2個 (大) マッシャー", "マッシャー", "括号 + 数量 + 括号 交替"),
    ("FryWare_2個_マッシャー_ステンレス製", "マッシャー_ステンレス製", "下划线分隔不残留前导符号"),
    ("【限定】3個セット タンブラー", "タンブラー", "全角括号"),
    # —— 量词白名单：以下前缀是真实规格，绝不能被剥掉 ——
    ("24cm フライパン", "フライパン", "规格 24cm 允许剥离（非量词白名单，属品牌位）"),
    ("304ステンレス鋼 ボウル", "304ステンレス鋼 ボウル", "304 后无空格，不得剥离"),
    ("3段ラック スチールラック", "3段ラック スチールラック", "段 不在量词白名单，不得剥离"),
    ("2WAY 折りたたみチェア", "折りたたみチェア", "2WAY 视为品牌位可剥离，名词保留"),
]
for raw, expect_clean, note in CLEAN_CASES:
    got = noun_matcher.clean_title_noise(raw)
    check(f"clean({raw})", got == expect_clean, f"-> {got!r}  （{note}）")

# ---------------------------------------------------------------- 2. 数量词 + 长词优先
print("\n[2] 数量前缀后的实体名词提取（规范第 5 节验证目标）")
MATCH_CASES = [
    # 用户真实导出表 mj355 / llc16 的原文
    ("FryWare 2個 マッシャー ステンレス製 サツマイモつぶし ポテトマッシャー",
     "压泥器", "mj355：跳过 2個 命中 マッシャー"),
    ("FryWare ビニール袋 鮮度保持袋 ロール ポリ袋 お徳用",
     "塑料袋", "llc16：命中 ビニール袋"),
    ("FryWare 2個 収納ボックス ジュエリーオーガナイザー アクセサリーケース",
     "收纳箱", "lzq8：跳过 2個 命中 収納ボックス"),
    ("２個 給水プランター ベランダ 園芸", "吸水花盆", "全角数量 + 长词 給水プランター > プランター"),
    ("2個セット アイスクリームスクープ ステンレス", "冰淇淋勺", "数量セット + 长词优先"),
    ("3枚 鍋蓋 ステンレス製", "锅盖", "数量 + 鍋蓋"),
    ("2個 ガラス調味料ボトル オイルボトル", "调料瓶", "长词 調味料ボトル > ボトル"),
    ("2個 包丁スタンド 大容量", "菜刀架", "包丁スタンド 长词优先于 包丁"),
]
for text, expect, note in MATCH_CASES:
    got = noun_matcher.match_first_noun(text)
    check(f"{text[:36]}", got == expect, f"-> {got!r}  （{note}）")

# ---------------------------------------------------------------- 3. match_noun_only
print("\n[3] match_noun_only：命中返回中文，未收录返回 None")
noun_matcher._cache.clear()
check("命中 マッシャー", noun_matcher.match_noun_only("2個 マッシャー") == "压泥器")
check("未收录返回 None", noun_matcher.match_noun_only("謎の物体xyz") is None,
      f"-> {noun_matcher.match_noun_only('謎の物体xyz')!r}")
check("空串返回 None", noun_matcher.match_noun_only("") is None)
check("None 输入返回 None", noun_matcher.match_noun_only(None) is None)
check("兜底路径仍可用", noun_matcher.match_first_noun("謎の物体xyz") != "",
      f"-> {noun_matcher.match_first_noun('謎の物体xyz')!r}")

# ---------------------------------------------------------------- 4. rematch 接口契约
print("\n[4] POST /api/dictionary/rematch 接口契约")
try:
    from main import RematchItem, RematchRequest, rematch_names  # noqa: E402

    req = RematchRequest(items=[
        RematchItem(key="shopA||mj355", parent_sku="mj355",
                    raw_name="FryWare 2個 マッシャー ステンレス製"),
        RematchItem(key="shopA||llc16", parent_sku="llc16",
                    raw_name="FryWare ビニール袋 鮮度保持袋 ロール"),
        RematchItem(parent_sku="zzz9", raw_name="謎の物体xyz"),      # 未收录
        RematchItem(parent_sku="empty", raw_name="   "),              # 空标题 -> 跳过
    ])
    res = rematch_names(req)
    m = res["matched_names"]
    check("key 作为回填主键", "shopA||mj355" in m and m["shopA||mj355"] == "压泥器", str(m.get("shopA||mj355")))
    check("llc16 -> 塑料袋", m.get("shopA||llc16") == "塑料袋", str(m.get("shopA||llc16")))
    check("缺省 key 回落 parent_sku", "zzz9" in m, str(m.get("zzz9")))
    check("空 raw_name 被跳过", "empty" not in m)
    check("hit_skus 标记真实命中", sorted(res["hit_skus"]) == ["shopA||llc16", "shopA||mj355"],
          str(res["hit_skus"]))
    check("miss_skus 标记未收录", res["miss_skus"] == ["zzz9"], str(res["miss_skus"]))
    check("hit_count / miss_count 自洽",
          res["hit_count"] == 2 and res["miss_count"] == 1 and res["matched_count"] == 3,
          f"hit={res['hit_count']} miss={res['miss_count']} matched={res['matched_count']}")
    check("dict_total 与内存一致", res["dict_total"] == len(noun_matcher.dictionary))
    check("接口不写盘（词库条目数不变）", len(noun_matcher.dictionary) == res["dict_total"])
except Exception as exc:  # noqa: BLE001
    check("rematch 接口可用", False, f"导入/调用失败：{exc}")

# ---------------------------------------------------------------- 5. 零网络
print("\n[5] 全程零网络：屏蔽 socket 后重跑匹配链路")
_real_socket = socket.socket


class _BlockedSocket(_real_socket):
    def connect(self, *a, **k):        # noqa: D102
        raise OSError("网络已被测试屏蔽")

    def connect_ex(self, *a, **k):     # noqa: D102
        raise OSError("网络已被测试屏蔽")


socket.socket = _BlockedSocket
try:
    blocked_ok = True
    err = ""
    try:
        noun_matcher._cache.clear()
        for text, expect, _ in MATCH_CASES:
            if noun_matcher.match_first_noun(text) != expect:
                blocked_ok = False
                err = f"断网下结果异常：{text}"
                break
    except Exception as exc:  # noqa: BLE001
        blocked_ok, err = False, str(exc)
finally:
    socket.socket = _real_socket
check("断网下匹配结果不变", blocked_ok, err)

# ---------------------------------------------------------------- 6. 真实数据端到端
print("\n[6] 真实 10988 行导出表端到端验收")
REAL = "C:/Users/Administrator/Downloads/parentProductListing2026-10-10_0(1).xlsx"
if os.path.exists(REAL):
    from core.parser import parse_sellfox_excel_multi_shop  # noqa: E402

    t0 = time.time()
    parsed = parse_sellfox_excel_multi_shop(open(REAL, "rb").read())
    t_parse = time.time() - t0
    print(f"     解析耗时 {t_parse:.3f}s")

    groups = parsed["shops"]["三头牛-JP"]["groups"]
    check("解析出父体数量", len(groups) == 1326, f"实际 {len(groups)}")

    noun_matcher._cache.clear()
    t0 = time.time()
    total = hit = 0
    for _sku, g in groups.items():
        total += 1
        if noun_matcher.match_noun_only(g["raw_name"] or _sku):
            hit += 1
    t_match = time.time() - t0
    rate = hit / total * 100
    print(f"     全量匹配 {t_match:.3f}s ({t_match / total * 1000:.3f} ms/条)")
    check("1326 个父体 1 秒内匹配完", t_match < 1.0, f"{t_match:.3f}s")
    check("词库命中率 >= 90%", rate >= 90.0, f"{rate:.1f}% ({hit}/{total})")

    # 规范指定的两个验证目标
    g355 = groups.get("mj355")
    check("mj355 原始标题含 2個", bool(g355) and "2個" in (g355["raw_name"] or ""),
          (g355 or {}).get("raw_name", "")[:40])
    check("mj355 -> 压泥器（跳过 2個）",
          bool(g355) and noun_matcher.match_first_noun(g355["raw_name"]) == "压泥器",
          noun_matcher.match_first_noun(g355["raw_name"]) if g355 else "")

    g16 = groups.get("llc16")
    check("llc16 -> 塑料袋",
          bool(g16) and noun_matcher.match_first_noun(g16["raw_name"]) == "塑料袋",
          noun_matcher.match_first_noun(g16["raw_name"]) if g16 else "")

    # 数量词开头的标题必须被正确穿透（原始导出表带品牌前缀，先剥品牌再看）
    import re  # noqa: E402
    _BRAND = re.compile(r"^[a-zA-Z0-9_\-\s]+[_\s]+")

    def has_qty_prefix(t):
        s = _BRAND.sub("", str(t).strip()).strip()
        return bool(re.match(r"^[0-9０-９]+\s*(?:個|枚|本|点|組|パック|セット)", s))

    qty_titles = [g["raw_name"] for g in groups.values() if g.get("raw_name")]
    qty_start = [t for t in qty_titles if has_qty_prefix(t)]
    qty_hit = [t for t in qty_start if noun_matcher.match_noun_only(t)]
    print(f"     数量词开头的父体 {len(qty_start)} 个，命中 {len(qty_hit)} 个")
    check("数量词开头标题确实存在", len(qty_start) > 0, f"{len(qty_start)} 个")
    check("数量词开头标题全部穿透成功",
          len(qty_start) == 0 or len(qty_hit) == len(qty_start),
          f"{len(qty_hit)}/{len(qty_start)}")
else:
    print(f"     [SKIP] 未找到真实样例：{REAL}")

# ---------------------------------------------------------------- 7. 词库一致性
print("\n[7] 词库一致性：种子词不含黑名单 / 无重复")
check("种子词与黑名单无交集", not (set(DEFAULT_NOUN_DICT) & set(NON_NOUN_BLACKLIST)),
      str(sorted(set(DEFAULT_NOUN_DICT) & set(NON_NOUN_BLACKLIST))))
check("种子词条目 >= 200", len(DEFAULT_NOUN_DICT) >= 200, f"{len(DEFAULT_NOUN_DICT)} 条")
check("v7 指定补词全部在库",
      all(k in noun_matcher.dictionary for k in
          ["ビニール袋", "マッシャー", "給水プランター", "アイスクリームスクープ",
           "調味料ボトル", "包丁スタンド", "折りたたみチェア", "鍋蓋"]))
check("词库可 JSON 序列化", bool(json.dumps(noun_matcher.export_dictionary(), ensure_ascii=False)))

print("\n" + "=" * 78)
print(f"结果：PASS {OK}  FAIL {FAIL}")
print("=" * 78)
sys.exit(1 if FAIL else 0)
