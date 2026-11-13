# 取水井水表读数 → 耗水区间服务

将取水井水表的**累计读数**转换为漏损分析用的**相邻耗水区间**。
FastAPI + PostgreSQL，双 Web 进程共用一个数据库，支持补录/更正、
整批单调性校验与并发安全提交，提供可实际启动的 Docker Compose 环境。

## 1. 需求与实现对照

| 需求 | 实现 |
| --- | --- |
| 上传含 `meter_id, ts_utc, cumulative` 的 CSV，每批 ≤ 1000 行 | `app/logic.py::parse_csv`，超限返回 400；时间必须为带时区的 UTC（`Z`/`+00:00`），累计量为非负整数（int64 范围） |
| 同表同刻视为同一读数，可补录或更正 | 主键 `(meter_id, ts_utc)` + `INSERT ... ON CONFLICT DO UPDATE`；同一 CSV 内重复同刻以后出现的行为准 |
| 按同表时间相邻读数持久化区间起止与差值，首条无区间 | `meter_interval` 表，窗口函数 `lead()` 由完整有序序列生成，delta = 后累计 − 前累计 |
| 补录/更正须替换受影响的旧区间 | 提交事务内对**受影响水表**删除全部旧区间并按最终序列整体重算；其他水表不动 |
| 累计量不得随时间倒退，按整批最终序列判断 | 锁内把本批与该表完整历史合并为最终序列后逐对相邻比较（相等允许） |
| 冲突整批拒绝并展示相邻冲突读数 | HTTP 409，响应含每对冲突的前后读数（水表、两个 UTC 时刻、两个累计量）；事务回滚，全不改 |
| 两个共库 Web 进程，检查/计算期间同表其他上传仍能提交 | 准备阶段（解析、合并、校验）不持任何数据库锁；只有最终提交时短暂持有按水表划分的事务级咨询锁 |
| 提交须符合当时完整序列，不能让分别合法的并发补录合成倒退 | `pg_advisory_xact_lock` 按 `meter_id` 全局排序加锁，锁内重读权威序列复核；后提交者发现倒退即整批拒绝 |
| 原读数与区间一起生效，失败全不改 | 读数 upsert 与区间重算在**同一事务**内，任何错误整体回滚 |
| 查询不能混用修改前后状态 | 查询走 `REPEATABLE READ` 只读事务，读数与区间取自同一快照 |
| 重启保留 | PostgreSQL 命名卷 `pgdata` 持久化 |
| 浏览器可上传、查原值及区间 | 根路径 `/` 单页界面（上传、暂停控制、读数/区间查询） |
| 可暂停计算的交错补录样例 | `scripts/interleave_demo.py` + 管理端点暂停门闩（见第 5 节） |

## 2. 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 路由：上传、查询、暂停管理、页面
│   ├── logic.py           # 纯领域逻辑：CSV 解析、合并、冲突检测、区间生成
│   ├── db.py              # 连接池、建表、咨询锁事务提交、一致性查询
│   ├── blocker.py         # 演示用进程内“提交前暂停”门闩
│   ├── config.py          # 环境变量配置
│   ├── web/index.html     # 浏览器界面
│   └── tests/             # 纯逻辑单元测试 + 并发算法模拟测试（无需数据库）
├── samples/               # 演示与手工测试用 CSV
├── scripts/interleave_demo.py   # 交错补录端到端演示（仅用标准库）
├── Dockerfile
├── docker-compose.yml     # db + web1(8001) + web2(8002)
└── requirements.txt
```

## 3. 启动

```bash
docker compose up --build
```

- 浏览器界面：<http://localhost:8001/> 或 <http://localhost:8002/>
- OpenAPI 文档：<http://localhost:8001/docs>
- 健康检查（可看到两个不同的进程 pid/主机名）：`GET /healthz`
- PostgreSQL：`localhost:5432`，库/用户/密码均为 `water`

停止并清空数据：

```bash
docker compose down -v
```

## 4. CSV 格式与 API

### CSV（UTF-8，表头固定，顺序固定）

```csv
meter_id,ts_utc,cumulative
MW-001,2026-01-01T00:00:00Z,100
MW-001,2026-01-01T01:00:00Z,112
```

- `ts_utc`：ISO-8601 且必须是 UTC（`Z` 或 `+00:00`），带其他偏移/不带时区一律拒绝；
- `cumulative`：十进制非负整数，拒绝负数、小数、科学计数法；
- 数据行 1～1000 行，可跨多个水表；空文件/错表头返回 400。

### 上传

```bash
curl -F 'file=@samples/01_baseline.csv' http://localhost:8001/api/readings/batch
# 也可直接发 text/csv 原始请求体
curl --data-binary @samples/01_baseline.csv \
     -H 'Content-Type: text/csv' http://localhost:8001/api/readings/batch
```

成功（200）返回每个受影响水表的读数/区间总数。倒退（409）示例：

```json
{
  "status": "rejected",
  "message": "cumulative regression detected on the final per-meter sequence; ...",
  "batch": {"rows": 1, "meters": ["MW-001"], "meter_count": 1},
  "conflicts": [
    {
      "meter_id": "MW-001",
      "earlier": {"meter_id": "MW-001", "ts_utc": "2026-01-01T00:30:00Z", "cumulative": 110},
      "later":   {"meter_id": "MW-001", "ts_utc": "2026-01-01T00:45:00Z", "cumulative": 105},
      "expected": "later cumulative >= earlier cumulative"
    }
  ]
}
```

### 查询（同一 REPEATABLE READ 快照，读数与区间不会跨状态混用）

```bash
curl http://localhost:8002/api/meters                 # 所有水表概览
curl http://localhost:8002/api/meters/MW-001          # 某表全部读数+区间
curl 'http://localhost:8002/api/meters/MW-001?limit=20'
```

## 5. 可暂停计算的交错补录演示

### 场景

基线序列（`MW-001`）：`00:00=100, 01:00=112, 02:00=125, 03:00=140`

| 补录 | 内容 | 单独判断 |
| --- | --- | --- |
| A | `00:30 = 110` | 合法（100 ≤ 110 ≤ 112） |
| B | `00:45 = 105` | 合法（100 ≤ 105 ≤ 112） |

两者同时生效时出现 `110(00:30) → 105(00:45)` 的倒退——**分别合法，合起来非法**。

### 一键脚本（推荐）

```bash
docker compose up --build -d
# 等健康检查通过后：
python3 scripts/interleave_demo.py
```

脚本流程：

1. 向 web1 提交基线批次；
2. 在 web1 预置暂停门闩，然后发起带 `X-Pause-Before-Commit: MW-001` 的补录 A，
   请求停在“准备完成、拿锁提交之前”（不占数据库连接、不持任何锁）；
3. 同时向 **web2** 提交补录 B，立即成功；
4. 放行 A：A 在咨询锁内重读权威完整序列复核，检测到倒退，**整批拒绝、全不改**；
5. 查询最终读数与区间，断言 A 未生效、B 生效、所有 delta 非负。

### 手工/浏览器复现

```bash
# 终端 1：在 web1  Arm 门闩
curl -X POST 'http://localhost:8001/api/admin/blocker/arm?key=MW-001'
# 终端 1：发起 A（请求会挂起）
curl -H 'X-Pause-Before-Commit: MW-001' --data-binary @samples/02_backfill_a_legal_alone.csv \
     -H 'Content-Type: text/csv' http://localhost:8001/api/readings/batch
# 终端 2：并发提交 B，立即 200
curl --data-binary @samples/03_backfill_b_legal_alone.csv \
     -H 'Content-Type: text/csv' http://localhost:8002/api/readings/batch
# 终端 3：放行 A，A 返回 409 并附带相邻冲突读数
curl -X POST 'http://localhost:8001/api/admin/blocker/release?key=MW-001'
```

浏览器界面第 ② 区提供同样的 Arm / Release / 状态查看按钮。
暂停功能仅用于演示：门闩是**进程内**状态（两进程不共享内存，这正是可以把
请求卡在单机提交窗口的原因），生产部署设置 `BLOCKER_ENABLED=0` 即完全关闭，
相关请求头会被忽略/拒绝，不影响任何正确性逻辑。

### 其他样例

| 文件 | 含义 | 预期 |
| --- | --- | --- |
| `01_baseline.csv` | 两表基线批次 | 200，生成区间 |
| `02/03_*.csv` | 交错补录 A/B | 见上 |
| `04_correction_legal.csv` | 同刻更正 `02:00` 125→131 | 200，受影响区间被替换重算 |
| `05_regression_rejected.csv` | 插入 `02:30=99` | 409，整批拒绝 |
| `06_batch_regression.csv` | 批次内部就倒退（10→9） | 409，整批拒绝 |

## 6. 并发正确性说明

1. **准备无锁**：CSV 解析、序列合并、初步校验在应用线程内完成，不持有数据库锁，
   同一水表的其他上传在此期间仍可提交。
2. **提交串行化（按水表）**：提交事务先按 `meter_id` 全局排序申请
   `pg_advisory_xact_lock`（CRC32 稳定哈希，命名空间固定；统一加锁顺序杜绝死锁）。
   不同水表互不阻塞，同一水表的提交在全库范围串行——包括两个 web 进程之间。
3. **锁内权威复核**：拿锁后重读该表完整序列，与本批合并得到“提交那一刻”的最终序列，
   再做单调性判断。任何先提交的并发补录都已可见，因此后提交者不可能基于过期状态写入倒退。
4. **原子性**：读数 upsert、旧区间删除、新区间插入在同一事务内；冲突即回滚，失败全不改。
5. **一致性读**：查询接口使用 `REPEATABLE READ` 快照，读数与区间要么全是旧状态、
   要么全是新状态。

## 7. 本地测试（无需数据库、无需第三方依赖）

```bash
# 纯逻辑 + 并发算法模拟，共 17 个用例
python3 -I -c "
import sys, unittest
sys.path.insert(0, '.')
suite = unittest.TestLoader().discover('app/tests')
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)"
```

- `app/tests/test_logic.py`：CSV 解析边界、同刻覆盖、UTC 校验、行数上限、
  合并/冲突/区间生成；
- `app/tests/test_concurrency_sim.py`：用内存锁复刻数据库提交算法，
  多线程验证交错补录下“分别合法、合起来倒退”的后发批次被整批拒绝、失败全不改。
