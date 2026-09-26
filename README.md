# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，含`reviews`复评列表（按时间顺序）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/reviews`：复评列表。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `POST /api/records/{id}/reviews`：服务人员（`servicer`）录入复评，请求体为`{"data":{"monthly_income":...,"household_expenses":...,"new_debt_payment":...,"note":"..."}}`，仅`active`状态可录入。
- `POST /api/records/{id}/reviews/{review_id}/decision`：复核岗（`reviewer`）确认或驳回建议，请求体为`{"decision":"confirmed|rejected","note":"..."}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 存续期复评

方案生效（`active`）后，服务人员定期录入借款人最近三个月月均收入、家庭支出和新增债务月供，系统按批准的月供计算承受比例（批准月供/月均收入）并给出建议：

- 承受比例连续两次高于四成：建议展期；
- 承受比例低于两成五：建议退出纾困；
- 其余情况：维持原方案。收入为零时无法计算比例，按超限处理。

复评与复核均不改动方案状态和版本，复核岗确认前原方案照常履约；确认后的建议由既有业务动作流跟进执行。每次复评、建议和确认结果写入审计时间线并持久化于SQLite，服务重启后仍可查看。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、复评建议与复核、重复引用、权限拒绝和版本冲突。
