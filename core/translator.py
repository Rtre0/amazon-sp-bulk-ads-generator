"""本地纯净名词词库匹配引擎（Offline Pure-Noun Dictionary Engine）。

设计目标（v6）
--------------------------------------------------------------------
1. **彻底离线**：不发起任何网络请求 —— 零延迟、零封禁风险、零 API 依赖。
2. **纯净名词表**：词库 Key 仅收录「纯物理实体名词」，不含修饰词（折りたたみ /
   大型 / 連結式）、品牌词、功能词（保冷 / 収納 / 充電）与场景类目词
   （キャンプ / ゴルフ）。词条规范见 ``DEFAULT_NOUN_DICT`` 与 ``NON_NOUN_BLACKLIST``。
3. **双优先扫描**（``match_first_noun``）：
   - **规则一 · 位置最靠前优先**（Earliest-Position First）：优先命中在标题中
     最早出现的实体名词，精准提取「首个核心产品名」。
   - **规则二 · 同位置最长词优先**（Longest-Match First）：同一位置命中多个词时
     取最长者，彻底解决 ``包丁立て``(菜刀架) 被 ``包丁``(菜刀) 吞掉「架」的问题。

   例：``店舗用陳列架子大型連結式`` -> ``陳列架子`` -> ``陈列架``（修饰词自动过滤）
       ``ステンレス包丁立て``     -> ``包丁立て`` -> ``菜刀架``（长词优先）
       ``折りたたみテーブル``     -> ``折りたたみテーブル`` -> ``折叠桌``

4. **一键导入**：前端「导入对照表 JSON」按钮 -> ``POST /api/dictionary/import``，
   合并写入并持久化到 ``data/ja_zh_dict.json``，无需重启。
5. **前置清洗流水线**（``clean_title_noise``）：循环剥离
   「品牌前缀 → 纯数量/规格词（``2個`` / ``3枚`` / ``2個セット`` / ``2枚組``）→ 开头括号」，
   再进入名词扫描。``FryWare 2個 マッシャー ...`` -> ``マッシャー`` -> ``压泥器``。
6. **动态重匹配**：前端「刷新品名匹配」按钮 -> ``POST /api/dictionary/rematch``，
   导入新词库后无需重新上传表格，当前页面所有商品的日文原标题立即重跑匹配。

性能
--------------------------------------------------------------------
匹配按「首字符」建立倒排索引，单条标题复杂度 O(标题长度 × 该首字符下的候选数)，
纯 CPU 内存扫描。1300+ Listing 实测 < 1 秒（10988 行真实导出表全量匹配亦在毫秒级）。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 词库文件位置
# ---------------------------------------------------------------------------
DICT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
DICT_FILE = os.path.join(DICT_DIR, "ja_zh_dict.json")

# ---------------------------------------------------------------------------
# 内置基础常见电商「纯物理名词」表
#   规范：Key 必须是完整的日文实体名词，不可拆成「修饰词 + 名词」两段。
# ---------------------------------------------------------------------------
DEFAULT_NOUN_DICT: Dict[str, str] = {
    # —— 复合与长名词优先定义 ——
    "包丁立て": "菜刀架",
    "カッティングボード": "砧板",
    "クーラーカート": "保温推车",
    "システム手帳": "手账本",
    "陳列棚": "陈列架",
    "陳列架子": "陈列架",
    "止水板": "挡水板",
    "ベッドサイドテーブル": "床头柜",
    "ダイニングテーブル": "餐桌",
    "オフィスチェア": "办公椅",
    "オットマン": "脚凳",
    "スツール": "凳子",
    "テーブル": "桌子",
    "チェア": "椅子",
    "デスク": "书桌",
    "まな板": "砧板",
    "ワイン": "醒酒器",
    "香炉": "香炉",
    "竹刀": "竹刀",
    "食器棚": "餐具柜",
    "ステッキ": "手杖",
    "杖椅子": "拐杖椅",
    "加湿器": "加湿器",
    "生豆": "生豆烘焙器",
    "手帳": "记事本",
    "包丁": "菜刀",
    # —— v6 追加：长名词优先示例（含修饰词的完整产品名，整体作为 Key）——
    "折りたたみテーブル": "折叠桌",
    "折り畳みテーブル": "折叠桌",
    "折りたたみ式テーブル": "折叠桌",
    "折り畳み式テーブル": "折叠桌",
    "ローテーブル": "矮桌",
    "こたつテーブル": "暖桌",
    "サイドテーブル": "边几",
    "ロッキングチェア": "摇椅",
    "ゲーミングチェア": "电竞椅",
    "ラウンジチェア": "休闲椅",
    "クーラーボックス": "保温箱",
    "メイクボックス": "化妆箱",
    "保存容器": "保鲜盒",
    "水筒": "保温水杯",
    "タンブラー": "随行杯",
    "ボウル": "碗",
    "フライパン": "平底锅",
    "鍋": "锅",
    "ラック": "置物架",
    "シェルフ": "架子",
    "ワゴン": "推车",
    "台車": "手推车",
    "カート": "推车",
    "ハンガー": "衣架",
    "ミラー": "镜子",
    "カーテン": "窗帘",
    "じゅうたん": "地毯",
    "マット": "垫子",
    "クッション": "靠垫",
    "ソファ": "沙发",
    "ベンチ": "长凳",
    "ライト": "灯",
    "ランタン": "营地灯",
    "ヒーター": "取暖器",
    "扇風機": "风扇",
    "スピーカー": "音箱",
    "センサー": "传感器",
    "時計": "时钟",
    "傘": "伞",
    "布団": "被褥",
    "寝袋": "睡袋",
    "テント": "帐篷",
    "タープ": "天幕",
    "椅子": "椅子",
    "事務椅子": "办公椅",
    "車椅子": "轮椅",
    "脚立": "人字梯",
    "はしご": "梯子",
    "踏み台": "踏步梯",
    "のこぎり": "锯子",
    "ドリル": "电钻",
    "ドライバー": "螺丝刀",
    "レンチ": "扳手",
    "ペンチ": "钳子",
    "ネジ": "螺丝",
    "チェーン": "链条",
    "フォークリフト": "叉车",
    "ストレッチャー": "担架",
    "バッグ": "包",
    "リュック": "背包",
    "ベル": "铃",
    "ネット": "网",
    "フェンス": "护栏",
    "ボックス": "收纳箱",
    "ボトル": "瓶",
    "カップ": "杯",
    "保冷カップ": "保温杯",
    "スプレー": "喷雾器",
    "じょうご": "漏斗",
    "雨どい": "雨链",
    "根抜き": "拔根器",
    "薪割り": "劈柴器",
    "焚き火": "焚火台",
    "焼き網": "烤网",
    "流量計": "流量计",
    "パネル": "面板",
    "ハードル": "跨栏架",
    "熊鈴": "防熊铃",
    "パッティング": "推杆",
    "シンク": "水槽",
    "トレー": "托盘",
    "ハンドル": "方向盘",
    "ルーフ": "车顶",
    "シート": "座椅",
    # —— v6 追加：通用纯名词（减少未命中时回落到日文原文）——
    "測定器": "测量仪",
    "ケース": "收纳盒",
    "カバー": "保护套",
    "スタンド": "支架",
    "ホルダー": "固定架",
    "ブラシ": "刷子",
    # —— v7 追加：用户指定补词（塑料袋 / 压泥器 等）——
    "ビニール袋": "塑料袋",
    "ポリ袋": "塑料袋",
    "レジ袋": "塑料袋",
    "保存袋": "保鲜袋",
    "マッシャー": "压泥器",
    "アイスクリームスクープ": "冰淇淋勺",
    "プランター": "花盆",
    "給水プランター": "吸水花盆",
    "調味料ボトル": "调料瓶",
    "オイルボトル": "油瓶",
    "包丁スタンド": "菜刀架",
    "折りたたみチェア": "折叠椅",
    "鍋蓋": "锅盖",
    # —— v7 追加：真实导出表高频名词（按 10988 行样本词频补充）——
    "収納ボックス": "收纳箱",
    "紙製収納ボックス": "纸质收纳盒",
    "ダイニングテーブルセット": "餐桌椅套装",
    "スチールラック": "置物架",
    "メタルラック": "置物架",
    "バーベキューネット": "烤网",
    "鉄板網": "铁板网",
    "裏ごし器": "筛网",
    "粉ふるい": "面粉筛",
    "綿ロープ": "棉绳",
    "鋳鉄ホーロー鍋": "铸铁搪瓷锅",
    "サッカーグローブ": "足球手套",
    "ソファー": "沙发",
    "キーパーグローブ": "守门员手套",
    "ティーポット": "茶壶",
    "寿司桶": "寿司桶",
    "マグカップ": "马克杯",
    "香水瓶": "香水瓶",
    "カトラリー": "餐具",
    "ピッチャー": "水壶",
    "グラス": "玻璃杯",
    # —— v7 追加：真实导出表长尾名词（按 1326 个父体未命中聚类补充）——
    "太陽熱温水器": "太阳能热水器",
    "温水器": "热水器",
    "貯水タンク": "储水罐",
    "貯水槽": "储水罐",
    "水嚢": "水袋",
    "グレーチング": "排水沟盖板",
    "ローラースケート": "轮滑鞋",
    "インラインスケート": "轮滑鞋",
    "寿司メーカー": "寿司模具",
    "寿司船": "寿司船",
    "石臼": "石磨",
    "すり鉢": "研钵",
    "にんにくつぶし器": "压蒜器",
    "抹茶泡立て器": "茶筅",
    "泡立て器": "打蛋器",
    "茶せん": "茶筅",
    "茶筅": "茶筅",
    "茶盤": "茶盘",
    "急須": "茶壶",
    "小皿": "小碟",
    "風鈴": "风铃",
    "置物": "摆件",
    "招き猫": "招财猫",
    "マネキン": "模特架",
    "トルソー": "半身模特",
    "バスケット": "篮子",
    "かごバスケット": "篮子",
    "収納かご": "收纳篮",
    "竹かご": "竹篮",
    "背負い籠": "背篓",
    "グリル": "烤架",
    "バーベキューコンロ": "烧烤炉",
    "七輪": "炭炉",
    "練炭コンロ": "炭炉",
    "火鉢": "火盆",
    "バケツ": "水桶",
    "洗濯板": "搓衣板",
    "スコップ": "铲子",
    "ケーキ型": "蛋糕模具",
    "パイ皿": "派盘",
    "ステーキプレート": "牛排盘",
    "スライドレール": "滑轨",
    "ペーパーカッター": "裁纸刀",
    "湯切り": "沥水器",
    # —— v7 追加：剩余长尾（第二轮聚类，均为真实在售物理实体）——
    "オイルポット": "油壶",
    "ウォータージャグ": "储水桶",
    "ポリタンク": "塑料桶",
    "水タンク": "水箱",
    "真空断熱ポット": "真空保温壶",
    "サーモポット": "保温壶",
    "フードストッカー": "保鲜盒",
    "密閉容器": "密封罐",
    "足湯桶": "泡脚桶",
    "フットバス": "足浴盆",
    "虫取り網": "捕虫网",
    "ホワイトボード": "白板",
    "ライティングボード": "写字板",
    "じょうろ": "洒水壶",
    "菜箸": "长筷子",
    "金型": "模具",
    "調理器具": "厨具",
    "茶器セット": "茶具套装",
    "折りたたみ式ベッド": "折叠床",
    "サマーベッド": "夏日躺床",
    "手すり": "扶手",
    "使い捨て竹串": "一次性竹签",
    "竹串": "竹签",
    "寿司盛台": "寿司盛台",
    "刺身盛台": "刺身盛台",
}

# ---------------------------------------------------------------------------
# 非纯名词黑名单：修饰词 / 动词 / 功能词 / 场景类目词
#   这些词一旦进入词库，会因「位置最靠前优先」规则抢占真正的产品名词
#   （例如 `キャンプ用テーブル` 会被判成「露营用品」而不是「桌子」），
#   因此载入时统一清洗掉。若确有需要，可自行从此列表移除。
# ---------------------------------------------------------------------------
NON_NOUN_BLACKLIST = frozenset(
    {
        # —— 修饰词 / 形态 / 数量 ——
        "折りたたみ", "折り畳み", "大型", "小型", "軽量", "大容量", "業務用",
        "連結式", "多機能", "新型", "日本製", "高さ調節", "セット", "モデル",
        "タイプ", "付き", "対応", "可能", "専用", "店舗用", "家庭用", "特大",
        "ミニ", "おしゃれ", "かわいい", "コンパクト", "スリム",
        # —— 动作 / 功能词 ——
        "充電", "冷却", "収納", "結束", "脱出", "製氷", "保冷", "介護",
        "救急", "日除け", "車載", "釣り", "筋トレ",
        # —— 场景 / 类目词（非物理实体）——
        "キャンプ", "アウトドア", "ゴルフ", "テニス", "ボクシング", "ヨガ",
        "ピックルボール", "ペット", "インスリン",
        # 注：`バスケット` 已从黑名单移入词库（本店真实在售「篮子 / かご」类商品）
    }
)

# ---------------------------------------------------------------------------
# 文本处理正则
# ---------------------------------------------------------------------------
# 开头品牌标识：FryWare_ / DOHERA / 2WAY  等（后接下划线或空格）
_BRAND_PREFIX_RE = re.compile(r"^[a-zA-Z0-9_\-\s]+[_\s]+")

# 开头「纯数量 / 规格」干扰词：2個 / 3枚 / 4点 / 2個セット / 2枚組 / 2個入り / ２個（全角数字）
#   仅做前缀剥离；量词白名单可避免误伤 24cm / 304ステンレス / 3段ラック 等真实品名
_QTY_UNITS = "個|枚|本|点|點|組|パック|セット|ヶ|ケ|人前|袋|箱|缶|束|玉|巻|双|対|台|膳"
_QTY_TAIL = "セット|組|入り|入|パック|箱入り"
_QTY_PREFIX_RE = re.compile(
    rf"^[\s　]*[0-9０-９]+[\s　]*(?:{_QTY_UNITS})(?:[\s　]*(?:{_QTY_TAIL}))?[\s　]*"
)

# 开头括号标记： [xxx] / (yyy) / 【zzz】 / （yyy） / 「zzz」
_BRACKET_PREFIX_RE = re.compile(
    r"^[\s　]*(?:\[[^\]]*\]|\([^)]*\)|【[^】]*】|（[^）]*）|「[^」]*」)[\s　]*"
)

# 兜底分词分隔符
_TOKEN_SPLIT_RE = re.compile(r"[\s_、,，|/\-]+")
# 非法字符（用于广告活动命名安全）
_ILLEGAL_RE = re.compile(r"[+\-*/\\^&%#@!~`=()\[\]{}|:;\"'<>,.?！？，。、\s]+")
_WS_RE = re.compile(r"\s+")

# 「修饰后缀」标记：若命中的名词紧跟着这些字，说明它在标题里是修饰成分而非产品本体，
# 应跳过并继续向后寻找真正的核心名词。
#   スプレー**式**冷却加湿器  -> 跳过「スプレー」，命中「加湿器」
#   テーブル**用**ラック      -> 跳过「テーブル」，命中「ラック」
_MODIFIER_SUFFIXES = frozenset("式用型製付向対")

_CACHE_LIMIT = 20000


# ---------------------------------------------------------------------------
# 纯名词匹配引擎
# ---------------------------------------------------------------------------
class NounDictionaryMatcher:
    """本地纯净名词词库 + 双优先（位置 / 长度）扫描匹配器。"""

    def __init__(self, dict_path: str = DICT_FILE):
        self._lock = threading.RLock()
        self.dict_path = dict_path
        self.dictionary: Dict[str, str] = {}
        # 首字符 -> 该首字符下所有 Key，按长度降序（保证同位置最长优先）
        self._index: Dict[str, List[str]] = {}
        # 缓存值 = (词库命中结果 or None, 最终展示结果)
        self._cache: Dict[str, Tuple[Optional[str], str]] = {}
        self._mtime = 0.0
        self._last_check = 0.0
        self.purged_keys: List[str] = []
        self._load_dictionary()

    # ---------------- 持久化 ----------------
    def _load_dictionary(self) -> None:
        """载入词库：种子词打底 + 磁盘词覆盖 + 修饰词清洗。"""
        os.makedirs(os.path.dirname(self.dict_path), exist_ok=True)

        disk: Dict[str, str] = {}
        if os.path.exists(self.dict_path):
            try:
                with open(self.dict_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    disk = {
                        str(k).strip(): str(v).strip()
                        for k, v in data.items()
                        if str(k).strip() and str(v).strip()
                    }
            except Exception:  # noqa: BLE001 - 文件损坏时回落到种子词
                disk = {}

        # 一次性清洗：剔除历史自动沉淀进来的修饰词 / 动词 / 场景词
        self.purged_keys = sorted(k for k in disk if k in NON_NOUN_BLACKLIST)
        for k in self.purged_keys:
            disk.pop(k, None)

        # 种子词打底，磁盘词（用户导入 / 手工编辑）优先覆盖
        merged = dict(DEFAULT_NOUN_DICT)
        merged.update(disk)
        self.dictionary = merged
        self._rebuild_index()

        # 首次运行、发生过清洗、或种子词有新增 -> 回写磁盘
        if self.purged_keys or merged != disk:
            self._save_dictionary()
        else:
            try:
                self._mtime = os.path.getmtime(self.dict_path)
            except OSError:
                self._mtime = 0.0
        self._cache.clear()

    def _save_dictionary(self) -> None:
        """原子写入（tmp + os.replace），避免并发/中断导致 JSON 损坏。"""
        try:
            os.makedirs(os.path.dirname(self.dict_path), exist_ok=True)
            tmp = self.dict_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.dictionary, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.dict_path)
            self._mtime = os.path.getmtime(self.dict_path)
        except Exception as exc:  # noqa: BLE001
            print(f"[Warn] 词库持久化写入失败: {exc}")

    def _rebuild_index(self) -> None:
        """重建首字符倒排索引（Key 按长度降序，实现同位置最长优先）。"""
        index: Dict[str, List[str]] = {}
        for key in sorted(self.dictionary.keys(), key=len, reverse=True):
            if not key:
                continue
            index.setdefault(key[0], []).append(key)
        self._index = index

    def _maybe_reload(self) -> None:
        """按 mtime 检测手工编辑（最多每秒检查一次），无需重启。"""
        now = time.time()
        if now - self._last_check < 1.0:
            return
        self._last_check = now
        try:
            mtime = os.path.getmtime(self.dict_path)
        except OSError:
            return
        if mtime > self._mtime:
            with self._lock:
                print("[Dict] 检测到词库文件变更，已热加载")
                self._load_dictionary()

    # ---------------- 核心匹配 ----------------
    @staticmethod
    def clean_title_noise(text: Any) -> str:
        """前置清洗流水线：循环剥离「品牌前缀 → 数量规格词 → 开头括号」。

        循环执行是因为三种噪声会交替出现，例如 ``FryWare 2個 (大) マッシャー``
        需要剥三轮才能露出真正的名词。最多 8 轮，防止病态输入导致死循环。
        """
        s = str(text or "").strip()
        for _ in range(8):
            before = s
            s = _BRAND_PREFIX_RE.sub("", s).strip()
            s = _QTY_PREFIX_RE.sub("", s).strip()
            s = _BRACKET_PREFIX_RE.sub("", s).strip()
            s = s.lstrip("_ 　\t|/-")  # 清掉剥离后残留的前导分隔符
            if s == before:
                break
        return s

    def _scan(self, text: str) -> Optional[str]:
        """位置最靠前优先 + 同位置最长词优先的实体名词扫描。

        额外规则（修饰后缀跳过）：命中词若紧跟「式 / 用 / 型 / 製 / 付 / 向 / 対」，
        视为修饰成分而跳过，继续向后寻找真正的产品名词。
        """
        index = self._index
        dictionary = self.dictionary
        length = len(text)
        for i in range(length):
            candidates = index.get(text[i])
            if not candidates:
                continue
            for key in candidates:  # 已按长度降序
                if text.startswith(key, i):
                    end = i + len(key)
                    if end < length and text[end] in _MODIFIER_SUFFIXES:
                        continue  # 修饰成分，跳过
                    return dictionary[key]
        return None

    def match_first_noun(self, text: Any) -> str:
        """提取标题中「首个核心实体名词」的中文名。

        流程：前置清洗（品牌 / 数量词 / 括号）-> 全文双优先扫描 -> 兜底取首词。
        """
        return self._match_pair(text)[1]

    def match_noun_only(self, text: Any) -> Optional[str]:
        """仅返回「词库真实命中」的中文名；未命中返回 ``None``（不做首词兜底）。

        用于「刷新品名匹配」场景：区分该商品是被词库精准收录，
        还是仅靠兜底规则拼出的占位名，便于前端提示与统计。
        """
        return self._match_pair(text)[0]

    def _match_pair(self, text: Any) -> Tuple[Optional[str], str]:
        """内部统一匹配入口，返回 ``(词库命中 or None, 最终展示结果)``。"""
        if text is None:
            return None, "商品"
        raw = str(text).strip()
        if not raw:
            return None, "商品"

        # 手工编辑 JSON 后无需重启：按 mtime 热加载（内部有 1s 节流，开销极小）
        self._maybe_reload()

        cached = self._cache.get(raw)
        if cached is not None:
            return cached

        clean = self.clean_title_noise(raw) or raw

        # 去空白后扫描：删空格不改变各词的相对先后，位置语义完全保留，
        # 同时能命中「ダイニング テーブル」这类被空格拆开的长名词。
        stripped = _WS_RE.sub("", clean)
        hit = self._scan(stripped) if stripped else None
        if hit is None and stripped != clean:
            hit = self._scan(clean)  # 兜底：词库 Key 自身含空格时

        pair: Tuple[Optional[str], str] = (hit, hit if hit else self._fallback(clean))

        if len(self._cache) >= _CACHE_LIMIT:
            self._cache.clear()
        self._cache[raw] = pair
        return pair

    @staticmethod
    def _fallback(text: str) -> str:
        """词典完全未命中：取首个词片并滤除非法字符。"""
        for token in _TOKEN_SPLIT_RE.split(text):
            candidate = token.strip().strip("　[]()（）【】「」")
            if candidate:
                cleaned = _ILLEGAL_RE.sub("_", candidate)
                cleaned = re.sub(r"_+", "_", cleaned).strip("_")
                return cleaned[:10] if cleaned else "商品"
        return "商品"

    # ---------------- 词库管理 ----------------
    def update_dictionary(self, new_data: Dict[str, str]) -> Dict[str, Any]:
        """合并导入外部清洗后的词典，返回导入统计。"""
        if not isinstance(new_data, dict):
            raise ValueError(
                "导入的词典格式必须为 JSON 对象键值对: {\"日文名词\": \"中文名词\"}"
            )
        imported = 0
        purged: List[str] = []
        with self._lock:
            for k, v in new_data.items():
                key, val = str(k).strip(), str(v).strip()
                if not key or not val:
                    continue
                if key in NON_NOUN_BLACKLIST:
                    purged.append(key)
                    continue
                if self.dictionary.get(key) != val:
                    self.dictionary[key] = val
                    imported += 1
            self._rebuild_index()
            self._save_dictionary()
            self._cache.clear()
        return {"imported": imported, "purged": purged, "total": len(self.dictionary)}

    def upsert(self, ja: str, zh: str) -> None:
        with self._lock:
            self.dictionary[str(ja).strip()] = str(zh).strip()
            self._rebuild_index()
            self._save_dictionary()
            self._cache.clear()

    def delete(self, ja: str) -> bool:
        with self._lock:
            if ja in self.dictionary:
                del self.dictionary[ja]
                self._rebuild_index()
                self._save_dictionary()
                self._cache.clear()
                return True
            return False

    def reload(self) -> int:
        with self._lock:
            self._load_dictionary()
            return len(self.dictionary)

    def stats(self) -> Dict[str, Any]:
        return {
            "count": len(self.dictionary),
            "path": self.dict_path,
            "blacklist_size": len(NON_NOUN_BLACKLIST),
            "purged": self.purged_keys,
        }

    def list_entries(self) -> List[Dict[str, str]]:
        self._maybe_reload()
        return [{"ja": k, "zh": v} for k, v in sorted(self.dictionary.items())]

    def export_dictionary(self) -> Dict[str, str]:
        """导出当前词库（供前端下载备份）。"""
        self._maybe_reload()
        return dict(self.dictionary)


# ---------------------------------------------------------------------------
# 全局单例 + 便捷函数
# ---------------------------------------------------------------------------
noun_matcher = NounDictionaryMatcher()


def match_first_noun(text: Any) -> str:
    """便捷入口：提取首个核心实体名词的中文名。"""
    return noun_matcher.match_first_noun(text)


def match_first_nouns_batch(names: Iterable[str]) -> Dict[str, str]:
    """批量匹配（纯 CPU 扫描，无需并发）。"""
    return {
        str(n): noun_matcher.match_first_noun(n)
        for n in names
        if n is not None and str(n).strip()
    }
