# Amazon SP Bulk Ads Generator

> 亚马逊 Sponsored Products 广告投放工作台 —— **赛狐 ERP 数据同步 → 商品 / 搜索词分析 → 自动广告 + 手动广告批量创建**，支持 Bulk Excel 导出与接口直接创建双通道。

<p>
  <img alt="version" src="https://img.shields.io/badge/version-v2.7.0-2f81f7">
  <img alt="python" src="https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python\&logoColor=white">
  <img alt="fastapi" src="https://img.shields.io/badge/FastAPI-0.100%2B-009688?logo=fastapi\&logoColor=white">
  <img alt="vue" src="https://img.shields.io/badge/Vue-3-4FC08D?logo=vuedotjs\&logoColor=white">
  <img alt="offline" src="https://img.shields.io/badge/%E8%AF%8D%E5%BA%93-%E7%BA%AF%E7%A6%BB%E7%BA%BF-blue">
  <img alt="tests" src="https://img.shields.io/badge/%E9%AA%8C%E6%94%B6%E6%B5%8B%E8%AF%95-291%20assertions-success">
</p>

> **当前版本 `v2.7.0` · 2026-11-12**
> 本次发布将 GitHub 上停留在 **v10.0.0（`c60aff0`，2026-10-30）** 的仓库一次性补齐 **10 个功能提交**，并补上版本与维护文档。
> 首次部署 / 从旧版升级请先读 [发版说明与升级指南](docs/RELEASE_NOTES_v2.7.0.md)。

## 文档索引

| 文档 | 用途 |
| --- | --- |
| [README.md](README.md) | 项目概览、安装、使用流程、FAQ、API 一览（本文） |
| [CHANGELOG.md](CHANGELOG.md) | 逐版本变更记录（Keep a Changelog 格式） |
| [docs/RELEASE_NOTES_v2.7.0.md](docs/RELEASE_NOTES_v2.7.0.md) | 本次发版说明、与 GitHub 旧版差异、升级步骤与回滚 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块职责、数据流、关键设计约定（面向维护者） |

---

## 1. 项目介绍

本项目专为**亚马逊跨境运营团队**设计，解决一个高频且极易出错的重复劳动：

> 运营在赛狐（Sellfox）ERP 里导出一份多店铺商品表 → 手工在亚马逊广告后台为每个父体建活动、建广告组、建 4 组自动投放、逐个填竞价 → 动辄几十上百个活动，人工做一次要几小时，且**任何一个字段写错，整个 Bulk 文件会被亚马逊整体拒绝**（19853 行全废）。

v10 把这条链路压缩成 3 步；**v2.x 进一步把「导表」这一步也自动化了**：配置一次赛狐登录态，商品库与搜索词报表会自动落到本地 SQLite，之后可直接从本地库选品、看哪些父体还没开广告、批量创建投放。

```
路径 A（离线）  赛狐导出 Excel ──▶ 网页勾选 + 调预算 ──▶ 下载 Bulk Excel ──▶ 亚马逊后台上传
路径 B（在线）  赛狐 API 同步 ──▶ 本地 SQLite 商品库/搜索词 ──▶ 选品分析
                                    ├──▶ 导出 Bulk Excel
                                    └──▶ 接口直接创建（自动广告 / 手动广告）
```

### 基础能力（v1 ~ v10，完整保留）

| 能力 | 说明 |
| --- | --- |
| **多店铺隔离** | 单文件支持 16+ 店铺混合导出；按「店铺」分层聚合，各店铺独立计算价格中位数，互不污染 |
| **自适应表头嗅探** | 不要求固定列序 / 表头行；启发式扫描 + 别名库 + 有效值密度择优定位每一列 |
| **客单价加权预算池** | `等权预算` / `客单价加权总预算池`（`Budget_i = max(100, Pool × P_i / ΣP_k)`） |
| **纯离线名词词库** | 日文标题 → 中文品名匹配完全离线（零网络、零延迟、零封禁风险）；461 条纯净名词 + 位置优先 / 长词优先 + 热更新 |
| **官方模板零漂移** | 输出表头与亚马逊官方模板 31 列逐字对齐；`_validate_output()` 本地自检拦下非法值 |

### v2.x 新增能力

| 能力 | 说明 |
| --- | --- |
| **多赛狐账号池** | 多份 Cookie 按账号隔离（ContextVar），逐个账号同步，登录态互不覆盖；店铺归属记录在 `shops.account_id` |
| **本地商品库 + 增量同步** | SQLite（WAL）持久化商品 / 每日搜索词 / 投放产生 ASIN；APScheduler 定时增量同步 |
| **手动广告工作台** | 搜索词出单分析、关键词 / ASIN 双维度、匹配类型多选、竞价倍数批量应用、逐行可编辑活动名 |
| **接口直接创建** | 自动广告与手动广告均可跳过 Excel，直接调赛狐接口创建活动；Bulk 导出能力同时保留 |
| **父 SKU 广告开启状态** | 按活动 `state` 判定「已开启 / 已暂停 / 未开启」，支持「只看未开」筛选，快速找出尚未投放的商品 |
| **图片代理与数据外置** | 商品图走后端白名单代理 + 落盘缓存；`SELLFOX_HOME` 把数据库与 Cookie 物理移出仓库 |

---

## 2. 系统架构与核心功能

### 2.1 技术栈

| 层 | 技术 |
| --- | --- |
| **后端** | FastAPI + Uvicorn（ASGI）、Pandas、NumPy、OpenPyXL、APScheduler |
| **前端** | 单页应用（SPA）：Vue 3 + TailwindCSS（CDN 引入，**无需 npm / 构建**） |
| **数据** | SQLite（WAL）本地库 + 离线 JSON 词库 `data/ja_zh_dict.json`（原子写入 + mtime 热重载） |
| **离线能力** | 名词匹配 / Bulk 生成 / Excel 解析 **完全离线**，不依赖翻译或第三方 API |
| **在线能力**（可选） | 赛狐 ERP 接口（商品 / 搜索词 / 活动与广告商品 / 创建广告）、亚马逊图床图片代理；仅在配置登录态后启用 |
| **浏览器自动化** | Playwright（半自动登录，复用本机 Chrome，免 `playwright install`） |

> ⚠️ 与 v10 的差异：v10 声称「运行时零外部依赖」；v2.x 的**在线同步与接口创建**需要赛狐登录态，
> 但**离线路径（上传 Excel → 生成 Bulk）依然零网络依赖**，两条路径互不影响。

### 2.2 目录结构

```
amazon-bulk-ads-generator/
├── main.py                     # FastAPI 入口：全部路由 + 启动
├── start.bat                   # Windows 一键启动（自动建 venv、装依赖、开浏览器）
├── 启动（数据外置）.bat          # 带 SELLFOX_HOME 的一键启动（推荐）
├── 启动Chrome调试模式.bat        # 半自动登录时抓 Cookie 用
├── requirements.txt            # Python 依赖
├── README.md / CHANGELOG.md    # 项目说明 / 逐版本变更
├── docs/
│   ├── RELEASE_NOTES_v2.7.0.md # 本次发版说明 + 升级与回滚
│   └── ARCHITECTURE.md         # 模块职责与关键设计约定
├── core/
│   ├── parser.py               # 赛狐导出表自适应解析（表头嗅探 / 别名 / 多店铺聚合）
│   ├── budget_engine.py        # 客单价中位数加权预算池（纯函数）
│   ├── bulk_generator.py       # 亚马逊 Bulk Excel 生成 + 上传前自检
│   ├── translator.py           # 离线纯净名词词库匹配引擎
│   ├── paths.py                # ★ 数据/配置目录统一出口（SELLFOX_HOME）
│   ├── database.py             # SQLite 商品库 / 搜索词 / 父 SKU 映射
│   ├── sellfox_client.py       # 赛狐 API 客户端 + 多账号 Cookie 上下文
│   ├── sync_service.py         # 按店铺分片的增量同步编排
│   ├── scheduler.py            # APScheduler 定时任务
│   └── ad_coverage.py          # 父 SKU 广告开启状态判定（纯逻辑）
├── static/
│   └── index.html              # 单文件 Vue 3 SPA（UI 全部在这里）
├── data/
│   └── ja_zh_dict.json         # 中日名词对照词库（唯一随仓库发布的业务数据）
├── samples/
│   ├── AdvertisingBulksheetTemplate-seller.xlsx   # 亚马逊官方空白模板（回归基准）
│   ├── fixtures/sellfox_export_demo.xlsx          # 全合成演示导出表
│   └── patch_v7_nouns.json / example_import_nouns.json
├── tools/
│   ├── migrate_to_external.py  # 把 data/ config/ 一键搬到项目外
│   ├── rebuild_from_report.py  # 从亚马逊回传报错报告反向重出表格
│   └── make_demo_fixture.py    # 生成合成测试样本
└── tests/                      # 8 个验收脚本，run_all 共 291 条断言
```

> `data/local_store.db`、`config/sf_cookies.json` 等**运行时数据不属于仓库**，见 [3.3 数据目录](#33-数据目录把数据库和-cookie-放到项目外强烈建议)。

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

启动后访问：**<http://127.0.0.1:8000>**

> 终端会打印当前词库条数，例如：`本地词库：377 条 (…/data/ja_zh_dict.json)`

#### Windows 一键启动（免敲命令）

不想每次敲命令的话，直接**双击项目根目录的 `start.bat`** 即可：

- 首次运行会自动创建 `venv` 并安装依赖（约 1-2 分钟）；
- 之后每次双击就直接启动服务并自动打开浏览器；
- **关闭那个黑窗口 = 停止服务**。

> 前提：机器上已安装 Python 3.10+，且安装时勾选了 *Add Python to PATH*。

### 3.3 数据目录：把数据库和 Cookie 放到项目外（强烈建议）

本项目要上传 GitHub，而 `data/`（SQLite 数据库）和 `config/`（赛狐登录态）**只属于本机**。
除了 `.gitignore` 排除，更稳妥的是**物理隔离**——让它们根本不在仓库目录里。

```bash
# 一次性迁移：把现有数据搬到 %USERPROFILE%\SellfoxAdsData（会自动校验行数）
python tools/migrate_to_external.py --move

# 之后启动时指定数据目录
# Windows:
set SELLFOX_HOME=%USERPROFILE%\SellfoxAdsData
python main.py
# macOS / Linux:
export SELLFOX_HOME="$HOME/SellfoxAdsData"
python main.py
```

Windows 用户可以直接双击 **`启动（数据外置）.bat`**，它会自动设置好 `SELLFOX_HOME`。

迁移后的目录结构：

```
amazon-bulk-ads-generator/     ← 只有代码，可安全 git push
  core/  static/  tools/  main.py  ...

%USERPROFILE%\SellfoxAdsData\  ← 本地数据，永不进仓库
  data/local_store.db          商品库 + 每日搜索词/ASIN 报告
  data/scheduler_config.json   定时任务时间
  data/ja_zh_dict.json         日中名词词库
  data/chrome-login-profile/   半自动登录的浏览器配置
  config/sf_cookies.json       赛狐登录态（多账号）
  config/sf_accounts.json      「记住密码」的账号密码（可选）
```

> 不设 `SELLFOX_HOME` 时仍然使用项目内的 `data/` 和 `config/`，行为与旧版一致。
> 想确认当前用的是哪套目录，访问 `GET /api/system/paths`。

### 3.4 使用流程

#### 路径 A · Excel 离线出表（零配置，v10 起就有）

1. 把赛狐导出的 `.xlsx` **拖入页面**（或点击上传区选择文件）。
2. 切换 **店铺 Tab**，核对「清洗后品名 / 客单均价 / 分配预算」。
3. 在「投放与预算策略」里设置：竞价策略、预算分配模式、默认竞价、广告位溢价、自动 4 组竞价。
4. 勾选要投放的父体（支持全选 / 排序），点击 **生成亚马逊 Bulk 表格** 下载。
5. 到亚马逊广告后台 → **批量操作 → 上传**。建议先传小批量验证，再传全量。

#### 路径 B · 本地商品库 + 自动广告

1. 在「同步与 Cookie 设置」完成赛狐登录（见 3.5），点 **同步商品**，让本地库有数据。
2. 自动广告模块选 **载入本地商品库** → 选店铺 → 表格里可看商品图、客单价、建议预算、**广告状态**。
3. 勾 **只看未开** 可筛出尚未投放的父体；`↻` 可强制刷新覆盖判定缓存（默认缓存 10 分钟）。
4. 二选一收尾：
   - **生成 Bulk 表格** → 下载后到亚马逊后台上传；
   - **⚡ 接口直接创建** → 直接调赛狐接口按「一个父体一个自动活动」创建，活动名与 Bulk 同口径（`父SKU_品名_自动_YYYYMMDD`）。

#### 路径 C · 搜索词 / 投放产生 ASIN → 手动广告

1. **查询并同步**：按所选店铺 + 起止日期拉取搜索词报表（本地已有日期自动跳过），拉完自动查询。
2. 切换维度：**用户搜索词**（投关键词）/ **投放产生 ASIN**（把出单 ASIN 投到目标 ASIN 上，此维度才有「投 ASIN」列）。
3. 设置匹配类型（可多选）、竞价倍数、活动名模板（支持 `{来源活动}`，自动清洗为 `SKU+产品名`）。
4. **导出 Bulk** 或 **接口直接创建**。关键词投放与商品投放混合时，接口创建会自动拆成两次请求（赛狐限制），Bulk 导出不受此限制。

> 接口创建属于**写操作**，会真的在赛狐后台建活动。首次使用建议先用小批量 + 单一店铺验证。

### 3.6 版本、测试与发布

| 项 | 值 |
| --- | --- |
| 当前版本 | `v2.7.0`（`main.__version__` / `GET /api/health`） |
| GitHub 上一版 | `v10.0.0`，commit `c60aff0`，2026-10-30，无 tag / 无 Release |
| 验收测试 | `bash tests/run_all.sh` → **PASS 291 / FAIL 0**（7 个脚本）；另有 `test_7_multi_account.py` 9 项独立断言 |
| 变更记录 | `CHANGELOG.md`；新增版本需同时更新 `__version__` 与发版说明 |

发布流程见 [docs/RELEASE_NOTES_v2.7.0.md](docs/RELEASE_NOTES_v2.7.0.md) 末尾的「维护者发布清单」。

---

### 3.5 赛狐账号登录（支持多账号）

一个赛狐账号只能看到**自己权限内**的店铺。有多个账号时，把每个账号分别加进「赛狐账号池」，
点「全量同步」就会**逐个账号各拉一遍**，登录态互不覆盖。

三种添加方式：

| 方式 | 操作 | 适用 |
| --- | --- | --- |
| **账号密码登录**（推荐） | 点「🔑 账号密码登录」→ 填账号密码 → 点「打开浏览器登录」→ 在弹窗里**输入图形验证码** → 自动抓 Cookie 入池 | 日常刷新登录态 |
| 粘贴 Cookie | 点「＋ 粘贴 Cookie」，把浏览器 F12 里的 cookie 行粘进去 | 临时应急 |
| Chrome 调试模式 | 双击 `启动Chrome调试模式.bat`，在独立窗口登录后点「从调试端口抓取」 | 需要可视化排查 |

> **为什么不能纯账号密码自动登录？** 赛狐登录页强制**图形验证码**（手机号/密码/验证码三项），
> 没有任何公开接口能跳过。所以采用「程序填账号密码 + 人工只输 4 位验证码」的半自动方式。
>
> 赛狐登录页有「5 天内自动登录」选项，Cookie 一般 **5 天左右过期**，到期重新登录一次即可。
> 勾选「记住密码」后，密码保存在 `SELLFOX_HOME/config/sf_accounts.json`（项目外，不进 Git）。

---

## 4. 赛狐 ERP 导出指标要求

导出时**必须勾选以下 10 个指标**，缺一不可（尤其是「店铺」和「价格」）：

| #  | 指标        | 用途                     | 是否必需 |
| -- | --------- | ---------------------- | ---- |
| 1  | **类型**    | 区分父体 / 子体（判定父子关系）      | ✅ 必需 |
| 2  | **父ASIN** | 父体聚合键之一                | ✅ 必需 |
| 3  | **ASIN**  | 子体标识、Bulk 表 ASIN 列     | ✅ 必需 |
| 4  | **MSKU**  | 子体标识、Bulk 表 SKU 列      | ✅ 必需 |
| 5  | **标题**    | 日文原标题 → 中文品名匹配的输入      | ✅ 必需 |
| 6  | **品名**    | 备用展示名（匹配失败时兜底参考）       | ✅ 必需 |
| 7  | **SKU**   | 父 SKU 识别（`parent_sku`） | ✅ 必需 |
| 8  | **店铺**    | **多店铺隔离分层键**           | ✅ 必需 |
| 9  | **价格**    | 客单价中位数 / 加权预算池         | ✅ 必需 |
| 10 | **图片**    | 页面预览缩略图                | 建议勾选 |

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

<summary><b>Q3. 报「身份证无效 / 此Create操作要求您指定临时"广告活动编号"（任何非数字文本）」？</b></summary>

亚马逊规定：`Create` 操作的实体编号必须是**临时 ID**，即**任何非数字文本**。  
纯数字（例如父 SKU 恰好是 `12`、`15`）会被当成**已存在的真实活动 ID**，于是 Create 被拒、**整表回滚**。

v10 已修复：生成器会自动把纯数字父 SKU 加 `c` 前缀（`12` → `c12`），并保证同一张表内活动 ID 唯一；`_validate_output()` 会在本地就拦下纯数字 ID、重复 ID 和孤儿引用。

若你手工改过表格的「广告活动编号」列，请确保它**不是纯数字**，且 Campaign / Ad Group / Product Ad / Product Targeting 四类行用的是同一个值。

</details>

<details>

<summary><b>Q4. 表头识别不出来 / 品名全是「商品」？</b></summary>

分两种情况：

- **解析失败（报 400）**：多半是导出表缺列。请回到赛狐按第 4 节的 10 个指标**重新导出**（特别是「店铺」「价格」「MSKU」「ASIN」）。前端会直接弹出精确原因。
- **能解析但品名不准**：说明日文标题里的名词没被词库收录。解决路径：`管理` 里补词 → 点 `🔄 刷新品名匹配`。无需重新上传表格。

</details>

<details>

<summary><b>Q5. <code>pip install</code> 依赖冲突 / 装不上？</b></summary>

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

<summary><b>Q6. 端口 8000 被占用怎么办？</b></summary>

打开 `main.py`，修改文件末尾：

```python
host, port = "127.0.0.1", 8000     # ← 把 8000 改成其它端口，如 8010
```

</details>

<details>

<summary><b>Q7. 生成的表格行数比预期少？</b></summary>

检查「搜索顶部 / 商品页面溢价 (%)」是否填了 `0`。

溢价为 0 属于**空操作**，生成器会跳过对应的 `Bidding Adjustment` 加成行（少一行就少一个被亚马逊校验的字段组合）。若需要加价，把溢价填成非 0 的数字，加成行会重新生成。

</details>

<details>

<summary><b>Q8. 上传后报告里有大量「由于其他行的错误，该行未得到处理」？</b></summary>

这是**级联警告，不是真实错误**。亚马逊只要有少数行报错，其余行就会显示「未得到处理」。

**看报告时只看「错误类型 = 用户输入无效 / 无效实体」的行**，忽略「未处理 / 警告」。修掉真实错误后，级联警告会自动消失。

</details>

<details>

<summary><b>Q9. 想批量试传，但全量文件太大？</b></summary>

用 `tools/rebuild_from_report.py` 可以从亚马逊回传的报错报告反向重建商品集合并重出表格：

```bash
python tools/rebuild_from_report.py <report.xlsx> <输出目录>
# 产出：全量.xlsx + 测试_仅1个活动.xlsx（子体最少的那个活动，便于先小文件验证）
```

</details>

<details>

<summary><b>Q10. 载入本地商品库后图片全是白块？</b></summary>

不是数据缺失，是浏览器直连 `m.media-amazon.com` 常被防盗链 / 网络拦截。

v2.6 起所有商品图统一走后端 `GET /api/image-proxy?u=...`：白名单域名校验 + sha1 落盘缓存（`data/image_cache/`）+ 7 天 `Cache-Control`。若仍是白块，检查后端日志里代理请求的状态码，以及 `SELLFOX_HOME/data/image_cache/` 是否可写。

</details>

<details>

<summary><b>Q11. 「查询并同步」到底同步了哪段时间？</b></summary>

同步范围**严格取页面上的店铺下拉 + 起止日期**，默认最多 92 天。本地已经存在该店铺该日期数据时会自动跳过（`force=false`），重复点不会重复拉取；单日失败只在该日写一条 `error`，不影响其它日期。

需要强制重拉时调用 `POST /api/sync/search-terms` 并传 `"force": true`。

</details>

<details>

<summary><b>Q12. 搜索词少了几行 / 某些店铺查不到数据？</b></summary>

两类原因：

- **Cookie 失效**：赛狐返回 `2001 验证失败` 或 `360000 没有店铺访问权限` → 在「同步与 Cookie 设置」重新登录。Cookie 一般 5 天左右过期。
- **串号**：多账号场景下，某个店铺只能用**归属账号**的 Cookie 访问。`shops.account_id` 记录归属；v2.4 起同步会先探测当前账号可访问店铺再过滤。若看到「店铺不属当前账号」提示，切换到对应账号即可。

> 搜索词必须**按店铺分片拉取**：全账号一次拉会因赛狐排序不稳定导致漏 5%~19% 行。

</details>

<details>

<summary><b>Q13. 「广告状态」里的已暂停 / 未开启有什么区别？</b></summary>

判定规则：**只有活动 `state == enabled` 才算「已开启」**，`paused` 与 `archived` 都不算。

| 显示 | 含义 |
| --- | --- |
| 🟢 已开启 | 找到至少一个 enabled 活动，且广告商品 SKU 归属该父体 |
| ⚪ 已暂停 | 只找到 paused / archived 活动，没有 enabled 的 |
| ⚪ 未开启 | 完全没找到相关活动 |
| 查询中 / 未知 | 覆盖数据尚未返回，或取数失败（此时不阻塞商品表） |

首次查询会拉活动与广告商品，大店铺约 20 秒；结果缓存 10 分钟，可点 `↻` 强制刷新。

</details>

<details>

<summary><b>Q14. 接口直接创建报「不能同时提交关键词与商品投放」？</b></summary>

赛狐的 `spTargetInfo.keywordTarget` 是二选一开关：`True + keywords[]` 或 `False + targetings[]`，同一次请求不能混合。

手动广告接口创建会自动拆成「原活动」+「原活动_ASIN」两次请求；**Bulk 导出不需要拆分**，亚马逊允许同一广告组里 Keyword 与 Product Targeting 共存。如果遇到其它创建失败，先用 Bulk 导出验证配置，再切回接口创建。

</details>

---

## 附录 A · API 一览

完整 OpenAPI 文档：启动后访问 **<http://127.0.0.1:8000/docs>**。以下按模块列出主要接口。

**基础 / Excel 链路（v10 起）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 单页应用入口 |
| `GET` | `/api/health` | 健康检查：版本号、Cookie 状态、库统计、`db_path` |
| `GET` | `/api/system/paths` | 当前 `SELLFOX_HOME` / 数据 / 配置实际路径 |
| `POST` | `/api/analyze` | 上传赛狐 Excel → 按店铺分层的商品聚合 + 预算建议 |
| `POST` | `/api/generate` | 勾选商品 + 配置 → 下载亚马逊 Bulk Excel |
| `POST/GET/DELETE` | `/api/dictionary*` | 词库增删改查、导入导出、磁盘重载、`rematch` 重匹配 |

**账号 / Cookie / 同步（v2.4 起）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET/POST` | `/api/accounts` | 账号池列表 / 新增 |
| `POST` | `/api/accounts/{id}/activate` `…/probe` | 切换当前账号 / 探测可访问店铺 |
| `DELETE` | `/api/accounts/{id}` | 移除账号 |
| `POST` | `/api/accounts/login` + `/status` + `/cancel` | 半自动账号密码登录（人工输验证码） |
| `GET/POST` | `/api/cookie/status` `/api/cookie/update` | 登录态查询 / 手动粘贴 |
| `GET` | `/api/cookie/shop-access` | 探测当前账号可访问店铺（结果缓存 5 分钟） |
| `GET/POST` | `/api/shops` `/api/shops/register` `/api/shops/discover` | 店铺列表 / 登记 / 发现 |
| `POST` | `/api/sync/products` | 按店铺同步商品到本地库 |
| `POST` | `/api/sync/search-terms` | 按店铺 + 起止日期同步搜索词（`force` 可强制重拉） |
| `POST` | `/api/sync/search-terms/range` + `/status` | 后台批量区间同步 + 进度 |
| `POST` | `/api/sync/all-accounts` + `/status` | 多账号一键全量同步 + 进度 |
| `GET/POST` | `/api/scheduler/*` | 定时任务状态 / 启停 / 配置 / 立即执行 |

**商品库 / 广告工作台（v2.6 ~ v2.7）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/products/{shop_id}` | 本地商品库原始列表 |
| `GET` | `/api/products/{shop_id}/payload` | 与 `/api/analyze` **同结构**的商品聚合（自动广告用） |
| `GET` | `/api/shops/{shop_id}/ad-coverage` | 父 SKU 广告开启状态（`refresh` / `days`，10 分钟缓存） |
| `GET` | `/api/image-proxy?u=` | 商品图代理（域名白名单 + sha1 落盘缓存） |
| `POST` | `/api/auto-ads/create-via-api` | 自动广告接口直接创建 |
| `POST` | `/api/search-terms/query` | 搜索词 / 投放产生 ASIN 双维度查询 |
| `POST` | `/api/manual-ads/export-bulk` | 手动广告 Bulk 导出 |
| `POST` | `/api/manual-ads/create-via-api` | 手动广告接口直接创建（混合投放自动拆单） |

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
# 一键跑全部：test_4 需要后端已在 127.0.0.1:8000 运行；test_6 需要 jsdom
bash tests/run_all.sh
# 多账号隔离的独立用例（不在 run_all 内，需 PYTHONPATH 指向仓库根）
PYTHONPATH="$(pwd)" venv/Scripts/python.exe tests/test_7_multi_account.py
```

| 脚本 | 断言 | 覆盖内容 |
| --- | --- | --- |
| `test_1_noun_matching.py` | 53 | 名词匹配五项准则、修饰词后缀、性能、倒排索引一致性 |
| `test_2_end_to_end.py` | 80 | 解析 → 预算 → 生成；表头与官方 31 列逐字比对；字段合规；v10 临时 ID 事故回归 |
| `test_3_dictionary_io.py` | 24 | 词库增删改查、原子写入、热重载 |
| `test_4_http_smoke.py` | 42 | HTTP 接口冒烟（需后端运行） |
| `test_5_v7_quantity_and_rematch.py` | 42 | 数量词清洗、`match_noun_only`、rematch 契约、真实数据端到端 |
| `test_8_ad_coverage.py` | 27 | 广告状态字段兼容、enabled/paused/archived 判定、活动名兜底整词匹配 |
| `test_6_frontend_refresh.js` | 23 | 前端「刷新品名匹配」交互（jsdom + 真实 Vue 运行时） |
| **run_all 合计** | **291** | `PASS 291 / FAIL 0` |
| `test_7_multi_account.py` | 9 | 账号池隔离、`get_account` 不回退 active、店铺归属（独立运行） |

> `tests/run_all.sh` 默认使用项目 `venv/Scripts/python.exe`；用系统 Python 会因缺 `apscheduler` 让 `test_5` 误报失败。
> `test_2` 会把 `BULK_COLUMNS` 与 `samples/AdvertisingBulksheetTemplate-seller.xlsx` 官方表头逐字比对，任何列名漂移都会被拦下。

---

## 安全与隐私

- `config/sf_cookies.json`、`config/sf_accounts.json`、`data/local_store.db` 及 Chrome profile **全部禁止进仓库**（`.gitignore` 已排除）；推荐用 `SELLFOX_HOME` 物理外置，见 [3.3](#33-数据目录把数据库和-cookie-放到项目外强烈建议)。
- 仓库内不含任何真实店铺名 / shop_id；测试与示例统一使用 `店铺A-JP…` / `100001…` 占位。
- **接口直接创建会真的在赛狐后台建活动**，请先小批量验证。
- 商品图代理只允许 `m.media-amazon.com` 等 5 个亚马逊域名，不接受任意 URL，避免 SSRF。

## 许可

内部运营工具，未选择开源许可证；未经授权不得公开分发。
`data/ja_zh_dict.json` 为业务词库资产，请在团队内部使用。
