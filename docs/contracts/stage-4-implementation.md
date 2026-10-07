# Stage 4 实现切片与接口冻结

日期：2026-10-07。用户要求完成《交接1006》中的剩余任务，明确授权审查/合并 PR #7
并实现 Stage 4，以单独 PR 交付。交接 PR 已合并为 `dd059fc`；本切片不授权 Stage 5+。
依据 core-mvp-v1 的 API、生命周期、数据与 Evidence 契约，以及 Stage 4 数据验收清单。

## 实现范围

- POST `/api/v1/inquiries/{id}/resolve-context`：封闭 JSON 仅 expected_lock_version；
  必需单个 Idempotency-Key，1–200 字符、非空白、保留原值。首个成功/部分成功为 201，
  同 key 同请求重放为 200，RUNNING 重放为 202（含查询地址）；失败重放原安全错误。
- GET `/api/v1/inquiries/{id}/runs/{run_id}`：授权后只读操作状态、版本、Context ID、
  error_code、最新 lock_version；BUSY 对应 RUNNING，不增加工作流 enum。
- GET `/api/v1/inquiries/{id}/contexts/{context_id}`：封闭完整 CaseContext 与外层 is_current；
  历史每次鉴权，跨 Inquiry/不存在统一 404。返回前校验整个版本/引用链。
- GET `/api/v1/inquiries/{id}/order`：只读取有效当前 Context 所引用订单快照，返回
  order、逐字段 freshness、context_id；无绑定 422、无有效当前 Context 409，不网络取数。
- CORS 显式允许 Idempotency-Key；不增加 PATCH、AI、草稿/批准/发送或任意订单搜索。

返回 DTO 也封闭：resolve 为 run_id/state/context_id/context_version/quality/lock_version，
RUNNING 另有 query_url 与 Location；run 查询为 id/state/version/context_id/error_code/lock_version；
Context 查询 envelope 精确为 `{context: CaseContext, is_current: boolean}`；order 为
`{context_id, order: OrderSnapshot, freshness: {JSON Pointer: FRESH|STALE|UNKNOWN}}`。
业务时间以 UTC 返回；成功读取不刷新原 canonical 时间。OpenAPI 标明 UUID 路径参数。

## 短事务与中断

复用 User → AuthSession → Inquiry 锁序。开始事务先认证/授权，校验封闭请求和 key，
检查既有幂等记录，再检查新操作的 state/lock_version/占用；写 RUNNING，递增 Run.version，
清旧当前 Context/Draft，Inquiry OPEN、lock_version +1。提交后依次执行 Provider HTTP。
结束事务重新认证/授权、检查占用、版本和原绑定未变，验证快照与 Evidence 关联，再原子
保存 SourceFetch/Context/Audit、结束 Run、lock_version +1。数据库异常回滚整个结束事务，
遗留 RUNNING 留给人工恢复；拒绝不能被通用 Stage 3 包装器部分提交。

网络期间权限变化：不保存或返回所取 payload，内部失败收尾只记录安全状态/Audit并释放
仍属于原 Run 的占用；响应当前身份/授权错误。不会恢复旧 Context。若占用已被人工恢复，
不得发布结果或覆盖该恢复。意外进程中断保留 RUNNING，重放不重复调用。
人工恢复仅由可信数据库运维通过 CLI 对指定 inquiry/run/expected_lock_version 执行，
要求明确确认；目标不是当前 RUNNING 则拒绝。写 FAILED/OPERATION_RECOVERED，不调用来源。

## 来源与证据

必需 get_inquiry → get_order → get_parcels，失败无 Context。随后对所有 parcel 获取
完整 shipment（tracking=null 记录 NO_TRACKING），并按订单获取 warehouse notes。
来源 ID/订单绑定和数组 ID 唯一性必须验证；不合法批次不保存部分“成功”快照。
Provider 错误使用冻结 outcome 映射；可选失败保留成功部分，PARTIAL/DEGRADED。
SupportInquiry 不增加 canonical fetched_at；SourceFetch 的调用时间与其原创建时间分别保存。
各快照及事件 fetched_at/source_updated_at/occurred_at 均保留原值。

DOC-02 接口差异记录：交接概述曾将 notes 与逐包裹查询并列，但已冻结 WarehouseProvider
只接受 external_order_id，WarehouseNoteSnapshot 没有 parcel_id。因此一次查询订单级 notes，
对应订单 scope；不复制查询、不伪造包裹归属。逐包裹完整性要求应用于 shipment/outcome。
这是明确既有接口的实现解释，不扩展 Provider 或 Sandbox。

闭合 schema 覆盖全部 Evidence/Context 字段。Evidence ID、pointer/value/type、来源和时间
按冻结规则验证，FACT/SOURCE_TEXT 分开；freshness、缺失、两项保守冲突、quality 和 flags
均由程序确定性生成。freshness-v1/core-policy-v1 默认配置随 Context 保存；配置调整必须
使用新版本，历史不可覆盖。新取数失败不恢复旧当前指针，失败 run 保留版本空洞。

## 模块与验收

预计修改 api/core 与新增 resolution API、main CORS/Provider 工厂；新增闭合模型、纯函数
Evidence 构建、Provider 编排、短事务服务和人工恢复 CLI；沿用既有数据库，不改 0001/0002。
测试分为规则/引用、真实 PostgreSQL API/事务/并发、独立 Sandbox HTTP 场景三层。
覆盖验收矩阵 CORE-01–20、24、26、30 中的 Stage 4 断言及交接补充场景；Stage 5/6
不计入本次通过数。保留问题 ID、修复提交、回归结果和所有未执行项目。
