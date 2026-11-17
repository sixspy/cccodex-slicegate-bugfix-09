# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 筛选一致说明（口径钉死）

可放行集合与放行写入**同源裁决**，选定**乙处置**：筛选列表只作提示，放行写前在写事务内现场重算完整度，
未完整则零写入并追加筛选偏离流水；同一筛选条件下抢点放行排队，后手标注筛选争用。
口径全文见 [`docs/筛选一致说明.md`](docs/筛选一致说明.md)，2026-11-17 起生效，当日禁止改口。

- 筛选列表 / 切片详情 / 放行闸共用唯一裁决函数 `adjudicate()`，存储的完整度缓存列不作放行依据
- 闸口拒绝留痕：`GET /api/deviations`（筛选偏离 / 筛选口径偏了），放行流水含 `筛选争用` 标注
- 策略条可查：`GET /api/policy`；筛选参数：`GET /api/slices?biz_date=&partition_key=&completeness=&releasable=1`

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10634 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 启动（一键）

`powershell
cd projects/04-slicegate
docker compose up -d --build
`

浏览器：http://127.0.0.1:10634/  
健康检查：GET http://127.0.0.1:10634/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10601
python -m pytest -q
`
