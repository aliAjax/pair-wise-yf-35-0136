# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
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
- `passport_reading`：生物护照读数，即实验室在分析样本时登记的某个指标值。

## 生物护照复核

实验室在样本分析后登记读数（`POST /api/passport_readings`，角色`lab`），登记内容为指标名、数值和单位；采样时间取自样本的`collected_at`，因此可跨年形成序列。同一运动员同一指标按采样时间排序，同一样本重复分析不重复记账（按`sample_id`+`marker`幂等返回已有读数）。

- 同一指标的前两条读数直接入账（`recorded`）；从第三条起，若偏离前两条（按采样时间最近的两条）平均值超过一成（>10%，双向），读数进入`pending_review`，否则为`recorded`。
- 存在`pending_review`读数时：该运动员的退役（`retire`）被拒绝，且该指标暂停接收新读数（其他指标不受影响）。
- 专家组（`panel`）可`dismiss`（解除异常，需`rationale`）或`confirm`（确认违规）；异常历史永久保留，已解除/确认的读数仍参与后续序列。
- 确认违规后，对应样本（含已被`clear`放行的样本）自动转为`adverse`阳性，可据此创建`case`。
- 待复核列表：`GET /api/passport_readings?status=pending_review`。
- 同指标单位必须一致，单位冲突会被拒绝。
- 读数也可在样本`analyze`动作中随`data.readings`数组一并登记。

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
