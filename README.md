# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

一个案例可挂多组「药品—事件词」搭配：首报建立第一组主搭配，随访可补报新的药品或事件词。每组搭配各自记录严重性与死亡转归，案例整体取最重一档（任一搭配死亡则案例死亡）。分国家报告按搭配出具，同一国家同一搭配只留一份；转归变化时，尚未提交的报告期限按新档位重算。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例；首报建立第一组主搭配，可用 `combos` 附带后续搭配。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询，详情含 `combos` 搭配组列表。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访；可用 `combos` 补报新的药品或事件词搭配。
- `POST /api/cases/{id}/medical-review`：医学审核员更新严重性、死亡和关联性；可用 `combos` 或 `combo_id` 指定搭配组，缺省裁定主搭配。案例整体取最重一档，未提交的分国家报告期限随转归重算。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：生成并提交分国家报告；报告按搭配出具（`combo_id`，缺省主搭配），同一国家同一搭配只建一份，重复提交返回已存在报告。
- `POST /api/cases/{id}/merge`：全局管理员合并重复案例。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 数据升级

旧版数据库首次启动时自动迁移：原案例的药品与事件词归入第一组搭配，已受理的旧报告和医学裁定挂到第一组搭配上；后补搭配另出报告。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型，事务以进程内互斥锁串行化并发提交。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
