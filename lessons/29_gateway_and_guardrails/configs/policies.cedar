// 第 29 课：IT 助手的授权策略 —— 策略即代码。
//
// 判定规则（Cedar 语义）：任一 forbid 命中 → 拒绝；否则任一 permit 命中 → 允许；否则默认拒绝。
// 求值出错的策略会被 Cedar 跳过；agentkit.contrib.policy.CedarPolicy 把"有错误"一律当拒绝（fail closed）。
//
// 两个动作：
//   call_tool             能不能调用这个工具。不能 → 直接拒绝，拒绝原因反馈给模型。
//   call_tool_unattended  能不能不经人工审批直接执行。不能 → PauseRun，等人批。
//
// 每条策略都有 @id：审计日志里记录的就是它，出了问题能直接定位到"是哪条规则放行 / 拒绝的"。

// ============================ permit：谁能调用什么 ============================

@id("employee-non-dangerous")
permit (principal, action == Action::"call_tool", resource)
when { principal.roles.contains("employee") && resource.risk != "dangerous" };

// 员工可以发起重置密码；"能不能重置别人"是参数级问题，由下面的 reset-self-only 管
@id("employee-reset-password")
permit (principal, action == Action::"call_tool", resource == Tool::"reset_password")
when { principal.roles.contains("employee") };

@id("it-admin-all-tools")
permit (principal, action == Action::"call_tool", resource)
when { principal.roles.contains("it_admin") };

// ====================== permit：哪些操作可以无人值守执行 ======================

@id("unattended-non-dangerous")
permit (principal, action == Action::"call_tool_unattended", resource)
when { resource.risk != "dangerous" };

// ================== forbid：硬性规则，优先于任何 permit ==================

// 租户隔离：租户专属工具只能被本租户的人调用。多一个角色也"洗白"不了。
@id("tenant-isolation")
forbid (principal, action, resource)
when { resource has tenant && resource.tenant != principal.tenant };

// 免费版租户不开放危险操作（第 09 课"套餐 × 风险"矩阵的策略版）
@id("free-plan-no-dangerous")
forbid (principal, action, resource)
when { resource.risk == "dangerous" && principal.tenant.plan == "free" };

// 参数级授权（ABAC）：普通员工只能重置自己的密码（对应 capstone 的 ArgumentPolicy）
@id("reset-self-only")
forbid (principal, action, resource == Tool::"reset_password")
when { context has target_user && context.target_user != principal }
unless { principal.roles.contains("it_admin") };

// 工资单只有财务部能导出 —— 连 IT 管理员也不行（显式拒绝优先）
@id("payroll-finance-only")
forbid (principal, action, resource == Tool::"export_payroll")
unless { principal.department == "finance" };

// 紧急开关：某个工具刚发现漏洞时，发布一条 forbid 就能全局停用，不用改代码、不用发版。
// @id("kill-switch-send-email")
// forbid (principal, action, resource == Tool::"send_email");
