# Release Notes · v2.7.0

| | |
| --- | --- |
| **版本** | `v2.7.0` |
| **发布日期** | 2026-11-12 |
| **Tag** | `v2.7.0`（annotated） |
| **提交范围** | `c60aff0` → 发布提交（10 个功能提交 + 1 个版本/文档提交） |
| **仓库** | `Rtre0/amazon-sp-bulk-ads-generator`，分支 `main` |
| **验收** | `bash tests/run_all.sh` → PASS 291 / FAIL 0；多账号独立用例 9 项通过 |
| **文档** | [README](../README.md) · [CHANGELOG](../CHANGELOG.md) · [架构](ARCHITECTURE.md) |

---

## 1. GitHub 上原本是什么版本

推送前抓取的远程状态（2026-11-12）：

```
refs/heads/main  c60aff040f0934ccd4124c419f80e862518b8fe8
```

| 项 | GitHub 上的实际情况 |
| --- | --- |
| 最新提交 | `c60aff0` — `fix(bulk): 修复临时ID为纯数字导致 Create 被亚马逊拒绝（身份证无效）`，2026-10-30 |
| 内部版本号 | `10.0.0`（`main.py` FastAPI `version=` 与 `/api/health`） |
| 提交数 | 4 个：`e0bf32e` initial release v1.0.0 → `0113a24` 全合成测试样本 → `5644d14` start.bat → `c60aff0` |
| Tag / Release | **均无**（`git ls-remote --tags` 为空，Releases 页面显示 "There aren't any releases here"） |
| README 状态 | 徽章写 `264 assertions`、词库 `377 条`、`6 个验收脚本`；技术栈写「运行时零外部依赖：不调用任何翻译 / 广告 / 第三方 API」 |
| 功能范围 | 纯离线工具：上传赛狐导出 Excel → 生成亚马逊 Bulk Excel → 人工上传后台 |

也就是说，**GitHub 上停在一个 13 天前的、只有 Excel 单链路的 v10 版本**，
本地此后完成的全部 v2.x 架构演进从未同步过。本次发布一次性补齐这 10 个提交，
并把 README / CHANGELOG / 架构文档一起纳入版本管理。

> 关于 `v10 → v2.7` 的编号：这不是版本倒退，而是产品线重新编号。
> 完整说明见 [CHANGELOG · 版本线沿革](../CHANGELOG.md#版本线沿革先读这里)。

---

## 2. 本次更新了什么（相对 GitHub 旧版）

### 2.1 能力对照

| 维度 | v10.0.0（GitHub 旧版） | v2.7.0（本次） |
| --- | --- | --- |
| 数据来源 | 只能手工上传 Excel | Excel **或** 赛狐 API 自动同步到本地 SQLite |
| 多店铺 | 单个 Excel 内按列分组 | 分组能力保留；另有店铺表 + 归属账号 |
| 赛狐账号 | 无 | **多账号池**，ContextVar 隔离，逐个账号同步 |
| 定时同步 | 无 | APScheduler，商品周更 / 搜索词日更 |
| 自动广告 | 仅导出 Bulk Excel | Bulk 导出 **+ 接口直接创建** |
| 手动广告 | 无 | 搜索词 / 投放产生 ASIN 双维度、匹配类型多选、商品投放、逐行活动名 |
| 选品辅助 | 价格中位数 / 预算建议 | 再加**父 SKU 广告开启状态**与「只看未开」筛选 |
| 商品图 | 浏览器直连图床（常白块） | 后端白名单代理 + 落盘缓存 |
| 数据与凭据 | 项目内 `data/` `config/` | 可用 `SELLFOX_HOME` 物理外置，不进仓库 |
| 验收断言 | 264 | **291**（+ 9 项独立多账号用例） |
| 文档 | 仅 README | README + CHANGELOG + 发版说明 + 架构文档 |

### 2.2 逐版本摘要

| 版本 | 日期 | 主题 |
| --- | --- | --- |
| **2.0.0** | 11-11 | 赛狐 API 接入、SQLite + WAL、APScheduler、自动/手动双模块、Cookie 管理；确立「升级只允许加功能、不许改结构」铁律 |
| **2.1.0** | 11-11 | 手动粘贴 Cookie 成为主推方式；内联 SVG favicon |
| **2.2.0** | 11-11 | 手动广告竞价倍数批量应用、`{来源活动}` 自动命名、名称尾部清洗 |
| **2.3.0** | 11-11 | 匹配类型多选、ASIN 商品投放、逐行可编辑活动名、`cpc` 竞价基准 |
| **2.4.0** | 11-11 | 多账号池、一键全量同步、按店铺分片修复搜索词漏行与串号 |
| **2.5.0** | 11-12 | `SELLFOX_HOME` 数据外置、路径统一出口、清除仓库内真实店铺名 |
| **2.6.0** | 11-12 | 自动广告接口创建、图片代理、查询并同步按范围、维度化投 ASIN、活动名智能清洗 |
| **2.7.0** | 11-12 | 父 SKU 广告开启状态判定与筛选、广告覆盖接口与缓存、性能优化 |

每条的详细 Added / Changed / Fixed 见 [CHANGELOG.md](../CHANGELOG.md)。

### 2.3 本次（v2.7.0）的重点：找出「还没开广告」的商品

**需求**：在商品表里直接看出某个父体在广告中是否已开启，暂停与归档不算，
方便筛出还没投放的商品去开活动。

**实现**：

- `core/ad_coverage.py` 是纯判定逻辑（无 IO 依赖，可单测）；
  `campaign_state()` 兼容 `state` / `status` / `campaignStatus` 三种字段，
  `ad_product_sku()` 归一化 `sku` / `sellerSku` / `msku`。
- 主判定：活动 `state == enabled` 且广告商品 SKU 能经
  `db.get_parent_sku_map()` 归到该父体 → 已开启。
- 兜底：活动名里包含父 SKU 时也算命中，用 `N 个父 SKU 合成一条交替正则`
  + `(?<![0-9A-Za-z])...(?![0-9A-Za-z])` 整词边界，避免 `hua22` 误命中 `hua226`。
- 接口 `GET /api/shops/{shop_id}/ad-coverage`：10 分钟内存缓存 + `threading.Lock`
  单飞，取数失败返回 `ok=false` 而不抛错，商品表照常渲染。
- 前端：状态列（🟢 已开启 / ⚪ 已暂停 / ⚪ 未开启 / 查询中）、
  `↻` 强制刷新、「只看未开」勾选、统计栏计数。

**实测**（真实店铺，521 个父体 / 2204 个活动 / 2656 个广告商品）：

```
enabled=173   paused=267   none=81
hua226 → 已暂停（活动确实处于 paused）
hua130 / hua238 → 已开启
浏览器：522 行，已开启 173 / 未开启或暂停 348 / 未知 0；勾「只看未开」后 349 行，已开启 0；console 0 报错
```

**性能**：初版「2204 活动 × 521 父体」双循环正则（≈115 万次）超过 60 秒超时；
改为只查 enabled 活动（2204 → 291）+ 合并正则后，首次 **20.1s**、缓存命中 **0.24s**。

---

## 3. 兼容性与破坏性变更

| 变更 | 影响 | 处理 |
| --- | --- | --- |
| 新增 `APScheduler` / `Playwright` 依赖 | 旧环境 `pip install -r requirements.txt` 后需重装 | 见 §4 步骤 3 |
| 新增在线同步 / 接口创建 | **不再是「运行时零外部依赖」** | 离线 Excel 路径仍完全零网络；不配 Cookie 就能用 |
| 新增 `data/local_store.db` 等运行时文件 | 旧版没有这些文件 | 首次启动自动建表，无需迁移 Excel 数据 |
| `config/sf_cookies.json` 结构升级为 `{_version, active, accounts[]}` | 旧扁平 Cookie 文件 | `_load_store()` 自动迁移，无需手工改 |
| 版本号 `10.0.0 → 2.7.0` | 依赖 `/api/health.version` 做数值比较的脚本 | 按语义化字符串解析，不要 `float()` 比较 |
| 数据目录可由 `SELLFOX_HOME` 覆盖 | 不设时行为不变 | 想确认实际路径访问 `GET /api/system/paths` |
| 自动广告模块曾一度被重构破坏 | — | v2.0 起已恢复 `c60aff0` 完整功能并冻结为「只加不改」基线 |

**无数据丢失风险**：本次没有删除或改写既有表字段；`shops.account_id` 为新增列，
`upsert_shop` 传空值时保留原归属。

---

## 4. 升级步骤（从 GitHub 上的 v10 升级）

> Windows 命令用 `venv\Scripts\python.exe`；macOS / Linux 换成 `venv/bin/python`。

```bash
# 0) 停掉正在运行的服务（关闭黑窗口，或结束 python main.py 进程）

# 1) 备份：旧版项目内可能已有本地数据
cp -r data  data.backup.$(date +%Y%m%d)          # 若有
cp -r config config.backup.$(date +%Y%m%d)       # 若有 Cookie

# 2) 拉新代码（远程 main 是 fast-forward，无需 force）
git pull --ff-only origin main
git log --oneline c60aff0..HEAD                  # 10 个功能提交 + 1 个发布提交

# 3) 补依赖（新增 apscheduler / playwright）
venv\Scripts\python.exe -m pip install -r requirements.txt
# Playwright 复用本机 Chrome，不需要 playwright install chromium

# 4) 【推荐】把数据与 Cookie 物理移出仓库
venv\Scripts\python.exe tools/migrate_to_external.py --move
#   脚本会校验行数后再移动；随后用「启动（数据外置）.bat」启动
#   或手动：set SELLFOX_HOME=%USERPROFILE%\SellfoxAdsData && python main.py

# 5) 重新登录赛狐（Cookie 一般 5 天过期）
#    页面 →「同步与 Cookie 设置」→「🔑 账号密码登录」→ 人工输入图形验证码
#    多账号时把每个账号分别加进账号池

# 6) 验证
curl http://127.0.0.1:8000/api/health           # version 应为 2.7.0，检查 db_path
curl http://127.0.0.1:8000/api/system/paths
bash tests/run_all.sh                            # PASS 291 / FAIL 0
```

**验证清单**

- [ ] `GET /api/health` 返回 `"version": "2.7.0"`，`db_path` 指向期望目录
- [ ] 上传一份 Excel 走离线路径，能正常下载 Bulk 表（v10 能力未回归）
- [ ] 同步商品后载入本地商品库，缩略图不是白块
- [ ] 「广告状态」列有值，勾「只看未开」能过滤
- [ ] 手动广告「查询并同步」的日期范围与所选一致
- [ ] 接口创建先用**单店铺 + 小批量**试一次

**回滚**

```bash
git checkout c60aff0 -- main.py core/ static/ tests/ README.md
# 或整仓回退：git reset --hard c60aff0（会丢本地改动，先确认已备份）
```

回滚后 `data/` 与 `config/` 不会被删除；旧版不读 SQLite，忽略即可。
若已用 `--move` 外置数据，把 `SELLFOX_HOME` 取消即可让旧版回到项目内目录。

---

## 5. 验证结果汇总

| 项目 | 结果 |
| --- | --- |
| 回归测试 | `PASS 291 / FAIL 0`（test_1 53、test_2 80、test_3 24、test_4 42、test_5 42、test_8 27、test_6 23） |
| 多账号独立用例 | `test_7_multi_account.py` 9 项通过 |
| 表头合规 | `test_2` 与 `samples/AdvertisingBulksheetTemplate-seller.xlsx` 官方 31 列逐字比对 |
| 图片代理 | 564 张商品图，0 失败 |
| 广告覆盖 | 521 父体 / 2204 活动 / 2656 广告商品，状态分布 `173 / 267 / 81` |
| 浏览器实测 | 522 行渲染、筛选正确、console 0 报错 |
| 语法 / 服务 | `main.py` AST 校验通过；`http://127.0.0.1:8000` HTTP 200 |

---

## 6. 已知限制

1. **赛狐 Cookie 约 5 天过期**，且登录页强制图形验证码，无法全自动登录；
   到期需人工输一次验证码。失效时表现为 `2001 验证失败` 或
   `360000 没有店铺访问权限`，容易与「数据缺失」混淆。
2. **搜索词必须按店铺分片拉取**：赛狐全账号一次拉取会因排序不稳定漏 5%~19% 行，
   且 `totalSize` 恒为 0，分页只能按「本页行数 < pageSize」判断。
3. **`ad-coverage` 首次查询耗时与店铺规模正相关**（约 20s / 2200 活动）；
   已有 10 分钟缓存，超大店铺建议错峰点 `↻`。
4. **接口直接创建是写操作**，会真的在赛狐后台建活动；混合关键词 / 商品投放时
   自动拆成两次请求，Bulk 导出则不需要拆分。
5. **商品投放表达式**、`OFFICIAL_COLUMNS` 31 列 + `ASIN` 辅助列共 32 列
   均为设计约束，不要在表里手工增删列。
6. 本仓库为内部运营工具，未附开源许可证；词库 `data/ja_zh_dict.json`
   属业务资产，请勿外传。

---

## 7. 维护者发布清单

发下一个版本时按此顺序执行（v2.7.0 已按此流程发布）：

1. 把 `[Unreleased]` 条目归入新版本段落，写 `CHANGELOG.md`。
2. 更新 `main.py` 的 `__version__`（FastAPI 与 `/api/health` 共用）、
   README 顶部版本徽章与断言数。
3. **检查行尾符**：`main.py` = CRLF、`static/index.html` = LF。
   编辑工具常把它们翻转，导致 `git diff` 变成整文件重写。
   ```bash
   python -c "print(open('main.py','rb').read().count(b'\r\n'), open('static/index.html','rb').read().count(b'\r\n'))"
   ```
4. 跑 `bash tests/run_all.sh`，确认全绿；浏览器过一遍三个使用路径。
5. 提交、推送、打 annotated tag 并推送：
   ```bash
   git push origin main
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```
6. 在 GitHub Releases 用新 tag 建 Release，正文复制 `docs/RELEASE_NOTES_vX.Y.Z.md`
   的「更新了什么 / 升级步骤 / 已知限制」三节。
