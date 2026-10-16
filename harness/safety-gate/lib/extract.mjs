// 从 dsh 的 tools/pre-execute payload 里取出模型需要的信息。
// 依据 dsh-probe（dsh 0.2.0-rc.2）：
//   payload.name / payload.arguments / payload.callId
//   payload.agent.session.deriveMessages() -> 完整消息列表
//   消息的 source.kind === 'user' 是用户的输入

import { DENY_MESSAGE } from './deny.mjs'

// 去掉 MCP 前缀，例如 "mcp__agentdojo__send_money" -> "send_money"
export function normalizeToolName(name, strip = true) {
  if (!strip || typeof name !== 'string') return name
  const i = name.lastIndexOf('__')
  return i >= 0 ? name.slice(i + 2) : name
}

function textOf(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter(c => c && c.type === 'text' && typeof c.text === 'string').map(c => c.text).join('\n')
}

// 找出所有已经有返回结果的工具调用：toolCallId -> 返回的文字
// 两种格式都认：
//   npm dsh 0.2.0-rc.2：{ role: 'tool', toolCallId, content: [{ type: 'text', text }] }
//   SDK dsh 0.1.5rc1  ：content 里有 { type: 'tool-result', toolCallId, content: [{ type: 'text', text }] }
function collectResults(messages) {
  const results = new Map()
  for (const m of messages) {
    if (m?.role === 'tool' && m.toolCallId) results.set(m.toolCallId, textOf(m.content))
    if (!Array.isArray(m?.content)) continue
    for (const block of m.content) {
      if (block?.type === 'tool-result' && block.toolCallId) results.set(block.toolCallId, textOf(block.content))
    }
  }
  return results
}

function parseArgs(args) {
  if (typeof args !== 'string') return args ?? {}
  try { return JSON.parse(args) } catch { return args }
}

export function getMessages(payload) {
  const session = payload?.agent?.session
  const msgs = session?.deriveMessages?.()
  return Array.isArray(msgs) ? msgs : []
}

export function getSessionId(payload) {
  return payload?.agent?.session?.header?.id ?? payload?.agent?.id ?? 'unknown-session'
}

// 用户任务：第一条真正由用户发的消息
export function getTask(messages) {
  const m = messages.find(m => m?.role === 'user' && m?.source?.kind === 'user')
  return m ? textOf(m.content) : ''
}

// trace：当前这次调用之前，agent 发起过的所有工具调用（按时间顺序）
// 和训练数据（MCP 服务记录的 trace.jsonl）保持一致，只保留：
//   - AgentDojo 的工具：名字以 gatedPrefix 开头（dsh 自带工具不经过 MCP，训练数据里没有）
//   - 真正执行了的调用：被本插件拒绝的到不了 MCP，训练数据里也没有
//   - 已经有返回结果的调用：同一轮并行发起、还没执行的调用不算（执行出错的算，因为已经到过 MCP）
export function getTrace(messages, currentCallId, strip = true, gatedPrefix = '') {
  // 先找出有结果的调用，再从中去掉被本插件拒绝的（返回文字里带有 DENY_MESSAGE）
  const results = collectResults(messages)
  const executed = id => results.has(id) && !results.get(id).includes(DENY_MESSAGE)

  const trace = []
  for (const m of messages) {
    if (m?.role !== 'assistant' || !Array.isArray(m.content)) continue
    for (const block of m.content) {
      if (block?.type !== 'tool-call' || block.id === currentCallId) continue
      if (gatedPrefix && !(typeof block.name === 'string' && block.name.startsWith(gatedPrefix))) continue
      if (!executed(block.id)) continue
      trace.push({ function: normalizeToolName(block.name, strip), args: parseArgs(block.arguments) })
    }
  }
  return trace
}

export function extract(payload, cfg) {
  const strip = cfg.strip_tool_name_prefix !== false
  const messages = getMessages(payload)
  return {
    sessionId: getSessionId(payload),
    callId: payload?.callId,
    rawToolName: payload?.name,
    toolName: normalizeToolName(payload?.name, strip),
    toolArgs: parseArgs(payload?.arguments),
    task: getTask(messages),
    trace: getTrace(messages, payload?.callId, strip, cfg.gated_tool_prefix),
  }
}
