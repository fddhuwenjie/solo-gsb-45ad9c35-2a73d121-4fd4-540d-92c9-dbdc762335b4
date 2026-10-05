# 电缆隧道接头巡检复核台

基于 **Flask + SQLite + 原生 JavaScript** 的电缆隧道接头巡检数据复核系统。
巡检员录入线路里程、接头、巡检批次、负荷区间、测温距离、温差读数、设备校准版本，
以及复测读数与人工复核意见；系统按**同一接头 + 同一批次**对齐观测并生成风险结论与处置清单。

## 一条命令启动

```bash
docker compose up --build -d
```

启动后访问：**http://localhost:8000**

> 首次启动自动建表并播种演示数据（含可比较 / 证据不足 / 相互矛盾 / 更正留痕 / 高风险已复核 /
> 高风险复测已过期 等示例；“2026-10 二轮”批次的 `110kV 西联络线 K03+760 J-03` 即复测过期阻断示例）。
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
- **封存门槛**：批次内每个高风险接头都必须具备**覆盖最新证据的当前复测读数**或**覆盖最新证据的明确人工复核意见**，否则整份报告拒绝封存（HTTP 400，沿用现有 `{"error": ...}` 响应、不生成任何报告），并在“批次复核”页列出全部阻断原因。

#### 复测 / 复核意见“覆盖最新证据”的判定规则

判定对象是**同一接头、同一批次**，且一律按记录上的观测/复核时间 `recorded_at` 与**更正链**计算，
与该条记录何时录入（提交）数据库无关：

1. **最新证据时间** = 当前有效观测（`superseded=0`）中最大的 `recorded_at`。
2. **复测读数**只有同时满足以下条件才算“当前复测、覆盖最新证据”：
   - 本身未被更正（未处于更正链中被取代的一环；`superseded=1` 的复测**不算当前证据**）；
   - 其 `recorded_at` **不早于**最新证据时间。
   - 若仍有效但时间早于最新有效观测 → **复测已过期**，封存阻断并要求“补充覆盖最新证据的复测”；
   - 若复测已被更正 → 明确阻断“原复测已被更正，不属于当前证据”。
3. **人工复核意见**同理：最近一条意见的 `recorded_at` 不早于最新证据时间才算覆盖；更早的意见标记为“已过期”。
4. **较早时间的回填不算新证据**：后补录入一条 `recorded_at` 更早的普通观测（仅因提交较晚），**不会**使时间更晚的复测/复核意见过期。
5. 阻断时，该接头在批次总览中给出阻断原因，且其**当前结论的原因、处置清单（补复测 / 补明确意见）与复测状态保持一致**；
   后续补上覆盖新证据的复测读数或明确人工复核意见后，门槛才解除、方可封存。
6. 被更正记录、更正链、失效的历史结论与**已封存快照**均原样保留；封存后的任何补录/更正都不改变既有报告。

> 录入页与复核页提供可选的“记录时间”输入（留空取服务器当前时间），补录历史观测/意见时填写，
> 系统据此区分“更晚的新证据”与“较早时间的回填”。

### 4a. 判定规则的验证步骤

以下 `curl` 在全新播种库上演示（也可直接在页面上操作，二轮批次已内置一个“复测过期”示例
`110kV 西联络线 K03+760 J-03`，打开 `#/batch/2` 即可看到阻断）。

```bash
# 1) 高风险初测 + 复测（记录时间 t1、t2），此时满足门槛
curl -s localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id":1,"mileage":"K80+001","joint_no":"J-V","batch_id":1,
  "load_range":"300-380A","measure_distance_m":5.0,"temp_diff_k":9.4,
  "calib_version":"CAL-2026.07","inspector":"测试员","recorded_at":"2026-09-18T01:00:00Z"}'
curl -s localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id":1,"mileage":"K80+001","joint_no":"J-V","batch_id":1,
  "load_range":"300-380A","measure_distance_m":5.0,"temp_diff_k":9.1,
  "calib_version":"CAL-2026.07","inspector":"测试员","is_retest":1,
  "recorded_at":"2026-09-18T03:00:00Z"}'

# 2) 补录“记录时间更晚”的普通观测 → 复测过期：总览 sealable=false、blockers 列出原因
curl -s localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id":1,"mileage":"K80+001","joint_no":"J-V","batch_id":1,
  "load_range":"300-380A","measure_distance_m":5.0,"temp_diff_k":3.0,
  "calib_version":"CAL-2026.07","inspector":"测试员","recorded_at":"2026-09-19T08:00:00Z"}'
curl -s localhost:8000/api/batches/1 | python3 -m json.tool   # 关注 blockers / 关节 retest_status=stale

# 3) 封存被拒绝：400 + {error:...}，且不产生报告
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/api/batches/1/seal   # 400

# 4) 补上覆盖新证据的复测（recorded_at 不早于最新观测）→ 门槛解除，可封存
curl -s localhost:8000/api/observations -H 'Content-Type: application/json' -d '{
  "line_id":1,"mileage":"K80+001","joint_no":"J-V","batch_id":1,
  "load_range":"300-380A","measure_distance_m":5.0,"temp_diff_k":8.6,
  "calib_version":"CAL-2026.07","inspector":"测试员","is_retest":1,
  "recorded_at":"2026-09-19T10:00:00Z"}'
curl -s -X POST localhost:8000/api/batches/1/seal                                # {"report_id":..,"version":..}
```

另两类边界可同样验证：补录 `recorded_at` **更早**的普通观测不会让复测过期（`retest_status` 仍为 `current`）；
用“更正已有读数”把复测本身更正掉后，阻断原因变为“原复测已被更正，不属于当前证据”。
人工复核意见过期/覆盖的验证见 `POST /api/reviews`（同样支持 `recorded_at`）。


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
