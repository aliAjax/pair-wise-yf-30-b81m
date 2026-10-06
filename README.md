# 药物警戒案例处理系统

使用 Python 标准库实现的独立原型，覆盖多渠道案例接入、去重、随访更正、严重性医学裁定、分国家报告、逾期升级、跨区域权限和案例合并审计。

## 多组药品-事件搭配（pairings）

一条案例对应一位患者，可挂多组「可疑药品 + 事件词」搭配，避免多药联用或随访补报时另开案例导致患者数与严重案例数虚高：

- 首报（`POST /api/cases`）建立一组主搭配（`seq=1`）。
- 随访（`POST /api/cases/{id}/followups`）可通过 `new_pairings` 补新搭配，或通过 `pairing_updates` 更新既有搭配的严重/死亡转归。
- 每组搭配各自记录严重（`serious`）、死亡（`fatal`）、关联性与起算时间（`anchor_at`）；案例整体转归取最重一档：死亡 > 严重 > 非严重，案例视图同时平铺主搭配字段以兼容旧客户端。
- 分国家报告按搭配出具：`POST /api/cases/{id}/reports` 可带 `pairing_id`（不带则主搭配），同一国家同一搭配只留一份，后补搭配需另行出报告。
- 搭配转归一变化（随访补报或医学裁定），该搭配所有**未提交**报告的期限以新信息接收时间重算（严重 15 天、死亡 7 天、非严重 90 天）；已受理报告不动。
- 同一组搭配并发提交：数据库唯一约束 `(case_id, 药品, 事件词)` 保证只建一份，撞键时幂等返回已存在搭配；随访仍用 `expected_revision` 做乐观并发控制。
- 旧库首次打开自动升级：原 `cases.product/event_term/serious/fatal` 迁入第一组搭配，旧报告与旧医学裁定继续挂在该搭配上（ID 保持不变），升级后可正常补搭配。
- 案例合并要求同一患者，搭配取并集；同一组搭配的同国家报告冲突时保留已提交（否则最早）一份。

## 运行

要求 Python 3.11+。

```bash
python3 app.py --db pharmacovigilance.db
```

默认监听 `127.0.0.1:8201`。首页为 `http://127.0.0.1:8201/`，健康检查为 `/health`。

所有接口使用请求头 `X-User-Id`、`X-Role` 和区域角色必需的 `X-Region`。角色为 `reporter`、`regional_lead`、`medical_reviewer`、`global_admin`。

## 主要接口

- `POST /api/cases`：录入案例，`dedupe_key` 相同则返回已存在案例。
- `GET /api/cases`、`GET /api/cases/{id}`：按权限查询；案例详情含 `pairings`、按搭配展开的 `reports`。
- `POST /api/cases/{id}/followups`：用 `expected_revision` 防止覆盖随访；可选 `new_pairings`（`product`、`event_term`、`serious`、`fatal`）和 `pairing_updates`（`pairing_id` 或 `seq` + 转归字段）。
- `POST /api/cases/{id}/medical-review`：医学审核员按搭配裁定严重性、死亡和关联性，可带 `pairing_id`（默认主搭配）。
- `POST /api/cases/{id}/reports`、`POST /api/reports/{id}/submit`：按搭配生成并提交分国家报告。
- `POST /api/cases/{id}/merge`：全局管理员合并同一患者的重复案例（搭配并集、报告去重）。
- `POST /api/escalate-overdue`、`GET /api/overdue`：逾期检查与升级。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

该实现使用请求头模拟身份，不含生产级登录、签名和密钥管理；SQLite 与标准库 HTTP 服务适合单机原型（写事务在进程内串行化，唯一索引兜底跨进程并发）。分国家规则采用内置严重 15 天、死亡 7 天、非严重 90 天规则，接入真实监管网关前需按当地法规扩展。
