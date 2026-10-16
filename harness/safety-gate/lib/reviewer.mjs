// 模拟人类审核员 占位
// 返回 { approved, reason, usage }
export async function review({ task, toolName, toolArgs, trace, rationale }, cfg) {
  const decision = cfg.reviewer?.placeholder_decision === 'approve'
  return {
    approved: decision,
    reason: rationale || `[占位审核员] 固定返回 ${decision ? '批准' : '拒绝'}`,
    usage: null,
  }
}
