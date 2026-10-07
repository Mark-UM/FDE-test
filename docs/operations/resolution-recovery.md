# 中断取数的人工恢复

此入口只供可信数据库运维使用，不是 Product 用户权限或 HTTP API。用户请求不能自行
标记其他人的操作失败。MVP 不定时重试，也不凭“运行时间较长”自动判定中断。

1. 确认原服务进程已停止或该调用确定不能提交。长时间外部请求仍可能正在运行，
   必须先调查。通过授权 GET `/api/v1/inquiries/{id}/runs/{run_id}` 读取 RUNNING 与
   最新 lock_version，运维核对 Inquiry.latest_run_id、绑定及占用。
2. 设置目标 Product 的 DATABASE_URL，在 backend 工作目录执行下面命令。使用确切
   UUID 和当前版本；示例为占位符，不要直接复制到共享数据库执行。

```powershell
python -m app.services.recover_resolution --inquiry-id <inquiry-uuid> --run-id <run-uuid> --expected-lock-version <current-version> --confirm-interrupted
```

3. CLI 仅锁定指定 Inquiry 并校验 run 属于它、是 latest_run、RUNNING、Inquiry OPEN，
   且版本匹配。失败时拒绝；成功原子写 FAILED/INTERRUPTED、finished_at、
   OPERATION_RECOVERED Audit，清当前指针、lock_version +1。不会恢复旧 Context，
   不修改历史快照，不调用 Provider。无独立 Recovery HTTP 路由。
4. 原 key 重放只返回 INTERRUPTED；需要重新取数时由当前获授权操作者提交新 key 和
   最新 lock_version。晚到的原调用会因 run 已结束而被拒绝，不能覆盖恢复结果。

验收通过 `tests/database/test_resolution.py` 的中断事务、错误目标/版本、重复恢复及
新 key 测试完成。每次测试只操作自身生成的 PostgreSQL schema，不能据此自动执行
生产恢复。共享环境应将命令、目标 UUID、操作者和 request_id 纳入运维记录。
