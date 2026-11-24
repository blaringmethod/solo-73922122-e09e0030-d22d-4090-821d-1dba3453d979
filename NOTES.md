# 交付说明与实测记录

日期：2026-11-24。环境：Linux、Python 3.11.2、Flask 3.1.3、gunicorn 27.0.1、pytest、SQLite 3.40.1（依赖装在 `/tmp/venv`，系统 Python 无 pip/flask）。

## 本次要解决的问题

旧实现 `Ledger.replace_leap_table` 成功换表后会 `UPDATE exposures` 重算**所有** UTC 文本行的 `start_real` 并把所有行的 `leap_version` 改成新版本；换表期间持写锁，补录/导入要等全表重写结束。新要求是：换表不重写曝光行与时间索引，只用索引检查拟删闰秒日上的 `23:59:60` 冲突，同时分别保住 UTC 文本的“原 UTC 时刻”和实秒的“原绝对时刻”，并支持旧库原地升级。

## 实现情况

### 1. 与表无关的稳定 UTC 键（`ledger/timecode.py`）

- 新增 `utc_day_ordinal / utc_label_parts / utc_parts_from_text / day_utc_key_window`。
- 键编码 `utc_key = 民用日序号 * 86401 + 当日字面秒`，每日键槽 `0..86400`，`23:59:60` 正好是槽 86400；标签唯一且按时间有序，**不依赖闰秒表**，因此换表后键与索引项都不用动。

### 2. 存储层（`ledger/store.py`）

- `exposures` 新增可空列 `utc_day / utc_sod / utc_key`：UTC 文本行在导入时写稳定键；实秒行留 NULL。基线列（含 `start_real`、`leap_version`、`raw_json`）全部保留。
- 新索引：
  - 部分索引 `exposures_utc_key(utc_key, exposure_id) WHERE start_source='utc'`，供 UTC 行起点窗口；
  - 覆盖索引 `exposures_utc_leap60(utc_sod, utc_day, exposure_id, start_source)`，供 `23:59:60` 冲突检查。
  - 原 `exposures_start_real`、`exposures_start_real_id`、`exposures_batch` 原样保留。
- **换表零重写**：`replace_leap_table` 在 `BEGIN IMMEDIATE` 内只做：取旧表→算拟删闰秒日→`INDEXED BY exposures_utc_leap60` 的覆盖索引查询→（有冲突则带曝光号抛 `LeapSwapRejected` 回滚；相同表 no-op）→仅 `INSERT INTO leap_tables` 新版本。没有任何 `UPDATE/DELETE exposures`、没有 `REINDEX`，也不再改 `exposures.leap_version`（它保持“导入时表版本”的出处语义）。返回新增 `rewritten_rows: 0`、`conflict_count`、`deleted_days`，保留旧键 `updated_text_rows`（恒 0）、`unchanged` 等。
- **日取数双索引窗口**（同一 `BEGIN` 读事务、同一闰秒表快照）：
  - UTC 行 `INDEXED BY exposures_utc_key` 读稳定键窗口 `[前一日 23:00, 次日 00:00)`；
  - 实秒行 `INDEXED BY exposures_start_real_id` 读 `[day_start-3600, day_end)`；
  - Python 侧按当前表从 `start_raw` 重算 UTC 行有效实秒（实秒行用冻结 `start_real`），统一做严格相交判定与左闭右开裁剪，按有效起点+曝光号排序。
  - UTC 行的 `source_start_utc` 直接取规范化后的原标签（保住原 UTC 时刻）；实秒行按新表渲染（绝对时刻不变、UTC 文本可平移，如 12:00:00→12:00:02/03）。
- **原地迁移**：新代码直接打开基线 schema 的旧库时，幂等 `ALTER TABLE ADD COLUMN`，用 `start_raw` 一次性回填稳定键（解析失败才用 `start_real`+当前表兜底），再 `CREATE INDEX IF NOT EXISTS`。不改动任何旧列；`import_batches`、`raw_json`、`start_raw` 原样保留。
- 新增只读 `preview_leap_table(text)`，返回拟删日与索引查到的冲突，便于扩展/自检。

### 3. 网页（`ledger/app.py`、`templates/`、`static/style.css`）

- `/leap` 支持 `review_date`（日明细页“换表并对比这一天”带参跳入，隐藏字段跨 POST 保留），成功/失败面板都给出“回看同一天日明细”的链接。
- 闰秒表页改写说明与成功文案：明确“未重写任何曝光行或时间索引（重写行数 0）”；冲突表显示曝光号/望远镜/原值/原因，超过 500 条只截断**展示**并提示总数（领域层不再截断）。新增“删除 2016-12-31”“清空表”快捷填充与少量原生 JS。
- 导入页新增“填入 2016 跨年夜演示数据 / 追加 23:59:60 冲突演示”按钮；日明细页给每行加 `UTC文本/实秒` 来源标记。
- 新增 `scripts/web_demo.py`（纯标准库 HTTP 校验，17 项断言）与 `demo/leap_night_2016.csv`。
- `Dockerfile` 增加 `COPY scripts`；新增 `.dockerignore`。`docker-compose.yml` 维持单服务、命名卷、8000 端口、`restart: unless-stopped`。

## 实测结果（均为本机真实运行）

1. **单元/集成测试：`python -m pytest -q` → 36 passed。**
   - 原 `tests/test_ledger.py` 的 **27 个用例全部保持通过，未改动该文件**（包括旧名 `..._recomputes_text_rows`：其断言只针对日报告数值，新架构读时重解释给出同样的 61→60 结果）。
   - 新增 `tests/test_upgrade_no_rewrite.py` **9 个用例**：换表后整表 `SELECT *` 快照逐列相同、索引 SQL 相同、`leap_version` 不被改；用 `set_trace_callback` 证明换表 SQL 只有 `BEGIN/SELECT 版本/覆盖索引查冲突/INSERT leap_tables/COMMIT`，无任何 `UPDATE exposures`；`EXPLAIN QUERY PLAN ... INDEXED BY exposures_utc_leap60` 为 `COVERING INDEX`；多个拟删日的 `:60` 全部列出；UTC 时刻/实秒绝对性；多次换表+跨版本导入的混合年代；基线 schema 旧库迁移保批次/raw 且索引能查冲突；换表后 CSV 裁剪。
2. **随机 fuzz**（`/tmp/verify_fuzz.py`，未入库）：12 个随机密/疏闰秒表 × 约 50 个随机日 × 每库 450 行（UTC+实秒、换表后再导入），索引双窗口结果与“全表逐行暴力参考实现”的相交集合和裁剪秒数**完全一致**。
3. **20 万行规模**（`/tmp/verify_scale.py`）：批量导入 200000 行约 21s；冲突查询计划
   `SEARCH exposures USING COVERING INDEX exposures_utc_leap60 (utc_sod=? AND utc_day=?)`，
   含 2 条冲突时拒绝换表约 **0.9–1 ms**；无冲突换表（仅插版本）约 **4–7 ms**、`rewritten=0`；
   `day_report`（2016-12-31，41 行相交）约 **4 ms**。
   - 过程中发现并修复了一个真实性能坑：初版复合索引以 `start_source` 开头，规划器把日窗口查询误选成该索引的单前导列扫描，20 万行时每次日取数约 325ms；改为 `utc_sod` 前导的专用覆盖索引并对三条语句加 `INDEXED BY` 后降到 4ms。
4. **真实基线旧库升级**：用 `/tmp/orig` 中的**原始代码**建库，导入 4 行并执行旧版换表（返回 `updated_text_rows: 3`，确实重写并把所有行 `leap_version` 改成 2）。再用新代码原地打开：数据/批次/raw 全保留，冻结的 `start_real` 不变；日报告仍为 86401 秒、裁剪 `{NOON:60, REAL:5, CROSS:61, OLD60:1}`；迁移后的索引查到 `OLD-60` 冲突并拒绝删 2016；另一次成功换表前后 `(exposure_id,start_real,leap_version)` 完全相同。
5. **gunicorn 实启 + HTTP 全流程**：用与镜像相同的入口
   `gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 --timeout 60 wsgi:app`
   启动，`python scripts/web_demo.py` → **17/17 PASS**：导入、换表前页面 `是（86401 秒）` 与 `23:59:60`、CSV 中 CROSS=61/LAST=2；删 2015+2016 的表被拒并列出运行号 C60、旧表仍 86401；只删 2016 的表成功（v2、重写行数 0）；换表后页面 `否（86400 秒）`、CSV CROSS=60/LAST=1、UTC 原标签保留。
   - 另实测：`/healthz` 200、`/` 302→`/day`、`/static/style.css` 200、CSV 响应含 `Content-Disposition: attachment; filename="exposures_2016-12-31.csv"`；multipart 文件上传 `demo/leap_night_2016.csv` 成功写入 4 行；闰秒表非法/重复内容渲染 200 错误页；`/leap?review_date=...` 与各 GET 页正常。
6. **交付文件校验**：`docker-compose.yml` 经 PyYAML 解析通过（服务/端口/卷/重启策略正确）；`Dockerfile` 经 `dockerfile-parse` 解析通过（python:3.11-slim、非 root、HEALTHCHECK、gunicorn 单 worker 多线程、新增 scripts 拷贝）。

## 与需求条目的对照

- “只用索引查拟删闰秒日中 UTC 起点为 23:59:60 的曝光，冲突列曝光号”：覆盖索引 + `LeapSwapRejected.offending`/网页列表，已测。
- “换表失败沿用原表”：事务回滚，版本号不变，已测（原有用例 + HTTP）。
- “新表启用原样保留曝光行和时间索引”：成功换表零 exposure 写，整行快照与 `sqlite_master` 索引定义前后一致，已测。
- “UTC 文本保留原 UTC 时刻 / 实秒保留原绝对时刻”：双坐标 + 读时按当前表解释，已测。
- “按日查看和 CSV 按索引读起点窗口、左闭右开裁剪”：双 `INDEXED BY` 窗口 + 严格相交/裁剪，原有边界用例 + fuzz + CSV 实测。
- “同一已提交状态的表与曝光；导入按提交时表整批校验”：单读事务快照；导入仍在 `BEGIN IMMEDIATE` 内取表并整批校验，原有用例与并发用例通过。
- “旧 SQLite 库原地升级，批次与 CSV 原值保留”：真实基线库升级实测 + 专用迁移测试。
- “可启动的 Docker Compose + 网页演示前后日明细”：compose/Dockerfile 校验通过；网页与 HTTP 演示已在等价 gunicorn 启动下完整走通。

## 待完成事项 / 已知限制

1. **本机没有 docker / podman 守护进程或 CLI**（已确认 `/var/run/docker.sock` 不存在），因此 `docker compose up --build` 未能在本环境真实构建运行；已用同一 `wsgi:app` 与镜像内相同的 gunicorn 参数在本机实际启动并跑通全部 HTTP 关键操作，compose YAML 与 Dockerfile 也通过解析器校验。首次在有 Docker 的机器上请实际 `docker compose up --build` 复核镜像构建、命名卷权限（uid 10001）与 HEALTHCHECK。
2. **未做浏览器像素级/JS 点击验证**：本环境无浏览器；“网页演示”通过真实 HTTP 抓取渲染后 HTML 与表单 POST 验证，快捷填充 JS 仅为辅助，核心流程不依赖 JS。
3. **gunicorn 固定单 worker**：多 worker 各持进程内写锁，跨进程只剩 SQLite `BEGIN IMMEDIATE`/`busy_timeout` 保证原子；建议保持单 worker，多 worker 需外部锁。
4. **闰秒表仍只支持正闰秒日**：负闰秒无标准民用表示，未实现。
5. **UTC 文本不解析时区偏移**：带 `+08:00` 的输入仍被拒，只接受裸 UTC/`Z`。
6. **曝光号仍为全局唯一主键**：不支持“同号不同望远镜”的复合唯一。
7. **未做认证/CSRF**：默认内网使用，对外前需补登录与 CSRF token。
8. **迁移回填是一次性 O(N)**：仅发生在首次用新代码打开旧库时；日常换表已是 O(版本插入 + 冲突索引查询)。20 万行规模下回填很快（本次未单独计时），超大旧库首次升级仍建议先备份。
9. **冲突领域结果不再截断**（网页只显示前 500 条但给出总数）；若单次候选导致数十万冲突，错误页本身会较大，必要时可加分页。
10. 未加操作者审计信息（批次/闰秒版本有时间戳与来源名）。
