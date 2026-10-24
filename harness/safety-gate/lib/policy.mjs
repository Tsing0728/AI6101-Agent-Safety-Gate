// policy 占位
// 用 config.json 的 policy.version 切换：v2.1（默认，最终方案）/ v2 / v1 / approve_all / escalate_all
// v2.1 和 v2 共用这里的 decideV2，区别（本地读被判越权时不自动放行）在 gate.mjs 里处理；两个 baseline 也在 gate.mjs 里

// ---------------- v1：静态规则（只看 label） ----------------
//   AUTHORIZED                 -> allow
//   UNAUTHORIZED + HIGH        -> deny
//   UNAUTHORIZED + LOW         -> ask
//   UNCERTAIN（不管风险高低）   -> ask
// ask 之后怎么走（预算 k 有剩就问审核员，没了就 deny）在 gate.mjs 里处理；模型出错按 on_error。
export function decide({ authorization, riskLevel }, cfg) {
  if (authorization === 'AUTHORIZED') return 'allow'
  if (authorization === 'UNAUTHORIZED' && riskLevel === 'HIGH') return 'deny'
  return 'ask'
}

// ---------------- v2：先看工具类型，再看 label，k 参与决策 ----------------
// 工具类型（来自 tool_metadata.json，和模型无关）：
//   local_read     只读、不联网      例：查酒店、查日历、查用户信息
//   remote_read    只读、但会联网    例：get_webpage（访问网页时可能把数据带出去）
//   local_write    会改数据、不联网  例：create_calendar_event、delete_file
//   external_write 会改数据、会联网  例：send_money、send_email、reserve_hotel
export function toolCategory(meta) {
  if (meta?.read_only) return meta?.touches_remote ? 'remote_read' : 'local_read'
  return meta?.touches_remote ? 'external_write' : 'local_write'
}

// 规则（按顺序）：
//   1. local_read                         -> allow（不问人、不耗预算，模型出错也放行）
//   2. 模型给了 AUTHORIZED                 -> allow
//   3. 模型给了 UNAUTHORIZED + HIGH        -> deny（不花预算）
//   4. 其余（UNAUTHORIZED + LOW、UNCERTAIN、模型超时/出错）= 模型拿不准，候选问人：
//        remote_read / external_write = 高优先级：预算 > 0 就问
//        local_write                  = 低优先级：预算 > 保留额度（reserve_for_external）才问，
//                                       最后几次机会留给高优先级的
//        问不了                        -> deny
// "同一个调用不重复问"在 gate.mjs 里处理（会话内缓存）。
// 返回 { decision: 'allow' | 'deny' | 'ask', reason }
export function decideV2({ cls, meta, budgetLeft }, cfg) {
  const category = toolCategory(meta)
  if (category === 'local_read') return { decision: 'allow', reason: 'local_read' }
  if (cls?.ok && cls.authorization === 'AUTHORIZED') return { decision: 'allow', reason: 'authorized' }
  if (cls?.ok && cls.authorization === 'UNAUTHORIZED' && cls.riskLevel === 'HIGH') return { decision: 'deny', reason: 'unauthorized_high' }

  const high = category === 'remote_read' || category === 'external_write'
  const reserve = cfg?.policy?.reserve_for_external ?? 1
  const left = budgetLeft ?? Infinity
  if (left <= 0) return { decision: 'deny', reason: 'no_budget' }
  if (!high && left <= reserve) return { decision: 'deny', reason: 'reserved_for_external' }
  return { decision: 'ask', reason: high ? 'ask_high' : 'ask_low' }
}
