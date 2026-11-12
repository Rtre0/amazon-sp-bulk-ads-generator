# Changelog

本文件记录 Amazon SP Bulk Ads Generator 的所有显著变更。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[Semantic Versioning 2.0](https://semver.org/lang/zh-CN/)：`MAJOR.MINOR.PATCH`。

---

## 版本线沿革（先读这里）

| | GitHub 上的版本（本次推送前） | 本次推送后的版本 |
| --- | --- | --- |
| 版本号 | **v10.0.0** | **v2.7.0** |
| 最后提交 | `c60aff0` | `4c3f4ba`（tag `v2.7.0`） |
| 日期 | 2026-10-30 | 2026-11-12 |
| 定位 | Excel → Bulk 单文件生成器 | 数据同步 + 商品分析 + 双模块投放工作台 |
| 网络 | 运行时零外部依赖 | 离线路径仍零依赖；在线同步/接口创建需赛狐登录态 |
| 验收断言 | 264 | 291（+ 9 项独立） |
| Tag / Release | 无 | `v2.7.0` |

> **为什么不是 v11？** `v1.0.0 → v10.0.0` 是「单文件 Bulk 生成器」产品线的迭代号。
> 2026-11 的架构重构（接入赛狐 API、SQLite、双模块）在提交信息中启用了 `v2.x` 版本号，
> 之后所有迭代（v2.4 / v2.6 / v2.7）都沿用了这条新版本线。为避免出现「v2.7 比 v10 旧」
> 的误读，**本次发布统一以 v2.7.0 为准，旧 v10 编号停止使用**，并在代码 `main.__version__`
> 与 `GET /api/health` 中收敛为唯一来源。

从 `c60aff0` 到 `4c3f4ba` 共 **10 个功能提交**，全部包含在本次推送中；
另外有一个版本号统一 + 维护文档的发布提交（`v2.7.0` tag 指向它）。

---

## [Unreleased]

暂无。后续变更请先写在这里，发版时再归入对应版本段落。

---

## [2.7.0] - 2026-11-12

> 提交：`ffb8dc1`（判定模块）、`7b728d7`（接口与前端）、`4c3f4ba`（测试脚本）

### Added

- **父 SKU 广告开启状态判定**：新增 `core/ad_coverage.py`。以活动 `state` 为准，
  只有 `enabled` 算「已开启」，`paused` / `archived` 均不算；广告商品行的 `sku`
  通过 `db.get_parent_sku_map()` 归属到父体，活动名作为兜底匹配来源。
- **商品表「广告状态」列**：🟢 已开启 / ⚪ 已暂停 / ⚪ 未开启 / 查询中 四态标签，
  支持 `↻` 强制刷新与「只看未开」筛选；统计栏显示 `已开 N / 未开 M（其中已暂停 K）`。
- **`GET /api/shops/{shop_id}/ad-coverage`**：10 分钟内存缓存 + 单飞锁，
  支持 `refresh` / `days`；取数失败降级 `ok=false`，不阻塞商品表渲染。
- **`tests/test_8_ad_coverage.py`**：27 项断言，覆盖状态字段兼容、三种状态、
  混合状态、活动名兜底、整词边界（`hua22` 不得命中 `hua226`）与结构完整性。

### Changed

- `tests/run_all.sh` 默认改用项目 `venv/Scripts/python.exe`（原先指向系统 Python，
  因缺 `apscheduler` 导致 `test_5` 的 rematch 用例误报 1 FAIL）；找不到 venv 才回退。
- `main.py` 版本号收敛为 `__version__ = "2.7.0"`，FastAPI 元数据与 `/api/health` 共用。

### Performance

- 覆盖判定只查询 **enabled 活动**的广告商品（实测 2204 → 291 个活动），
  活动名兜底改为「N 个父 SKU 合成一条交替正则 + 整词边界」，取代
  「活动 × 父体」双循环（2204 × 521 ≈ 115 万次匹配）。
  真实店铺首次查询 **60s+ 超时 → 20.1s**，缓存命中 **0.24s**。

### Fixed

- 活动名兜底误匹配：加入 `(?<![0-9A-Za-z])...(?![0-9A-Za-z])` 边界，
  避免短 SKU 命中更长 SKU。

---

## [2.6.0] - 2026-11-12

> 提交：`7b728d7`（主要）、`ffb8dc1`（同步后端）

### Added

- **自动广告接口直接创建**：复用 `build_auto_ad_payload` + `create_sp_ad`，
  新增 `POST /api/auto-ads/create-via-api`。一个父体一个自动活动，
  活动名与 Bulk 导出同口径 `父SKU_品名_自动_YYYYMMDD`；`targetingType=auto` +
  4 组自动竞价，竞价方案映射 `Fixed bid→manual` / `Down only→legacyForSales` /
  `Up and down→autoForSales`。Bulk 导出能力完整保留。
- **图片代理 `GET /api/image-proxy?u=`**：5 个亚马逊图床域名白名单、
  sha1 落盘 `data/image_cache/`、`Cache-Control: max-age=604800`，
  前端 `imgSrc()` 统一改写图片地址并加 `referrerpolicy="no-referrer"`。
- **父 SKU 映射 `db.get_parent_sku_map()`** 与 **`db.count_rows_for_date()`**，
  分别支撑广告覆盖归属与「已同步日期跳过」。

### Changed

- 手动广告「查询」与「同步」按钮合并为 **「查询并同步」**：按页面所选
  **店铺 + 起止日期**（最多 92 天）同步后自动查询；`POST /api/sync/search-terms`
  新增 `start_date` / `end_date` / `force`，`force=false` 时跳过本地已有日期。
- 「投 ASIN」列与「🎯 ASIN 商品投放」区块**仅在「投放产生 ASIN」维度显示**。
  用户搜索词维度里 ASIN 本身就是投放对象，再勾等于重复投放自己。
- 来源活动名清洗升级为 **SKU + 产品名**：日期截断 → 按 `空格 / , / - / _ / ー` 切段
  → 过滤噪音词（自动/手动/精准/词组/宽泛/广告/投放/关键词/匹配/复制）→ 组装。
  例：`lyl5-亚克力桌ーC-自动投放-15:59` → `lyl5-亚克力桌`。
- Cookie 设置页移除全部静态店铺名与备注里的真实组织名；
  店铺相关文案只在读取到用户 Cookie 后由 `/api/shops` 驱动显示。
- 账号与工具脚本中的真实店铺名 / shop_id 统一替换为 `店铺A-JP…` / `100001…`。

### Fixed

- **同步按钮不按所选时间范围同步**（用户实测）：原实现固定 `days=1`、`shop_id=""`，
  实际只拉当天全账号。重写为按日期区间逐店铺同步。
- **区间同步单日失败导致整体 500**：改为 `try/except` 在该日 `results` 写一条 `error` 后继续。
- **本地商品库图片全白**：定位为浏览器直连图床被防盗链拦截（88,371 / 88,678 行本有图片 URL），
  改由后端代理后实测 564 张 0 失败。
- **活动名清洗残留「手动_手动」**：赛狐活动名分隔符主要是**空格**，
  初版只按 `- _ ー` 切段切不动，补上按空格切词。

---

## [2.5.0] - 2026-11-12

> 提交：`2bc68d5`

### Added

- `core/paths.py`：数据 / 配置路径唯一出口。设置 `SELLFOX_HOME` 后
  `data/`、`config/` 整体搬到项目外；未设置时行为与旧版完全一致。
- `tools/migrate_to_external.py`（带行数校验的一次性迁移）与
  `启动（数据外置）.bat`（自动设置环境变量并启动）。
- `GET /api/system/paths`：随时确认当前实际使用的数据目录。

### Changed

- `database` / `translator` / `scheduler` / `sellfox_client` / `main` 统一从
  `core.paths` 取路径，不再各自拼 `os.path.dirname(...)`。
- `.gitignore` 明确排除 Cookie、Cookie 备份、账号密码、SQLite 及 Chrome profile。

### Security

- 清除仓库内全部真实店铺名、shop_id 与组织名（含注释、docstring、测试夹具、
  排障工具）；测试与示例统一使用占位名，为公开仓库做准备。

---

## [2.4.0] - 2026-11-11

> 提交：`d9ba7cf`

### Added

- **多赛狐账号池**：`config/sf_cookies.json` 升级为
  `{_version, active, accounts[]}`（旧扁平格式自动迁移）；
  `ContextVar` 账号上下文 + `use_account(aid)` 隔离，`load_cookies()` 跟随上下文。
- 店铺归属字段 `shops.account_id`；`upsert_shop` 传空 `account_id` 时保留原归属。
- **一键全量同步** `sync_service.sync_all_accounts(kind, days, account_ids)`，
  逐个账号各拉一遍，登录态互不覆盖。
- 前端零延迟的「店铺不属当前账号」提示（用 `accounts[active].shops` 本地判断）；
  `/api/cookie/shop-access` 首次探测约 12~14s，加 5 分钟缓存。

### Fixed

- **搜索词漏行 5%~19%**：全账号一次拉取会翻页，而赛狐排序不稳定。
  改为**按店铺分片拉取**，并统一传 `campaignIdList=[]`（空数组 = 全店铺，
  单店 153s → 6.8s）。
- **商品同步被 `max_pages` 截断**：改为逐店铺拉取，
  且先探测「当前账号可访问店铺」再过滤，不能直接遍历全局 `shops` 表。
- **串号**：`get_account(aid)` 找不到指定账号时必须返回 `None`，
  绝不回退 `active`（曾表现为「搜索词少几个 / ASIN 出单查不到」）。

---

## [2.3.0] - 2026-11-11

> 提交：`b964139`

### Added

- 手动广告**匹配类型多选**（`mtMatchTypes`）：勾 N 种 → 每个搜索词活动生成 N 条 Keyword；
  活动名后缀单选用全称，多选用 `精准+词组+宽泛` 短标签拼接。
- **ASIN 商品投放**：表达式固定 `asin="B0XXXXXXXX"`；`_validate_output()` 除自动广告
  4 组裸受控值外额外放行该正则，其它非法值仍拦截。
- 活动名「模板生成 + 逐行 override」：`mtNameOverrides` 以数据库主键 `r.id` 为 key
  （合并模式用 `__single__`），新查询时清空。

### Changed

- 明确 `spTargetInfo.keywordTarget` 是二选一开关：同一赛狐创建请求不能同时提交
  关键词投放与商品投放；`create_manual_via_api()` 自动拆成「原活动」+
  「原活动_ASIN」两次创建。**Bulk 导出不受限制**。
- 手动广告多活动场景按 campaign 序号单独分配临时 ID，避免多个活动共用编号被亚马逊合并。
- 手动广告竞价基准改用 `cpc` 而非 `suggested_bid`（自动搜索词行 suggested 恒为 0，
  实测 25/25 有 CPC、仅 1/25 有建议竞价）。

---

## [2.2.0] - 2026-11-11

> 提交：`d4cdfc9`

### Added

- 手动广告**竞价倍数批量应用**与**按来源活动自动命名**（`{来源活动}` 模板变量）。
- 来源活动名清洗：从第一个 8 位日期 `\d{8}` 起整段截断 + 去尾部分隔符，
  覆盖 `_20261027` / `-20260801 10:21:16.512-9` / `_1 复制 1` 等尾部形态。

### Fixed

- 搜索词报表 `asin` / `msku` 字段来自 `adProduct` 映射，指的是**自家广告商品**
  （9789 行全非空、3169 个 distinct），可直接用于商品投放；
  `query_search_terms()` 改为 `SELECT *`，前端可直接取到 `id` / `asin` / `msku`。

---

## [2.1.0] - 2026-11-11

> 提交：`3d47d3d`、`ad2f544`

### Added

- Cookie **手动粘贴**成为主推方式，兼容多种浏览器复制格式
  （原始 `name=value; ...` 行、JSON、带 `Cookie:` 前缀等）。
- 内联 SVG favicon，消除浏览器 favicon 404。

---

## [2.0.0] - 2026-11-11

> 提交：`218b86a` 及同批架构改动

### Added

- **赛狐 API 接入**：不再依赖手工导出 Excel，可自动拉取商品与搜索词报表。
- **本地 SQLite 持久化**（WAL）：商品库、每日搜索词 / 投放产生 ASIN 报告、
  增量去重；`GET /api/products/{shop_id}/payload` 与 `POST /api/analyze`
  共用 `core/budget_engine.build_shop_payload`，保证两条入口口径一致。
- **APScheduler 定时任务**：商品每周一 03:00、搜索词每日 02:00 增量同步。
- **双模块**：自动广告模块保留 v1~v10 全部能力；新增手动广告模块
  （搜索词出单分析 + 一键创建手动关键词广告）。
- **Cookie 管理**：Playwright CDP 自动抓取 + 手动粘贴两条路径。

### Changed

- **兼容性铁律**：曾有一版重写自动广告模块导致丢失商品图、竞价、预算与中位数排序。
  自此任何版本升级只允许「在稳定前端基底上追加」，不允许改结构。
  稳定基线固定为 `c60aff0`（v10）。

### Fixed

- `218b86a` 恢复自动广告模块为 v10 完整功能，v2.0 只在外层新增。

---

## [10.0.0] - 2026-10-30 · 旧版本线终点

> 提交：`c60aff0` —— GitHub 上本次推送前的最后一个版本

- 修复临时 ID 为纯数字导致 `Create` 被亚马逊整体拒绝
  （`_temp_id()` 加 `c` 前缀、`_plan_temp_ids()` 保证唯一、`_check_temp_ids()` 自检）。
- 断言数 254 → 264。
- 保留 v9 官方 31 列表头对齐、v8 受控值修正、v7 离线名词词库。
- 详见该提交的完整说明；此后版本线切换为 v2.x。

---

## 维护约定

1. 新功能先写进 `[Unreleased]`，发版时归入新版本段落。
2. 发版必须同步更新：`main.__version__`、`CHANGELOG.md`、
   `docs/RELEASE_NOTES_<version>.md`、README 顶部版本徽章与测试断言数。
3. 打 annotated tag（`git tag -a vX.Y.Z -m ...`）并推送，Release 正文引用发版说明。
4. **提交前检查行尾符**：`main.py` 为 CRLF、`static/index.html` 为 LF；
   编辑工具会翻转它们，导致 diff 膨胀成整文件重写。
