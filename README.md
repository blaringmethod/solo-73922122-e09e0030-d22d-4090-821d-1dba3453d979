# 巡天曝光 UTC 台账

按 UTC 日期领取曝光明细，用于对齐多台望远镜的观测。Flask 网页取数，SQLite 保存曝光 CSV 原值；正闰秒日按 86401 秒计算，区间一律左闭右开。

换闰秒表**不再重写全库曝光**：成功换表只新增一个闰秒表版本，曝光行与时间索引原样保留。UTC 文本起点保留原 UTC 时刻（取数时按当前表解读），实秒起点保留原绝对时刻。

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:8000>。数据保存在命名卷 `ledger-data`（容器内 `/data/ledger.sqlite3`），重启不丢。

不使用 Docker 时：

```bash
python3 -m pip install -r requirements.txt
LEDGER_DB=./var/ledger.sqlite3 gunicorn --bind 127.0.0.1:8000 --workers 1 --threads 8 wsgi:app
```

测试：`python3 -m pytest -q`（需要 `pip install pytest`）。

## 网页演示换表前后的同一天

1. 打开 <http://localhost:8000/import>，点“填入 2016 跨年夜演示数据”（可再点“追加一条 23:59:60 冲突演示”），提交导入。
2. 到“取数”，日期选 `2016-12-31`：当天 86401 秒，`DEMO-CROSS`（23:59:00+120s）在当天占 61 秒，`DEMO-LAST`（23:59:59+2s）占 2 秒。
3. 点“换表并对比这一天”，在闰秒表页：
   - 若提交删除 `2015-06-30` 的表（如只留 `2012-06-30`），索引查到 `DEMO-C60-2015` 冲突，页面列出曝光号并显示“换表未生效，原表保留”。
   - 点“保留至 2015（删除 2016-12-31）”并提交：换表成功，提示重写行数为 0。
4. 点“查看换表后 2016-12-31 日明细”：同一天变为 86400 秒，`DEMO-CROSS` 裁剪为 60 秒、`DEMO-LAST` 为 1 秒；UTC 文本行的原时刻不变，实秒行的绝对 `start_real` 不变（其 UTC 渲染随表平移）。

也可用脚本对运行中的服务做同一套 HTTP 校验（标准库，无需额外依赖）：

```bash
python scripts/web_demo.py http://127.0.0.1:8000
# 容器启动后也可：docker compose exec ledger python scripts/web_demo.py
```

`demo/leap_night_2016.csv` 可在导入页直接上传。

## 页面

- **取数**：选 UTC 日期，查看与该日相交的曝光，显示当天 UTC 起止时刻、实秒范围与每条曝光的裁剪后起止时刻和实秒数；标注起点是 UTC 文本还是实秒；可下载当天 CSV。
- **导入**：上传或粘贴 CSV，整批校验，要么全写入要么全回滚。提供示例 CSV 与一键演示数据。
- **闰秒表**：查看/替换生效中的正闰秒日期表，附版本历史、冲突曝光号列表与“换表后回看同一天”入口。

页面：`/`（取数）、`/import`、`/leap`、`/day?date=YYYY-MM-DD`、`/day.csv?date=YYYY-MM-DD`、`/healthz`。

## CSV 格式

表头固定为：

```csv
telescope,exposure_id,start,duration_s
T1,EXP-0001,2026-11-24 00:00:00,600
T2,EXP-0002,2016-12-31 23:59:60,1
T3,EXP-0003,846201600,900
```

- `telescope`：望远镜号，1～64 字符。
- `exposure_id`：唯一曝光号，1～128 字符；批内重复或已入库即整批拒绝。
- `start`：二选一
  - UTC 文本 `YYYY-MM-DD HH:MM:SS`（也接受 `T` 分隔、结尾 `Z`）；`23:59:60` 只能出现在当前闰秒表中的闰秒日的 23:59:60。
  - 从 2000-01-01 00:00:00 UTC 起累计的整数实秒（≥0，日期不超过 9999-12-30）。
- `duration_s`：整数 1～3600。

导入按提交那一刻的生效闰秒表在 `BEGIN IMMEDIATE` 事务内整批校验。CSV 原值整行保存在 `exposures.raw_json`，原始 `start` 词元另存 `start_raw`。

## 时间与索引设计

- 实秒 = 从 2000-01-01 00:00:00 UTC 起经过的绝对秒数（相当于把闰秒计入时间轴）。
- 正闰秒日 00:00:00 起共 86401 实秒，最后一秒记为 `23:59:60`；该日区间为 `[当天 00:00:00, 次日 00:00:00)`。
- 每行曝光同时保存三套起点坐标：
  - `start_real`：导入时按当时表算出的绝对实秒；实秒报送行的权威坐标，**换表后也绝不更新**。
  - `utc_day / utc_sod / utc_key`：与闰秒表无关的 UTC 标签键。每个民用日占 86401 个键槽，`23:59:60` 为槽 86400，因此标签唯一且按时间有序；UTC 文本报送行的权威坐标。
  - `start_raw` / `raw_json`：原始词元与整行 CSV 原值。
- 按日取数在同一读事务、同一闰秒表快照内走两条索引窗口（`INDEXED BY` 固定）：
  - UTC 文本行：部分索引 `exposures_utc_key(utc_key, exposure_id) WHERE start_source='utc'`，读 `[前一日 23:00:00 键, 次日 00:00:00 键)`；
  - 实秒行：`exposures_start_real_id(start_real, exposure_id)`，读 `start_real ∈ [当天起点 − 3600, 当天终点)`。

  随后按当前表计算各行的有效绝对起点，用严格条件 `start < 当天终点 且 start + duration_s > 当天起点` 判定相交，并按左闭右开裁剪。最大时长 3600 秒与正闰秒只增秒的性质保证 UTC 标签窗口不会漏行。
- 每次取数只加载一个闰秒表版本（同一读事务中读取版本、边界、两条索引窗口、筛选与格式化），换表并发时不会混用两张表或两份曝光状态。

## 换表规则（不重写全库）

- 提交内容整批校验（格式、日期合法、重复），任一行不合格整批拒绝。
- 事务内取旧表与候选表的差集，得到“拟删除的闰秒日”。唯一会失效的已存时刻是这些日期上的 UTC `23:59:60`：通过覆盖索引
  `exposures_utc_leap60(utc_sod, utc_day, exposure_id, start_source)`
  （执行计划为 `SEARCH ... USING COVERING INDEX`）查询，不再逐行 Python 重解析全库。
- 若有冲突，换表失败，异常 `LeapSwapRejected.offending` 与网页列出曝光号/望远镜/原开始值/原因，旧表继续生效。
- 无冲突且两表不同：只向 `leap_tables` 插入一个新版本，**不执行任何 `UPDATE/DELETE exposures`，不重建索引**；返回 `rewritten_rows: 0`。相同表提交为 no-op。
- 取数语义：UTC 文本行始终从 `start_raw` 按当前生效表重算有效实秒（保留原 UTC 时刻）；实秒行始终使用冻结的 `start_real`（保留原绝对时刻，其 UTC 渲染可能随表平移）。

## 旧库原地升级

用旧版本（换表会重算 `start_real` 的 schema）创建的 SQLite 文件可直接用新代码打开：

- `CREATE TABLE IF NOT EXISTS` 保留原表与全部行；
- 自动 `ALTER TABLE exposures ADD COLUMN utc_day/utc_sod/utc_key`（幂等），并一次性回填这些稳定键；
- 新建两个 UTC 索引；原有 `exposures_start_real(_id)`、`exposures_batch` 索引原样保留；
- `import_batches`、`exposures.raw_json/start_raw` 等原值原样保留，历史批次与 CSV 原值不丢；
- 升级本身只补新列/索引，不改写 `start_real`。

## 并发

- 写操作（导入、换表）串行化：进程内锁 + SQLite `BEGIN IMMEDIATE`，全批原子提交。换表事务现在只有一次版本插入和索引查询，补录几乎不用等待换表。
- 读操作使用 WAL 快照，可在导入/换表进行中继续取数。
- 交付使用 gunicorn 单 worker 多线程；SQLite 文件放在单卷上，避免多 worker 争锁。

## 已知限制

见 `NOTES.md` 的“待完成事项”。
