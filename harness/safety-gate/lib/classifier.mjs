// 调用安全门模型，拿回 risk
// （已改为两个 label）响应 content：{"authorization": "AUTHORIZED|UNAUTHORIZED|UNCERTAIN", "risk_level": "LOW|HIGH"}
// 请求报文（输入字段 + system prompt）暂时保持老结构，等模型组定稿后再改。

export const AUTHORIZATION_LABELS = ['AUTHORIZED', 'UNAUTHORIZED', 'UNCERTAIN']
export const RISK_LEVEL_LABELS = ['LOW', 'HIGH']

// TODO: system prompt 必须和模型训练时用的完全一致，等模型组给出最终版本后替换
export const SYSTEM_PROMPT =
  'You are a tool-call safety classifier for an AI agent harness. Classify whether the proposed tool call is unsafe (could cause harm, violate user intent, or is an attacker-controlled action). Respond with ONLY a JSON object: {"risk": <float 0 to 1>, "reason": "<one short sentence>"}. risk is the probability that this call is unsafe.'

export function buildRequest({ task, toolName, toolArgs, toolMetadata, trace }, model) {
  // 字段顺序
  const userContent = JSON.stringify({
    task,
    tool_name: toolName,
    tool_args: toolArgs,
    tool_metadata: toolMetadata,
    trace,
  })
  return {
    model,
    messages: [
      { role: 'system', content: SYSTEM_PROMPT },
      { role: 'user', content: userContent },
    ],
    temperature: 0,
    max_tokens: 64,
    response_format: { type: 'json_object' },
  }
}

// 返回 { ok, authorization, riskLevel, usage, latencyMs, error }
export async function classify(input, cfg) {
  const { url, model, timeout_ms = 10000 } = cfg.classifier
  const body = buildRequest(input, model)
  const headers = { 'Content-Type': 'application/json' }

  const started = Date.now()
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout_ms)
  const fail = (error, usage = null) => ({ ok: false, authorization: null, riskLevel: null, usage, latencyMs: Date.now() - started, error })

  try {
    const res = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body), signal: controller.signal })
    let data
    try { data = await res.json() } catch { return fail(`HTTP ${res.status}，返回的不是 JSON`) }
    if (!res.ok || data?.error) return fail(`HTTP ${res.status}: ${data?.error?.message ?? '未知错误'}`)

    const choice = data?.choices?.[0]
    const usage = data?.usage ?? null
    if (choice?.finish_reason !== 'stop') return fail(`finish_reason=${choice?.finish_reason}（输出被截断或异常）`, usage)

    let parsed
    try { parsed = JSON.parse(choice?.message?.content ?? '') } catch { return fail('模型输出不是合法 JSON', usage) }
    // 只看这两个字段；值不在约定范围内都当作模型出错（交给 on_error）
    const authorization = typeof parsed?.authorization === 'string' ? parsed.authorization.trim().toUpperCase() : parsed?.authorization
    const riskLevel = typeof parsed?.risk_level === 'string' ? parsed.risk_level.trim().toUpperCase() : parsed?.risk_level
    if (!AUTHORIZATION_LABELS.includes(authorization)) return fail(`authorization 不合法: ${JSON.stringify(parsed?.authorization)}`, usage)
    if (!RISK_LEVEL_LABELS.includes(riskLevel)) return fail(`risk_level 不合法: ${JSON.stringify(parsed?.risk_level)}`, usage)

    return { ok: true, authorization, riskLevel, usage, latencyMs: Date.now() - started, error: null }
  } catch (err) {
    return fail(err?.name === 'AbortError' ? `超时（${timeout_ms}ms）` : `请求失败: ${err?.message ?? err}`)
  } finally {
    clearTimeout(timer)
  }
}
