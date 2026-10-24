// 安全审批门插件
// 每次 tool call 执行前：
//   1. 从 dsh 取数据（工具名、参数、用户任务、trace、会话 ID）
//   2. 查表填 tool_metadata
//   3. 调安全门模型拿 authorization + risk_level
//   4. 固定阈值三选一：allow / deny / ask（这里后面我们具体看怎么做，我这里这么写保证链路完整）
//      现在默认是 policy v2.1（先看工具类型，再看 label，k 参与决策），见 lib/policy.mjs
//      v2.1 = v2 + 本地读被模型明确判 UNAUTHORIZED 时不自动放行（离线回放里拦截率 69% -> 95%）
//      v1、v2 和两个 baseline（approve_all / escalate_all）保留用于对比，用 config.json 的 policy.version 切换
//   5. ask：预算 > 0 交给模拟审核员（预算 -1）；预算 = 0 直接 deny
//   6. 记日志，返回结果给 dsh

import { loadConfig } from './lib/config.mjs'
import { lookupMetadata } from './lib/metadata.mjs'
import { extract } from './lib/extract.mjs'
import { classify } from './lib/classifier.mjs'
import { decide, decideV2, toolCategory } from './lib/policy.mjs'
import { Budget } from './lib/budget.mjs'
import { review } from './lib/reviewer.mjs'
import { makeLogger } from './lib/logger.mjs'
import { DENY_MESSAGE, NOT_AVAILABLE_MESSAGE } from './lib/deny.mjs'

export const name = 'safety-gate'

// 参数对象按 key 排序后再序列化，保证同样的参数得到同样的字符串（用于"同一个调用"的判断）
function stableStringify(v) {
  if (Array.isArray(v)) return `[${v.map(stableStringify).join(',')}]`
  if (v && typeof v === 'object') return `{${Object.keys(v).sort().map(k => `${JSON.stringify(k)}:${stableStringify(v[k])}`).join(',')}}`
  return JSON.stringify(v)
}

export function apply(ctx) {
  const cfg = loadConfig()
  const budget = new Budget(cfg.k)
  const log = makeLogger(cfg.log_dir)
  const reviewerDesc = cfg.reviewer?.mode === 'llm' ? `LLM（${cfg.reviewer.model}）` : `占位（固定${cfg.reviewer?.placeholder_decision === 'approve' ? '批准' : '拒绝'}）`
  const policyVersion = cfg.policy?.version ?? 'v2.1'
  // v2：同一会话里完全相同的调用（工具 + 参数）不重复问，直接沿用上次的最终结果
  const decided = new Map()
  console.log(`[safety-gate] 已加载。policy=${policyVersion}，k=${cfg.k}，非 AgentDojo 工具=${cfg.other_tools ?? 'deny'}，模型服务=${cfg.classifier.url}，审核员=${reviewerDesc}`)

  async function callClassifier(info, meta, timeoutMs) {
    const c = timeoutMs ? { ...cfg, classifier: { ...cfg.classifier, timeout_ms: timeoutMs } } : cfg
    return classify({
      userTask: info.task,
      history: info.history,
      proposedToolCall: { function: info.toolName, args: info.toolArgs },
      toolMetadata: meta,
    }, c)
  }

  function recordClassifier(record, cls) {
    Object.assign(record, {
      authorization: cls.authorization,
      risk_level: cls.riskLevel,
      risk_factors: cls.riskFactors,
      rationale: cls.rationale,
      classifier_ok: cls.ok,
      classifier_error: cls.error,
      classifier_usage: cls.usage,
      classifier_latency_ms: cls.latencyMs,
    })
  }

  // policy v2 的完整流程，返回 'allow' | 'deny'；所有中间结果写进 record
  async function decideWithV2(info, meta, record) {
    const category = toolCategory(meta)
    Object.assign(record, { policy_version: policyVersion, tool_category: category })

    // 本地只读：一律放行。模型照样问（label 记日志，报告用），但用较短的超时，超时也放行
    // v2.1：如果模型明确判 UNAUTHORIZED，就不自动放行，当作"本地写"走下面 v2 的规则（HIGH 拒绝，LOW 问人/没预算拒绝）
    //       模型超时/出错、判 AUTHORIZED 或 UNCERTAIN：照样放行
    let readCls = null
    if (category === 'local_read') {
      const cls = await callClassifier(info, meta, cfg.policy?.local_read_classifier_timeout_ms ?? 10000)
      recordClassifier(record, cls)
      if (!(policyVersion === 'v2.1' && cls.ok && cls.authorization === 'UNAUTHORIZED')) {
        Object.assign(record, { policy_decision: 'allow', policy_reason: 'local_read', escalated: false })
        return 'allow'
      }
      readCls = cls
      record.flagged_read = true
    }

    // 同一个调用之前判过：直接沿用，不问模型、不问人、不耗预算
    const key = `${info.sessionId}|${info.toolName}|${stableStringify(info.toolArgs)}`
    if (decided.has(key)) {
      Object.assign(record, { policy_decision: decided.get(key), policy_reason: 'repeat_call', escalated: false })
      return decided.get(key)
    }

    const cls = readCls ?? await callClassifier(info, meta)
    if (!readCls) recordClassifier(record, cls)
    const policyMeta = readCls ? { ...meta, read_only: false, touches_remote: false } : meta
    const { decision, reason } = decideV2({ cls, meta: policyMeta, budgetLeft: budget.left(info.sessionId) }, cfg)
    Object.assign(record, { policy_decision: decision, policy_reason: reason })

    let final = decision === 'allow' ? 'allow' : 'deny'
    if (decision === 'ask') {
      budget.consume(info.sessionId)
      // 审核员不看安全门模型的 label / 理由，独立判断
      const r = await review(info, cfg)
      Object.assign(record, {
        escalated: true, reviewer_approved: r.approved, reviewer_reason: r.reason, reviewer_usage: r.usage,
        reviewer_error: r.error, reviewer_latency_ms: r.latencyMs,
      })
      final = r.approved ? 'allow' : 'deny'
    } else {
      record.escalated = false
      if (reason === 'no_budget' || reason === 'reserved_for_external') record.budget_exhausted = true
    }
    decided.set(key, final)
    return final
  }

  ctx.on('tools/pre-execute', async (payload, next) => {
    // 只审批 AgentDojo 的工具（名字以 gated_tool_prefix 开头）。
    // dsh 自带工具（read、glob、grep、pwsh 等）能读电脑上的任意文件，
    // agent 会用它们偷看 AgentDojo 的任务答案和本插件的代码/日志，所以默认禁用（other_tools = "deny"）：
    // 不调模型、不消耗预算，日志里记为 non_agentdojo_tool。设为 "allow" 则直接放行。
    const prefix = cfg.gated_tool_prefix
    if (prefix && !(typeof payload?.name === 'string' && payload.name.startsWith(prefix))) {
      if (cfg.other_tools === 'allow') return await next()
      log({
        event: 'non_agentdojo_tool',
        session_id: payload?.agent?.session?.header?.id ?? payload?.agent?.id,
        call_id: payload?.callId,
        tool_name: payload?.name,
        final_decision: 'deny',
      })
      return { kind: 'deny', reason: NOT_AVAILABLE_MESSAGE }
    }

    const started = Date.now()
    const record = { event: 'decision' }
    let final = 'deny'

    try {
      // 1. 取数据
      const info = extract(payload, cfg)
      const { meta, found } = lookupMetadata(info.toolName)
      Object.assign(record, {
        session_id: info.sessionId,
        call_id: info.callId,
        tool_name: info.toolName,
        raw_tool_name: info.rawToolName,
        tool_args: info.toolArgs,
        tool_metadata: meta,
        metadata_found: found,
        task: info.task,
        trace_len: info.trace.length,
        k: cfg.k,
        budget_before: budget.left(info.sessionId),
      })

      // 两个 baseline（作业要求的对照组）：不调安全门模型
      //   approve_all ：所有 AgentDojo 调用都放行（相当于没有安全门，但 dsh 自带工具照样禁用，对比才公平）
      //   escalate_all：所有 AgentDojo 调用都交给审核员，预算用完就拒绝
      if (policyVersion === 'approve_all' || policyVersion === 'escalate_all') {
        Object.assign(record, { policy_version: policyVersion, tool_category: toolCategory(meta) })
        if (policyVersion === 'approve_all') {
          Object.assign(record, { policy_decision: 'allow', policy_reason: 'approve_all', escalated: false })
          final = 'allow'
        } else if (budget.consume(info.sessionId)) {
          const r = await review(info, cfg)
          Object.assign(record, {
            policy_decision: 'ask', policy_reason: 'escalate_all', escalated: true,
            reviewer_approved: r.approved, reviewer_reason: r.reason, reviewer_usage: r.usage,
            reviewer_error: r.error, reviewer_latency_ms: r.latencyMs,
          })
          final = r.approved ? 'allow' : 'deny'
        } else {
          Object.assign(record, { policy_decision: 'deny', policy_reason: 'no_budget', escalated: false, budget_exhausted: true })
          final = 'deny'
        }
        record.budget_after = budget.left(info.sessionId)
        record.final_decision = final
        record.gate_latency_ms = Date.now() - started
        log(record)
        if (final === 'allow') return await next()
        return { kind: 'deny', reason: DENY_MESSAGE }
      }

      if (policyVersion === 'v2' || policyVersion === 'v2.1') {
        final = await decideWithV2(info, meta, record)
        record.budget_after = budget.left(info.sessionId)
        record.final_decision = final
        record.gate_latency_ms = Date.now() - started
        log(record)
        if (final === 'allow') return await next()
        return { kind: 'deny', reason: DENY_MESSAGE }
      }

      // ---------------- 以下是 v1 ----------------
      // 2. 调安全门模型（报文结构与 train.py 输入对齐）
      const cls = await classify({
        userTask: info.task,
        history: info.history,
        proposedToolCall: { function: info.toolName, args: info.toolArgs },
        toolMetadata: meta,
      }, cfg)
      Object.assign(record, {
        authorization: cls.authorization,
        risk_level: cls.riskLevel,
        risk_factors: cls.riskFactors,
        rationale: cls.rationale,
        classifier_ok: cls.ok,
        classifier_error: cls.error,
        classifier_usage: cls.usage,
        classifier_latency_ms: cls.latencyMs,
      })

      // 3. 三选一（模型出错时按 on_error 处理）
      const policyDecision = cls.ok ? decide(cls, cfg) : cfg.on_error
      record.policy_decision = policyDecision

      // 4. 处理 ask
      if (policyDecision === 'ask') {
        if (budget.consume(info.sessionId)) {
          // 审核员不看安全门模型的 label / 理由，独立判断
          const r = await review(info, cfg)
          Object.assign(record, {
            escalated: true, reviewer_approved: r.approved, reviewer_reason: r.reason, reviewer_usage: r.usage,
            reviewer_error: r.error, reviewer_latency_ms: r.latencyMs,
          })
          final = r.approved ? 'allow' : 'deny'
        } else {
          Object.assign(record, { escalated: false, budget_exhausted: true })
          final = 'deny'
        }
      } else {
        record.escalated = false
        final = policyDecision === 'allow' ? 'allow' : 'deny'
      }
      record.budget_after = budget.left(info.sessionId)
    } catch (err) {
      // 插件自己出错：按 on_error 处理（ask 在这里也当 deny，避免消耗预算）
      record.plugin_error = String(err?.stack ?? err)
      final = cfg.on_error === 'allow' ? 'allow' : 'deny'
    }

    record.final_decision = final
    record.gate_latency_ms = Date.now() - started
    log(record)

    if (final === 'allow') return await next()
    return { kind: 'deny', reason: DENY_MESSAGE }
  })
}
