# Amazon SP Bulk Ads Generator

> 亚马逊 Sponsored Products 广告批量表生成工作台 —— 从**赛狐 ERP 导出表**到**亚马逊 Bulk Operations 官方模板**的一键自适应转换。

<p>
  <img alt="python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white">
  <img alt="fastapi" src="https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi&logoColor=white">
  <img alt="vue" src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs&logoColor=white">
  <img alt="offline" src="https://img.shields.io/badge/%E8%AF%8D%E5%BA%93-%E7%BA%AF%E7%A6%BB%E7%BA%BF-blue">
  <img alt="tests" src="https://img.shields.io/badge/%E9%AA%8C%E6%94%B6%E6%B5%8B%E8%AF%95-254%20assertions-success">
</p>

---

## 1. 项目介绍

本项目专为**亚马逊跨境运营团队**设计，解决一个高频且极易出错的重复劳动：

> 运营在赛狐（Sellfox）ERP 里导出一份多店铺商品表 → 手工在亚马逊广告后台为每个父体建活动、建广告组、建 4 组自动投放、逐个填竞价 → 动辄几十上百个活动，人工做一次要几小时，且**任何一个字段写错，整个 Bulk 文件会被亚马逊整体拒绝**（19853 行全废）。

本工具把这条链路压缩成 **3 步、30 秒**：

```
赛狐导出 Excel  ──▶  网页端勾选 + 调预算  ──▶  下载亚马逊 Bulk Excel  ──▶  后台上传
```

**核心特性**

| 能力 | 说明 |
|---|---|
| **多店铺隔离** | 单文件支持 **16+ 个店铺**混合导出；按「店铺」列分层聚合，各店铺**独立**计算价格中位数，互不污染 |
| **自适应表头嗅探** | 不要求固定列序 / 固定表头行。启发式扫描前若干行找真实表头，再按「别名库 + 有效值密度」择优定位每一列 |
| **客单价加权预算池** | 两种策略：`等权预算`（每个活动相同）/ `客单价加权总预算池`（`Budget_i = max(100, Pool × P_i / ΣP_k)`），按价格中位数比率自动分配 |
| **纯离线名词词库** | 日文标题 → 中文品名的匹配**完全离线**（零网络、零延迟、零封禁风险）。377 条纯净名词 + 位置优先 / 长词优先双规则 + 一键热更新 |
| **官方模板零漂移** | 输出表头与亚马逊官方模板 `商品推广活动` 工作表 **31 列逐字对齐**；上传前本地自检拦截，杜绝「生成了才发现被拒」 |

---

## 2. 系统架构与核心功能

### 2.1 技术栈

| 层 | 技术 |
|---|---|
| **后端** | FastAPI + Uvicorn（ASGI）、Pandas、NumPy、OpenPyXL |
| **前端** | 单页应用（SPA）：Vue 3 + TailwindCSS（CDN 引入，**无需 npm / 构建**） |
| **数据** | 本地 JSON 词库 `data/ja_zh_dict.json`（原子写入 `os.replace` + mtime 热重载） |
| **网络** | **运行时零外部依赖**：不调用任何翻译 / 广告 / 第三方 API |

### 2.2 目录结构

```
amazon-bulk-ads-generator/
├── main.py                     # FastAPI 入口：路由 + 启动
├── requirements.txt            # Python 依赖
├── core/
│   ├── parser.py               # 赛狐导出表自适应解析（表头嗅探 / 别名 / 多店铺聚合）
│   ├── budget_engine.py        # 客单价中位数加权预算池（纯函数）
│   ├── bulk_generator.py       # 亚马逊 Bulk Excel 生成 + 上传前自检
│   └── translator.py           # 离线纯净名词词库匹配引擎
├── static/
│   └── index.html              # 单文件 Vue 3 SPA（UI 全部在这里）
├── data/
│   └── ja_zh_dict.json         # 中日名词对照词库（可热更新）
├── samples/
│   ├── AdvertisingBulksheetTemplate-seller.xlsx   # 亚马逊官方空白模板（回归基准）
│   ├── patch_v7_nouns.json                        # 词库增量导入示例
│   └── example_import_nouns.json                  # 词库导入格式示例
├── tools/
│   └── rebuild_from_report.py  # 排障工具：从亚马逊回传报错报告反向重出表格
└── tests/                      # 6 个验收脚本，254 条断言
```

### 2.3 核心机制

#### ① 自适应表头嗅探（`core/parser.py`）

1. **找表头行**：扫描前 N 行，命中已知别名最多的那一行即判定为真实表头（兼容「表头在第 4 行」这类导出）。
2. **模糊列定位**：每个字段维护一份别名库（如价格列支持 `售价(总价)` / `售价` / `价格` / `单价` / `list price` …），先圈定候选列。
3. **按值密度择优**：候选列中「非空有效值占比」最高者胜出 —— 这是修复「价格全兜底 2000、比率全 1x」的关键。
4. **容错保底**：只要识别到 SKU 或 ASIN 就视为有效商品；无父体标识的单品 Listing 自动兜底为独立投放项。

#### ② 自然排序（`natural_sort_key`）

用 `re.split(r"(\d+)", s)` 切分，使 `SKU2` 排在 `SKU10` 之前（而非字典序的 `SKU10 < SKU2`）。

#### ③ 长词优先 + 位置优先（`core/translator.py`）

匹配引擎基于**首字符倒排索引**，单条标题复杂度 `O(标题长度 × 该首字符候选数)`：

- **规则一 · 位置最靠前优先**：优先命中标题中最早出现的实体名词 → 精准提取「首个核心产品名」。
- **规则二 · 同位置最长词优先**：同位置命中多词取最长 → 解决 `包丁立て`(菜刀架) 被 `包丁`(菜刀) 吞掉「架」的问题。
- **前置清洗流水线** `clean_title_noise`：循环剥离「品牌前缀 → 纯数量规格词（`2個` / `3枚` / `2個セット`）→ 开头括号」。
- **修饰词后缀跳过**：`式 / 用 / 型 / 製 / 付 / 向 / 対`（如 `スプレー式冷却加湿器` → 加湿器）。

> 实测：10988 行真实导出表 / 1326 个父体，全量匹配 **0.018 秒**，词库命中率 **95.9%**。

#### ④ 加权总预算算法（`core/budget_engine.py`）

```
P̄  = 该店铺全部有效商品价格的中位数          （按店铺隔离计算）
P_i = 父体 i 的代表客单价（子体价格中位数）
Budget_i = max(100, round(Pool × P_i / ΣP_k))   # 加权模式
Budget_i = Pool / N                             # 等权模式
```

#### ⑤ 输出结构与上传前自检（`core/bulk_generator.py`）

每个父体生成 **1 个自动投放活动**，行结构：

```
Campaign  (投放类型=Auto, 状态=enabled, 每日预算)
  └─ Bidding Adjustment × 2        # 广告位加成（百分比为 0 时不生成）
  └─ Ad Group                      # 广告组默认竞价
      ├─ Product Ad × N            # 每个子体一行（SKU）
      └─ Product Targeting × 4     # 自动投放四组
                                   # close-match / loose-match / substitutes / complements
```

生成后立即执行 `_validate_output()` **本地自检**，任一不合规直接抛错、不产出文件：

- 官方 31 列是否齐备、有无旧表头残留
- `Product Targeting` 行的投放表达式是否非空且为受控值
- `关键词文本` / `匹配类型` 是否误填到投放行
- `广告位` / `百分比` / `投放类型` 是否为受控枚举

---

## 3. 快速启动指南（Quick Start）

### 3.1 系统要求

- **Python 3.10+**（开发验证环境：3.13）
- 操作系统：Windows / macOS / Linux 均可
- 浏览器：Chrome / Edge / Safari 任意现代浏览器

### 3.2 安装与启动

```bash
# 1) 克隆
git clone <你的仓库地址>
cd amazon-bulk-ads-generator

# 2) 创建虚拟环境
python -m venv venv
# Windows:
venv\Scripts\activate
# macOS / Linux:
source venv/bin/activate

# 3) 安装依赖
pip install -r requirements.txt

# 4) 启动（会自动打开浏览器）
python main.py
```

启动后访问：**http://127.0.0.1:8000**

> 终端会打印当前词库条数，例如：`本地词库：377 条 (…/data/ja_zh_dict.json)`

### 3.3 使用流程

1. 把赛狐导出的 `.xlsx` **拖入页面**（或点击上传区选择文件）。
2. 左侧切换 **店铺 Tab**，核对「清洗后品名 / 客单均价 / 分配预算」。
3. 在「投放与预算策略」里设置：竞价策略、预算分配模式、默认竞价、广告位溢价、自动 4 组竞价。
4. 勾选要投放的父体（支持全选 / 排序），点击 **生成亚马逊 Bulk 表格** 下载。
5. 到亚马逊广告后台 → **批量操作 → 上传**。建议先传小批量验证，再传全量。

---

## 4. 赛狐 ERP 导出指标要求

导出时**必须勾选以下 10 个指标**，缺一不可（尤其是「店铺」和「价格」）：

| # | 指标 | 用途 | 是否必需 |
|---|---|---|---|
| 1 | **类型** | 区分父体 / 子体（判定父子关系） | ✅ 必需 |
| 2 | **父ASIN** | 父体聚合键之一 | ✅ 必需 |
| 3 | **ASIN** | 子体标识、Bulk 表 ASIN 列 | ✅ 必需 |
| 4 | **MSKU** | 子体标识、Bulk 表 SKU 列 | ✅ 必需 |
| 5 | **标题** | 日文原标题 → 中文品名匹配的输入 | ✅ 必需 |
| 6 | **品名** | 备用展示名（匹配失败时兜底参考） | ✅ 必需 |
| 7 | **SKU** | 父 SKU 识别（`parent_sku`） | ✅ 必需 |
| 8 | **店铺** | **多店铺隔离分层键** | ✅ 必需 |
| 9 | **价格** | 客单价中位数 / 加权预算池 | ✅ 必需 |
| 10 | **图片** | 页面预览缩略图 | 建议勾选 |

> 列的顺序、表头所在行**都无所谓** —— 解析器会自动嗅探。
> 但如果缺少「店铺」列，多店铺会全部归到一个分组；缺少「价格」列会回落到默认基准价 2000。

---

## 5. 本地词库维护说明

### 5.1 词库是什么

`data/ja_zh_dict.json` 是一个**中日名词对照表**（`{"日文名词": "中文名词"}`），例如：

```json
{
  "包丁立て": "菜刀架",
  "折りたたみテーブル": "折叠桌",
  "ステンレスボトル": "不锈钢水瓶"
}
```

它决定了「清洗后品名」列显示什么 —— 也就是你的广告活动名称里的中文品名。

**词库只收录「纯物理实体名词」**：不含修饰词（`折りたたみ` / `大型`）、品牌词、功能词（`保冷` / `収納`）与场景类目词（`キャンプ` / `ゴルフ`）。非纯名词会被 `NON_NOUN_BLACKLIST` 拒绝。

### 5.2 三种维护方式

#### 方式 A：网页端单条编辑（最快）

点击顶部「本地纯净名词词库」右侧的 **管理** → 在弹出的面板里新增 / 覆盖 / 删除词条（支持搜索、从磁盘重载）。改动**立即持久化**，无需重启。

#### 方式 B：批量导入 JSON

1. 准备好 `{"日文名词": "中文名词"}` 格式的 JSON（参考 `samples/patch_v7_nouns.json`）。
2. 点击 **导入对照表 JSON** → 选择文件。
3. 提示 `导入 N 条` 即成功；若提示「拒绝 M 条」，说明这些键不是纯名词，可在返回的 `purged_keys` 里查看。

#### 方式 C：直接编辑文件

用任意编辑器打开 `data/ja_zh_dict.json` 修改并保存 —— 引擎带 **mtime 热重载**（1 秒节流），保存后自动生效，无需重启服务。

### 5.3 【🔄 刷新品名匹配】

**导入新词库后不用重新上传表格！**

点击 **🔄 刷新品名匹配**，工具会用**最新词库**对当前页面所有商品的**原始日文标题**重跑一遍匹配，就地回填中文品名。

- 已经**手动改过名**的商品不会被覆盖（输入框会显示琥珀色边框 + 「手动」角标）。
- 完成后会提示：`品名已刷新：更新 N 个 · 保留手动改名 N 个 · 无变化 N 个 · 未收录 N 个（沿用兜底名）`。

---

## 6. 常见问题与排错（FAQ）

<details>
<summary><b>Q1. 上传后亚马逊报「无效实体 / Invalid entity」？</b></summary>

说明「实体层级」列的取值不被识别。本项目已按 Bulksheet 2.0 规范固定为：
`Campaign` / `Ad Group` / `Product Ad` / `Keyword` / `Negative Keyword` / `Product Targeting` / `Negative Product Targeting` / `Bidding Adjustment` 等。

如果出现该错误，请先确认你上传的是**本工具生成的文件**（而不是历史遗留的旧版本文件）。生成器内置 `_validate_output()`，非法值在本地就会被拦下。
</details>

<details>
<summary><b>Q2. 报「以下列缺少值：拓展商品投放编号」？</b></summary>

这是**自动投放表达式列**。官方英文表头叫 `Product Targeting Expression`，亚马逊的中文机翻叫「拓展商品投放编号」（字面像 ID，实际装的是 `close-match` / `loose-match` / `substitutes` / `complements`）。

本工具输出的是**官方 31 列**，该列位于第 27 列，自动投放四行会各自填好受控值。若你手工编辑过表头，请勿改动列名 —— 亚马逊按**表头文字精确匹配**，改一个字的列会被整列忽略，然后报「缺少值」。
</details>

<details>
<summary><b>Q3. 表头识别不出来 / 品名全是「商品」？</b></summary>

分两种情况：

- **解析失败（报 400）**：多半是导出表缺列。请回到赛狐按第 4 节的 10 个指标**重新导出**（特别是「店铺」「价格」「MSKU」「ASIN」）。前端会直接弹出精确原因。
- **能解析但品名不准**：说明日文标题里的名词没被词库收录。解决路径：`管理` 里补词 → 点 `🔄 刷新品名匹配`。无需重新上传表格。
</details>

<details>
<summary><b>Q4. <code>pip install</code> 依赖冲突 / 装不上？</b></summary>

```bash
# 强烈建议用独立虚拟环境，避免污染全局 Python
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # macOS / Linux
python -m pip install --upgrade pip
pip install -r requirements.txt
```

国内网络慢可加镜像：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

若 `pandas` / `numpy` 编译失败，请确认 Python 版本 ≥ 3.10（推荐 3.11 / 3.12 / 3.13）。
</details>

<details>
<summary><b>Q5. 端口 8000 被占用怎么办？</b></summary>

打开 `main.py`，修改文件末尾：

```python
host, port = "127.0.0.1", 8000     # ← 把 8000 改成其它端口，如 8010
```
</details>

<details>
<summary><b>Q6. 生成的表格行数比预期少？</b></summary>

检查「搜索顶部 / 商品页面溢价 (%)」是否填了 `0`。

溢价为 0 属于**空操作**，生成器会跳过对应的 `Bidding Adjustment` 加成行（少一行就少一个被亚马逊校验的字段组合）。若需要加价，把溢价填成非 0 的数字，加成行会重新生成。
</details>

<details>
<summary><b>Q7. 上传后报告里有大量「由于其他行的错误，该行未得到处理」？</b></summary>

这是**级联警告，不是真实错误**。亚马逊只要有少数行报错，其余行就会显示「未得到处理」。

**看报告时只看「错误类型 = 用户输入无效 / 无效实体」的行**，忽略「未处理 / 警告」。修掉真实错误后，级联警告会自动消失。
</details>

<details>
<summary><b>Q8. 想批量试传，但全量文件太大？</b></summary>

用 `tools/rebuild_from_report.py` 可以从亚马逊回传的报错报告反向重建商品集合并重出表格：

```bash
python tools/rebuild_from_report.py <report.xlsx> <输出目录>
# 产出：全量.xlsx + 测试_仅1个活动.xlsx（子体最少的那个活动，便于先小文件验证）
```
</details>

---

## 附录 A · API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/` | 单页应用入口 |
| `GET` | `/api/health` | 健康检查（含版本号） |
| `POST` | `/api/analyze` | 上传赛狐 Excel → 返回按店铺分层的商品聚合与预算建议 |
| `POST` | `/api/generate` | 提交勾选商品 + 配置 → 下载亚马逊 Bulk Excel |
| `POST` | `/api/dictionary/import` | 导入中日对照 JSON（持久化 + 清缓存） |
| `GET` | `/api/dictionary` | 词库统计 + 全部条目 |
| `GET` | `/api/dictionary/stats` | 词库条数总览 |
| `GET` | `/api/dictionary/export` | 导出当前词库 JSON |
| `POST` | `/api/dictionary` | 新增 / 修改一条词条 |
| `DELETE` | `/api/dictionary` | 删除一条词条 |
| `POST` | `/api/dictionary/reload` | 从磁盘重载词库 |
| `POST` | `/api/dictionary/rematch` | 用最新词库重匹配一批标题（不落盘） |

交互式文档：启动后访问 **http://127.0.0.1:8000/docs**

## 附录 B · 输出表头（官方 31 列 + 1 辅助列）

```
1  产品              2  实体层级          3  操作              4  广告活动编号
5  广告组编号        6  广告组合编号      7  广告编号          8  关键词编号
9  商品投放 ID       10 广告活动名称      11 广告组名称        12 开始日期
13 结束日期          14 投放类型          15 状态              16 每日预算
17 SKU               18 广告组默认竞价    19 竞价              20 关键词文本
21 母语关键词        22 母语区域          23 匹配类型          24 竞价方案
25 广告位            26 百分比            27 拓展商品投放编号  28 受众编号
29 购物者群体占比    30 购物者群体类型    31 站点              32 ASIN（辅助列，亚马逊忽略）
```

工作表名：`Sponsored Products Campaigns`

## 附录 C · 验收测试

```bash
# 一键跑全部（test_4 需要后端已在 127.0.0.1:8000 运行）
bash tests/run_all.sh
```

| 脚本 | 覆盖内容 |
|---|---|
| `test_1_noun_matching.py` | 名词匹配五项准则、修饰词后缀、性能、倒排索引一致性 |
| `test_2_end_to_end.py` | 解析 → 预算 → 生成；表头与官方模板逐字比对；字段合规；自检负向用例 |
| `test_3_dictionary_io.py` | 词库增删改查、原子写入、热重载 |
| `test_4_http_smoke.py` | 全部 HTTP 接口冒烟（需后端运行） |
| `test_5_v7_quantity_and_rematch.py` | 数量词清洗、`match_noun_only`、rematch 契约、真实数据端到端 |
| `test_6_frontend_refresh.js` | 前端「刷新品名匹配」交互（jsdom + 真实 Vue 运行时） |

合计 **254 条断言**。

---

## 许可与致谢

内部运营工具。词库（`data/ja_zh_dict.json`）为业务资产，请勿外传。
