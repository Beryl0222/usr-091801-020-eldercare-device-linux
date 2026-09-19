# 养老机器人补贴租赁（适老设备权益后端）

管理外骨骼、看护机器人等适老设备在**家庭购置补贴、社区租赁补贴、机构服务**
三条路径下的资格、补贴与结算。面向民政服务机构的核心承诺：

1. **先冻结，后决定**：申请日把老人资格证据（年龄、照护等级、户籍、居住证、
   低收入身份）、政策版本、设备目录版本、合同要素整体冻结进不可变快照；
   之后政策调整或档案变化都不影响原决定。
2. **比例就高、不得重复享受**：三条路径逐条评估，命中多档待遇时比例就高，
   最终只选择补贴最高的一条路径；未选路径保留互斥说明。
3. **年度额度并发安全**：个人年度上限与地区年度预算的“检查—占用”在同一把
   台账锁内原子完成，批量申请整批通过或整批拒绝，任何并发下都不会超占。
4. **冲正不篡改**：撤回、设备提前归还、资格复核失败一律追加反向分录
   （RELEASE / CLAWBACK），原决定原样保留；全量台账以哈希链串联，可随时验真。
5. **跨年租期分段结算**：补贴按自然日、最大余数法分摊到各自然年，分别占用
   各年度额度，分摊之和与总额严格相等；月中提前归还按实际占用天数截断。
6. **履约最小采集**：设备交付后只记录启用、维护、故障、替代、归还五类事件，
   字段白名单 + 健康特征拦截，体征/健康监测内容在入口即被拒绝（由医疗服务方持有）。
7. **争议冻结结算、不中断替代设备**：厂商/社区/家庭对故障责任有争议时冻结
   结算，争议期间归还先挂账；但替代设备照常申请启用，定责解冻后自动补结。
8. **旧申请可复算**：工作人员可用指定的新政策版本、新目录版本或家庭样例做
   只读试算，逐项输出家庭自付、财政责任、拒绝原因及与原决定的差异，不写台账。

金额在领域内一律用**整数分**，杜绝浮点误差。

## 运行

```bash
python3 service.py --check           # 自检
python3 service.py --port 8000       # 启动 HTTP 服务
npm test                             # 全量测试（42 个用例）
```

## 目录结构

```
service.py               HTTP 入口（保留 /health 稳定契约）
service_contract.py      健康检查契约测试
benefits/
  money.py               分/元换算、自然日分摊、最大余数法
  errors.py              业务错误与稳定拒绝原因码
  models.py              政策版本、目录、证据、快照等不可变模型
  registry.py            版本注册表（按申请日解析生效版本）、档案、预算
  eligibility.py         资格评估、就高待遇档、补贴与跨年分段计算
  ledger.py              哈希链只追加台账
  service.py             领域服务：申请/冲正/结算/争议/替代/复算/额度
data/seed.py             杭州/成都两地、2025/2026 跨版本政策目录与老人样例
test_domain.py           金额与资格引擎单元测试
test_service_flow.py     领域流程：并发占用、冲正、争议、复算、防篡改
test_http_api.py         HTTP 端到端测试
```

## 主要 HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/applications` | 提交单笔申请，返回冻结快照、三路径评估、决定与逐项金额 |
| POST | `/applications/batch` | 原子批量提交（同时提交的多笔申请额度一起校验） |
| GET  | `/applications/{id}` | 申请详情：决定、事件、已结分段、年度预留 |
| POST | `/applications/{id}/withdraw` | 撤回（冲正全部预留） |
| POST | `/applications/{id}/events` | 履约事件（启用/维护/故障/归还/替代） |
| POST | `/applications/{id}/settle` | 到期分段结算，可提前归还截断 |
| POST | `/applications/{id}/recheck` | 资格复核，失败则释放未用额度并追回已结补贴 |
| POST | `/applications/{id}/disputes` | 开启故障责任争议（冻结结算） |
| POST | `/applications/{id}/disputes/close` | 定责解冻，挂账归还/到期分段自动补结 |
| POST | `/applications/{id}/replacements` | 争议期申请替代设备（不中断照护） |
| POST | `/applications/{id}/replay` | 指定政策/目录版本复算旧申请（只读） |
| POST | `/replays` | 用家庭样例试算（只读） |
| GET  | `/quota?applicant_id=&region=&year=` | 个人/地区年度额度占用 |
| GET  | `/ledger/verify` | 哈希链验真 |
| POST | `/admin/policies` `/admin/catalogs` `/admin/applicants` `/admin/budgets` | 登记新版本/档案/预算 |

## 决定返回的关键内容

* `snapshot`：冻结的证据（含申请日年龄）、政策版本、目录版本、三路径合同要素。
* `evaluation.routes`：每条路径的 `eligible`、`reject_codes/reject_reasons`、
  命中待遇档、比例、月度补贴、年度分摊、逐月结算计划。
* `decision`：入选路径的优惠前金额、优惠、**财政补贴**、**家庭自付**、
  跨年分摊、未选路径的拒绝原因、`quota_proof`（每年度占用前/占用/占用后余额）。
* 被拒绝的申请 `status=DENIED`、`decision=null`，但三路径拒绝原因码完整可解释。

## 台账分录类型

`APPLICATION_SUBMITTED → DECISION → RESERVE → SETTLE`，冲正侧
`WITHDRAWAL / RELEASE / REVIEW_PASSED / REVIEW_FAILED / CLAWBACK`，
履约侧 `DEVICE_EVENT / REPLACEMENT_APPROVED`，争议侧
`DISPUTE_OPENED / DISPUTE_CLOSED`。每条分录保存前一条的 SHA-256，
任何对历史分录的改写都会在 `/ledger/verify` 暴露。

## 种子场景示例

```bash
python3 service.py --port 8000 &
# A001（杭州，重度失能低收入）跨年租 6 个月，补贴按 2025/2026 分两年占用
curl -s -X POST localhost:8000/applications -H 'Content-Type: application/json' -d '{
  "applicant_id":"A001","device_id":"EXO-01","apply_on":"2025-11-20",
  "route":"RENT","months":6,"lease_start":"2025-12-01"}'
# 用 2026 新政策+新目录复算这笔 2025 旧申请（只读，逐项对比）
curl -s -X POST localhost:8000/applications/APP-.../replay -H 'Content-Type: application/json' -d '{
  "policy_version":"HZ-2026","catalog_version":"HZ-CAT-2026"}'
```

样例老人：A001 重度失能低收入（三路径条件最充分）、A002 中度失能、
A003 轻度失能（仅可租赁）、A004 外地户籍无居住证（户籍/居住被拒）、
A005 年龄不足、A006 成都老人。
