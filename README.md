# 养老机器人补贴租赁 · 适老设备权益后端

管理外骨骼、看护机器人和适老设备的**家庭购置补贴、社区租赁补贴、机构服务补贴**三条路径。
资格按地区、年龄、照护等级、户籍、低收入身份与申请日政策版本判定；金额全程以整数"分"计算。

## 领域规则如何落地

| 业务要求 | 实现位置与做法 |
| --- | --- |
| 申请日冻结资格证据与政策版本 | `domain/policy.py` 的 `PolicyVersion`/`DeviceCatalog` 不可变；提交时 `quote()` 把政策全文、目录条目、证据（含申请日周岁）写入 `decision.frozen`，之后只读 |
| 比例就高、不得重复享受 | 三条路径逐项试算，合格者取补贴额最大者，只占用中选一条；在途申请的同型号设备直接追加 `duplicate_device_benefit` 拒绝原因 |
| 单件上限 / 年度上限 | 比例金额先按千分比取整（`ratio_amount`），再过单件上限；年度占用走台账校验 |
| 年度额度并发不超占 | `domain/ledger.py` append-only 台账，`OCCUPY/RELEASE/SETTLE/REVERSE` 四类分录；同一把锁内"先重放校验、后批量落账"，任一年不足则整批拒绝、不留分录 |
| 跨年租期 | `split_by_year` 按自然年切分半开区间 `[起, 止)`，月费换算日费后逐年计费、逐年占用 |
| 撤回 | `WITHDRAWN`，逐年 `RELEASE` 全额释放预留，原决定保留 |
| 提前归还 | 按实际使用日逐年分段结算，未用预留 `RELEASE` 退还年度额度，差异以更正记录留痕 |
| 资格复核失败 | 非追溯：结算至失败日前、之后释放；`retroactive=true`：已结算金额 `REVERSE` 追回。两种方式都不修改原决定 |
| 不篡改原决定 | 决定对象不再写入；一切变化追加 `corrections` 与台账反向分录，序号严格递增可重放 |
| 健康数据边界 | 履约事件只允许启用/维护/故障/替代/归还五类，字段白名单；含 `heart_rate/血压/健康` 等提示词或未知字段时**整份拒绝（422）且不落库**；医疗服务方数据不进入本系统，审核员视图同口径 |
| 故障责任争议 | `open_dispute` 置结算冻结，结算类操作返回 `settlement_frozen`；可同时登记替代设备事件，维护与替代服务不中断；认定责任后按停用区间分段结算（厂商/社区责任日不计补贴，家庭责任日全额自付） |
| 政策调整复算 | `recalculate` 用指定政策版本 + 目录版本 + 家庭样例重算旧申请，逐路径列出家庭自付、财政责任、拒绝原因与差额；同时模拟并发申请，逐家庭逐年给出额度不超占证明；`original_decision_unchanged` 证实原决定未变 |

## 目录结构

```
domain/
  errors.py       领域错误（稳定 code → HTTP 状态）
  money.py        金额/千分比/周岁/跨年分段
  policy.py       不可变政策、目录、证据与 quote() 纯函数试算
  ledger.py       家庭年度额度台账（append-only，线程安全）
  application.py  BenefitService：申请生命周期、事件白名单、争议、复算
  fixtures.py     杭州 2025/2026 政策、设备目录、两类家庭样例
service.py        HTTP 适配层 + --check 端到端自检
service_contract.py / test_domain.py / test_api.py  契约/领域/接口测试（共 45 例）
```

## HTTP 接口

- `GET  /health`
- `POST /admin/policies` · `POST /admin/catalogs` · `POST /admin/samples`（同名同版本重复注册返回 409）
- `POST /applications` 提交（拒绝也落决定，逐项给原因；超年度额度返回 422 与额度证明）
- `GET  /applications/{id}` · `GET /applications/{id}/reviewer`
- `POST /applications/{id}/withdraw|deliver|events|return|maturity|review-failed|disputes|dispute/resolve|recalculate`
- `GET  /households/{id}/quota?year=2025` · `GET /ledger?household_id=...`

## 运行

```bash
python3 service.py --check     # 端到端自检（冻结/跨年/争议/冲正/复算/超占拒绝）
python3 service.py --port 8000 # HTTP 服务
npm test                       # 45 例 unittest（契约 + 领域 + 接口）
```

金额单位均为**分**；比例为千分比整数（600 = 60%），任何环节不使用浮点。
