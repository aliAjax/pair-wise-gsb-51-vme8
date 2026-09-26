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
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/records/{id}/reviews`：持续复评列表，按时间顺序返回每次复评、建议与复核结果。
- `POST /api/records/{id}/reviews`：服务人员（`servicer`）录入一次复评，`data`为`{"avg_monthly_income":..,"monthly_expenses":..,"new_debt_payment":..,"note":"可选"}`。
- `POST /api/records/{id}/reviews/{rid}/decision`：复核岗（`reviewer`）确认复评，`data`为`{"decision":"adopt|keep","review_note":"可选"}`。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 持续复评

方案生效（`active`）后，服务人员录入借款人最近三个月月均收入、家庭支出和新增债务月供：

- 承受比例 = 批准月供（`approved_payment`）÷ 最近三个月月均收入；同时计算可支配余额 = 收入 − 家庭支出 − 新增债务月供。
- 本次承受比例 **低于 25%**：建议退出纾困（`exit`）。
- 本次与上一次复评 **连续两次高于 40%**：建议展期（`extend`）；中间一次回落到阈值内则重新计数。
- 其余情况：继续按原方案履约（`none`）。
- 复评提交后为`pending`，仅记录复评数据，**不改变方案状态和版本，原方案照常履约**；复核岗确认（采纳建议/维持原方案）后变为`confirmed`。
- 每次复评和复核都写入审计时间线，数据落 SQLite，服务重启后仍可按时间查看。演示页支持一键创建激活记录、录入复评与复核确认。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
