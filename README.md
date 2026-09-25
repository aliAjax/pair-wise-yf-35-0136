# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/passport.py`：生物护照读数记账、序列计算和确认后样本联动。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。
- `reading`：生物护照读数，由样本分析自动登记，不能直接创建。

## 生物护照复核

- 样本执行`analyze`时可携带`marker`（指标）、`value`（数值）、`unit`（单位），三者必须同时提供；系统自动登记一条`reading`，并按样本的`collected_at`（或显式传入的`sampled_at`）归入该运动员该指标的时间序列。
- 同一运动员同一指标从第三条读数起，若相对前两条读数平均值的偏离超过10%（`src/rules.py`中的`DEVIATION_THRESHOLD`），该读数进入`pending_review`。
- 存在`pending_review`读数时：该运动员该指标暂停接收新读数，且该运动员不能`retire`；对同一样本同一指标的重复分析不会重复记账。
- 专家组（`panel`/`admin`）对`pending_review`读数执行`release`（解除，恢复接收新读数）或`confirm`（确认违规，关联样本转为`adverse`，可据此创建`case`）；两个动作都需要`reason`。
- 读数的`anomaly`、`deviation`、`baseline_avg`永久保留，`release`/`confirm`只改变状态；用`GET /api/reading?status=pending_review`筛选待复核读数。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
