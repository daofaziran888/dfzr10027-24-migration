# 分析工坊 · 数据分析与数据应用平台

面向指标口径管理、多维度聚合查询、用户分群圈选、AB实验评估、时序趋势分析与数据服务发布的数据分析与数据应用平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标定义 | `metric_def` | 业务指标 | 指标编号、指标名称、计算公式 |
| 维度注册 | `dim_registry` | 分析维度 | 维度编号、维度名称、业务键 |
| 聚合任务 | `agg_task` | 聚合计算 | 任务编号、目标指标、聚合维度 |
| 多维立方体 | `cube_builder` | 数据立方体 | 立方体编号、事实来源、维度列表 |
| 分群规则 | `cohort_rule` | 用户分群 | 分群编号、分群名称、筛选条件 |
| 实验管理 | `experiment` | AB实验 | 实验编号、实验假设、分组方案 |
| 时序分析 | `trend_series` | 时序序列 | 序列编号、观测指标、时间粒度 |
| 归因模型 | `attribution` | 归因链路 | 链路编号、归因窗口、触点类型 |
| 数据分层 | `segmentation` | 数据切片 | 切片编号、源数据集、切分维度 |
| 报表生成 | `report_gen` | 分析报表 | 报表编号、报表类型、数据范围 |
| 预测分析 | `forecast` | 预测方案 | 方案编号、预测指标、预测算法 |
| 数据集市 | `data_mart` | 分析主题 | 主题编号、主题名称、业务域 |
| 集市存储 | `data_mart_storage` | 迁移批次/统计记录/复盘报表 | 行类型、读端模式、批次校验和 |
| 统计建模 | `stat_model` | 统计模型 | 模型编号、模型类型、因变量 |
| 数据提取 | `data_extract` | 提取任务 | 任务编号、来源数据集、筛选条件 |
| 质量监控 | `quality_mon` | 监控规则 | 规则编号、监控对象、校验类型 |
| 对比分析 | `compare_job` | 对比任务 | 任务编号、基准数据、对比数据 |
| 数据服务 | `api_service` | 服务接口 | 接口编号、查询数据集、请求参数 |
| 调度编排 | `schedule_plan` | 调度计划 | 计划编号、关联任务、Cron表达式 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 集市存储上线（主题目录 / 统计记录分离）

旧实现把带数字 `id` 的主题配置行与实验管道写入的无编号统计行混在 `data_mart`
同一张列表里。上线后按行类型分流，写入端与读取端共用
`app/services/mart_storage.py` 这一可区分行类型的来源：

- `row_type=topic`：主题配置行，保留数字 `id` 与全部元数据，存主题目录
  （新结构 `_mart_topics`，旧结构仍在 `data_mart`）。
- `row_type=experiment_stat`：实验统计行，无数字编号，按
  `实验编号 + 业务日期 + region + channel` 追溯，存统计记录表
  （新结构 `_mart_statistics`），重复回填按业务键幂等 upsert，不会多一份。
- 读端模式可在 `legacy`（旧混合列表）与 `new`（新结构）间切换；回滚只切读端，
  不销毁新结构数据，可再次提交迁移。
- 每次迁移生成一个批次，批次内含主题数据快照与 sha256 校验和，可核对迁移结果。
- 复盘报表按相对提升降序给出可复算排序，冻结当时的统计记录并记录来源引用，
  回滚后仍能打开关联统计，核对样本日期与旧主题编号（`DATA-STAT-<实验编号>`）。

主要接口（均挂在 `/api/data_mart` 下，注册顺序先于 `/{entry_id}`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/storage/state` | 读端模式与行类型、实验、日期构成 |
| POST | `/storage/migrate` | 提交迁移批次（可按 `experiments` 限定范围） |
| POST | `/storage/rollback` | 回滚读端到旧结构，新结构数据保留 |
| GET | `/storage/migrations[/{batch_no}]` | 迁移批次与快照校验和 |
| GET | `/topics` | 主题目录（仅配置行） |
| GET | `/statistics` | 统计记录（仅实验统计行，可按实验过滤） |
| POST | `/statistics/legacy-ingest` | 旧结构时期的统计行写入入口 |
| POST | `/statistics/correct` | 新结构统计更正并重算提升与显著性 |
| POST/GET | `/reviews[/{report_no}/statistics]` | 复盘报表及其关联实验统计 |

领域：数据工程
子方向：数据分析与数据应用
