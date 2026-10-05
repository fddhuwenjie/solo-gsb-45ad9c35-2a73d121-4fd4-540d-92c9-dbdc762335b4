# 电缆隧道接头巡检复核台

基于 **Flask + SQLite + 原生 JavaScript** 的电缆隧道接头巡检数据复核系统。
巡检员录入线路里程、接头、巡检批次、负荷区间、测温距离、温差读数、设备校准版本，
以及复测读数与人工复核意见；系统按**同一接头 + 同一批次**对齐观测并生成风险结论与处置清单。

## 一条命令启动

```bash
docker compose up --build -d
```

启动后访问：**http://localhost:8000**

> 首次启动自动建表并播种演示数据（含可比较 / 证据不足 / 相互矛盾 / 更正留痕 / 高风险已复核 等示例）。
> SQLite 数据保存在命名卷 `inspection-data`（容器内 `/data/inspection.db`），停止重建容器不丢数据。

停止：`docker compose down`（加 `-v` 会同时删除数据卷）。

### 不使用 Docker 的本地运行

```bash
pip install -r requirements.txt
python wsgi.py        # 开发模式，默认库文件 /data/inspection.db
# 或指定库路径：
DATABASE_PATH=./inspection.db python wsgi.py
```

## 核心业务规则

### 1. 同接头同批次对齐，三档证据状态

- **可比较**：测温距离（差 ≤ 1m）、负荷区间有交集、设备校准版本一致，且多条读数温差极差 ≤ 3K，可相互印证。
- **证据不足**：只有一条有效读数；或距离差 > 1m、校准版本不同、负荷区间无交集导致观测条件无法对齐（原因逐条列明）。
- **相互矛盾**：条件可对齐，但温差极差 > 3K，读数不能相互印证。

### 2. 风险结论（带原因 + 处置清单）

| 风险 | 判定 |
|---|---|
| 高 | 读数相互矛盾；或最大温差 ≥ 8K（证据不足时降为中风险） |
| 中 | 最大温差 ≥ 4K |
| 低 | 其余 |

每条结论包含人类可读原因（引用了哪些观测、因何不可比/矛盾）和处置清单
（复测、人工复核、设备校准核查、限负荷盯守、趋势跟踪、常规巡检等）。

### 3. 原始记录不可覆盖

`observations` / `reviews` / `conclusions` / `reports` 全部只追加：

- **更正**：写入新记录并以 `corrects_id` 指向原记录；原记录仅置 `superseded` 标记，内容永不修改。
- **补录 / 新增复核**：只令该**受影响接头**的当前结论 `invalidated=1` 并写明失效原因，再追加一条新结论；其他接头不受影响，历史结论完整可追溯。

### 4. 封存报告固定批次快照

- 封存即生成整批次 JSON 快照（全部原始观测、复核、结论版本历史），此后任何更正/补录都**不会改变**已封存内容；同一批次可封存出 v1、v2… 新版本。
- **封存门槛**：批次内每个**当前结论为高风险**的接头，都必须具备**覆盖最新证据的当前复测读数**或**覆盖最新证据的明确人工复核意见**，否则整份报告拒绝封存（HTTP 400，沿用原有错误响应、不生成报告），并在“批次复核”页列出全部阻断原因。

#### 复测 / 复核意见如何算“覆盖最新证据”

判定只依据**当前有效证据**与其 `recorded_at`（观测实际发生时间），与入库/提交时刻无关：

1. 取该接头本批次全部**未被更正**（`superseded=0`）观测，最新 `recorded_at` 记为**最新证据时间**。
2. **当前复测**：存在未被更正的复测读数，且其 `recorded_at` ≥ 最新证据时间，才算覆盖；被更正链指向的旧复测（`superseded=1`）不是当前证据，直接排除。
3. **明确人工复核意见**：最新一条复核意见的 `recorded_at` ≥ 最新证据时间，才算覆盖。
4. 因此：**较早观测时间的回填记录不会仅因提交较晚而使复测过期**（时间更早，不改变最新证据时间）；只有 `recorded_at` 更晚的后续观测（含补录）才会使原复测/意见过期。
5. 后续观测使原复测/意见过期且接头当前结论仍为高风险时：批次总览的**封存门槛列出阻断原因**，当前结论的**原因文本、处置清单（重新复测/重新复核条目）与复测/复核覆盖状态保持一致**；补录覆盖新证据的复测或明确意见后即可封存。
6. 全程只追加：原始观测、更正链、失效的历史结论、已封存报告快照均完整保留，封存后的快照永不改变。

### 5. 证据链页面

- **批次复核**：按批次查看全部接头的状态/风险/原因/处置与封存门槛。
- **线路证据链**：按线路查看每个接头在各批次的当前结论及结论失效历史（含失效原因），可下钻到接头的完整证据：原始读数、更正链、复测、人工复核和全部结论版本。

## 页面

| 路由 | 功能 |
|---|---|
| `#/entry` | 观测录入（含“更正已有读数”、复测标记）与人工复核意见 |
| `#/batch` / `#/batch/<id>` | 批次总览、一键重算、封存门槛与封存 |
| `#/line` / `#/line/<id>` | 按线路查看证据链与失效原因 |
| `#/reports` / `#/reports/<id>` | 封存报告列表与固定快照 |

## 复测覆盖判定的验证步骤

以下步骤以播种数据中的 **批次 1「2026-09 一轮」**（演示接头 J-08 高风险且复测/复核均覆盖，初始可封存）为例。
`recorded_at` 为观测实际发生时间，可显式传入以模拟“先复测、后补录更晚观测”。

### 场景 A：更晚时间的补录使旧复测过期 → 阻断封存

```bash
# 1) 初始批次1可封存
curl -s localhost:8000/api/batches/1 | python -m json.tool | grep -E 'sealable|blockers'
#   "sealable": true,  "blockers": []
curl -s -X POST localhost:8000/api/batches/1/seal    # 201，封存 v1 成功

# 2) 对高风险接头 J-08（220kV 东隧道线 K12+905）补录一条 recorded_at 更晚的普通观测
curl -s -X POST localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id": 1, "mileage": "K12+905", "joint_no": "J-08", "batch_id": 1,
  "load_range": "310-390A", "measure_distance_m": 5.0, "temp_diff_k": 9.5,
  "calib_version": "CAL-2026.07", "inspector": "验证员",
  "note": "隔日补录，时间晚于既有复测", "recorded_at": "2026-09-25T08:30:00Z"}'

# 3) 总览变为不可封存，列出阻断原因（复测与复核意见均早于 2026-09-25T08:30:00Z）
curl -s localhost:8000/api/batches/1 | python -m json.tool | grep -E 'sealable|未覆盖|过期'
# 4) 封存沿用拒绝响应（HTTP 400，不生成新报告；v1 快照仍在 /api/reports/1）
curl -s -i -X POST localhost:8000/api/batches/1/seal | head -1   # HTTP/1.1 400

# 5) 补一条覆盖新证据的复测（recorded_at 不早于最新证据时间）
curl -s -X POST localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id": 1, "mileage": "K12+905", "joint_no": "J-08", "batch_id": 1,
  "load_range": "310-390A", "measure_distance_m": 5.0, "temp_diff_k": 9.3,
  "calib_version": "CAL-2026.07", "inspector": "验证员", "is_retest": 1,
  "recorded_at": "2026-09-25T10:00:00Z"}'
curl -s -X POST localhost:8000/api/batches/1/seal    # 201，封存 v2 成功
```

### 场景 B：较早时间的回填（即使现在才提交）不使复测过期

```bash
# 对 J-08 补录一条 recorded_at 早于现有复测的观测 → 最新证据时间不变，门槛仍满足
curl -s -X POST localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id": 1, "mileage": "K12+905", "joint_no": "J-08", "batch_id": 1,
  "load_range": "310-390A", "measure_distance_m": 5.0, "temp_diff_k": 9.0,
  "calib_version": "CAL-2026.07", "inspector": "验证员",
  "note": "补录巡检当日更早的漏报读数", "recorded_at": "2026-09-18T08:00:00Z"}'
curl -s localhost:8000/api/batches/1 | python -m json.tool | grep '"sealable"'   # true
```

### 场景 C：被更正的复测不算当前证据

```bash
# 取 J-08 recorded_at 最新的有效复测观测 id（J-08 为 220kV 东隧道线 K12+905）
RID=$(curl -s localhost:8000/api/joints/2/batches/1/evidence \
  | python3 -c 'import sys,json;obs=[o for o in json.load(sys.stdin)["observations"] if o["is_retest"] and not o["superseded"]];print(sorted(obs,key=lambda o:o["recorded_at"])[-1]["id"])')
# 用 corrects_id 更正该复测：新记录是普通观测（不带复测标记），原复测仅置 superseded=1
curl -s -X POST localhost:8000/api/observations -H 'Content-Type: application/json' -d "{
  \"line_id\": 1, \"mileage\": \"K12+905\", \"joint_no\": \"J-08\", \"batch_id\": 1,
  \"load_range\": \"310-390A\", \"measure_distance_m\": 5.0, \"temp_diff_k\": 9.4,
  \"calib_version\": \"CAL-2026.07\", \"inspector\": \"验证员\",
  \"note\": \"更正原复测读数，本条为普通观测\",
  \"corrects_id\": $RID, \"recorded_at\": \"2026-09-26T08:00:00Z\"}"
# 已无有效复测，旧复核意见也早于 2026-09-26T08:00，接头仍为高风险 → 封存被拒绝（HTTP 400）
curl -s -i -X POST localhost:8000/api/batches/1/seal | head -1
```

> 三个场景可在同一演示库按 A→B→C 顺序执行；“可封存/被拒绝”的结论不依赖执行顺序，
> 仅封存版本号（v2、v3…）随已封存次数递增。

> 页面验证：打开 `#/batch/1`，“复测/复核覆盖”列显示 ✅覆盖 / ⚠已过期徽标，
> 阻断行显示 ⛔ 原因；“证据链”弹窗顶部给出最新有效观测时间与覆盖判定；
> 已封存报告在 `#/reports` 中保持封存时刻的固定快照。
> 播种数据的批次 2「2026-10 二轮」内置了 J-04“先复测、后补录更晚普通观测”的阻断示例。

## API 摘要

```
GET  /api/meta
POST /api/observations                 # 录入/更正/补录观测（corrects_id 表示更正）
POST /api/reviews                      # 人工复核意见
GET  /api/batches/:id                  # 批次总览 + 封存门槛
POST /api/batches/:id/recompute        # 重算本批结论
POST /api/batches/:id/seal             # 封存（不满足门槛返回 400 及原因）
GET  /api/reports · /api/reports/:id   # 封存报告及快照
GET  /api/lines/:id/evidence           # 线路证据链
GET  /api/joints/:jid/batches/:bid/evidence  # 接头单批次完整证据
```

## 技术结构

```
app/
  __init__.py      # Flask 应用工厂
  db.py            # SQLite 连接、幂等建表、审计日志
  evaluation.py    # 对齐/分级/风险/处置规则（阈值常量集中可调）
  services.py      # 只追加写入、结论失效与重算、封存校验、快照
  routes.py        # JSON API
  seed.py          # 演示数据
schema.sql         # 表结构
static/js/app.js   # 原生 JS 单页（hash 路由，无框架无构建）
```
