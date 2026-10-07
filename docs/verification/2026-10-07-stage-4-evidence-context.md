# Stage 4 Evidence / CaseContext 实现与交接记录

日期：2026-10-07（Asia/Shanghai）。授权来源：用户要求完成《交接1006》剩余工作。
交接 PR #7 已审查精确 head `03b67c6`、核对 CI/链接并合并为 `dd059fc`。
Stage 4 分支为 `codex/core-evidence-context`，接口冻结先行提交 `afb6ce8`；实现提交
`d3146cf34dfe83eaf85e227e48a61c0c6ebbe156`。独立交付为
[Draft PR #8](https://github.com/Mark-UM/FDE-test/pull/8)。
本文件只记录实际实现/验证，不代替数据负责人验收，不授权 AI 或下一阶段。

## 交付内容

1. 复用现有 Provider，依次验证 Inquiry、订单、完整包裹列表、各包裹物流及订单级仓库备注。
   正常/EMPTY/NO_TRACKING 与来源故障分开；必需来源失败无 Context，局部失败保留成功部分。
2. 封闭 Evidence/CaseContext：稳定 ID、不可变快照 JSON Pointer、原值/类型/源时间/事件时间；
   FACT 与不可信 SOURCE_TEXT 分开。程序计算时效、未知、缺失、两项保守冲突及质量。
3. resolve/run/context/order 四个入口，当前权限决定访问。创建和读取均检查版本/引用链；
   order 只读当前快照，按原时间重新判断时效，不网络取数。
4. 短事务、幂等、RUNNING/BUSY、单调版本与安全 Audit；新取数立即清旧当前指针。
   网络期间换权/换绑定丢弃结果；提交异常回滚后留 RUNNING，显式运维 CLI 可恢复指定 run。
5. CORS 允许 Idempotency-Key；新增回归和真实跨系统验收；沿用 0001/0002，不改变数据库。

主要文件：`app/services/context_models.py`、`source_collection.py`、`evidence.py`、
`resolution.py`、`recover_resolution.py`、`app/api/resolution.py`（均在 backend）；
新增 `tests/test_evidence.py`、`tests/database/test_resolution*.py` 及 canonical Fake fixture。
API/Main、既有边界测试、CI 选择器和文档作相应调整；无前端运行代码、Sandbox 或 AI 改动。

## 运行环境与命令

- Python 3.13.13；PostgreSQL 17.11（postgres:17-alpine），本次专用 loopback 容器。
  每测试随机 Product schema，仅迁移/清理自己的 schema；非 SQLite 替代。
- 独立 Sandbox SHA：`4131f1c4be7af6a6981e15379214d238228e8fa2`。
  临时 SQLite 文件由 Sandbox 自己使用；Product 仅经 HTTP，未导入其源码或读取其数据库。
- FixedClock：`2026-09-20T06:00:00Z`；`core-mvp-v1` / `core-policy-v1` / `freshness-v1`。
- 后端：`python -m pytest --sandbox-url http://127.0.0.1:19004 -q -p no:cacheprovider`
  （同时设置专用 TEST_DATABASE_URL）；Ruff check / format --check。
- 前端：`npm run lint` / `npm run typecheck` / `npm run build`。
- Compose：`docker compose --env-file .env.example config --quiet`。这是配置验证，不是完整联启。

| 最终本地检查（实现 d3146cf） | 实际结果 |
| --- | --- |
| 全量后端 pytest + 独立 Sandbox + PostgreSQL | **310 passed，0 failed，0 skipped**，165.60 秒 |
| 非数据库/HTTP 测试 | 144 项（含纯规则、来源/transport、权限边界） |
| PostgreSQL 测试 | 142 项＝原 91＋新增 Stage 4 51；另 11 个 resolver HTTP 测试也使用真实 PG |
| 真实 Sandbox HTTP | 24 项＝原 Provider 13＋resolver 11；无 mocks 替代 |
| Ruff lint / format | 全部通过，48 个文件格式检查通过 |
| 前端 lint / typecheck / build | 全部通过 |
| Compose config、恢复 CLI --help、改动文档相对链接 | 通过 |

310 是上述测试类别的合计；11 个 HTTP+PG resolver 只在 HTTP 类计数，不重复加。
另机械解析契约的 6 个正例（NORMAL/MULTI_PARCEL/STALE/SOURCE_FAILURE/UNKNOWN_TIME/CONFLICT）
全部符合新封闭 CaseContext schema，不计入 310，也不称其为真实抓取。
3 个非阻断 warning：两项依赖弃用提示和一项故意损坏 canonical 模型的负例 serializer 提示。
首轮夹具失败及两次环境中断未计通过；最终重跑是完整的零跳过结果。

实现 d3146cf 的 [push CI](https://github.com/Mark-UM/FDE-test/actions/runs/37569553868)
backend/frontend/database/compose 四项成功；CI database 明确排除独立 HTTP integration，
不以 skip 冒充真实 HTTP。文档同步后的最终 head 和 checks 可在 PR #8 实时核对。

## 场景证据

| 范围 | 断言与实际来源 |
| --- | --- |
| CORE-01–11 | 11 个真实 Sandbox resolver HTTP+PG 场景；S02/04 未知时间、S06 计划原文、S07 72h 旧源、S08 timeout、S09 warehouse unavailable、S10 logistics 404、S11 双证据冲突、S12 FAILED、S01/05 空包裹 |
| CORE-12–16 | 五身份；未登录/过期/撤销/停用；未知与越权同 403；闭合请求防伪、来源绑定检查、无订单绑定零取数 |
| CORE-17–20 | 两包裹一失败、NO_TRACKING 零物流调用、未知/未来时间、精确阈值及微秒超界、恶意文本仅 SOURCE_TEXT；Product Fake/FixedClock 与真实 PG |
| CORE-24/26 | 新取数成功/失败均淘汰旧当前；历史重鉴权；同 key 不重取、202/409、真实并发、网络后换身份/归属/绑定的拒绝 |
| CORE-30 | 提交异常全回滚、RUNNING 占用、精确人工恢复、错误版本/重复恢复拒绝，新 key 可继续 |
| 补充 | 历史篡改、跨 Inquiry 的有效外键但非法链被拒；绑定改变后旧 Context 仅历史；闭合 OpenAPI、preflight；events 失败/429/socket timeout/坏 JSON 的补充 transport→resolver 测试 |

每个 Context 返回前从本 run 的 SourceFetch 重建并机械校验全部 Evidence pointer/value/type、
质量及关联；场景断言包含状态、outcome、原时间、scope、引用和版本，不只校验 HTTP 200。
S08 是真实 HTTP 504；socket timeout/429/events 故障是固定 Sandbox 缺口的补充模拟，
不能称其为真实 Sandbox 新场景。真实 S02 SUCCEEDED 仍为 DEGRADED，不冒充 COMPLETE。

## 审查问题与回归

以下均为本轮未提交候选的自审修正，不声称生产历史漏洞。
S4-01–04 的修复和回归均包含于实现提交 `d3146cf`；DOC-02 首次冻结于 `afb6ce8`。

| ID | 问题 / 处理 | 回归证据 |
| --- | --- | --- |
| DOC-01 | PR #7 明确 auth→幂等→新请求版本检查；沿用，不重复改契约 | 完成/终止后旧版本重放、RUNNING 幂等测试 |
| DOC-02 | WarehouseProvider 仅订单级且无 parcel_id；一次订单 notes 查询，不伪造归属 | 来源编排、真实多包裹 S04 与冻结解释 |
| S4-01 | SourceOutcome 原按输入列表排列，影响同快照集合确定性；改稳定排序 | 反转同一 fetch 集合后 Context 完整重建一致 |
| S4-02 | 补完整响应 DTO、outcome/snapshot/version/quality 语义与落库后重建校验 | 篡改值/类型/pointer/时间/版本/snapshot/额外字段被拒 |
| S4-03 | 结束事务补原 actor/session 一致性；授权变化不保存事实 | 换身份/停用/撤销/换归属/绑定/版本真实 PG 回归 |
| S4-04 | 当前 Context 除 order/inquiry/ownership 还须匹配来源系统 | 三种绑定改变后 is_current=false、order 409，零新调用 |
| TEST-01 | 首轮 3 个夹具错误：ESCALATED 缺 reason、expiry 不大于 created、同步 hook 阻塞 event loop | 修正夹具；46 项当时 Stage 4 PG 重跑通过，最终扩展另计 |
| ENV-01 | Docker 引擎意外退出，随后重启数据库尚在恢复；两次验收中断不计通过 | 恢复引擎，新建隔离库、确认 pg_isready 后全量重跑 |

## 尚待完成的出口

- [独立 Stage 4 PR #8](https://github.com/Mark-UM/FDE-test/pull/8) 已提交；
  数据负责人按交接清单验收质量，必要修复后再审查合并。保持 Draft，不自动合并。
- 未声称两人签字或独立开发者批准；本轮是程序测试加自审。
- Stage 5 AI/Analysis/草稿、回复语义 Validation/人工审核，Stage 6 UI/浏览器 E2E 未实现。
- 静态工作台 PR #4、main 保护及完整 Compose 联启仍是独立未完成任务。
  本轮不配置治理、不修改共享服务，也不把基础构建或后端 HTTP 等同端到端产品完成。

恢复细节见 [runbook](../operations/resolution-recovery.md)，接口见
[冻结切片](../contracts/stage-4-implementation.md)；验收标准仍以
[数据负责人清单](../testing/2026-10-06-evidence-case-context-acceptance.md)为准。
