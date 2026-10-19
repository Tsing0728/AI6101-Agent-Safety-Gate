// 每个任务（dsh 会话）一个预算计数器：第一次见到时为 k，每问一次人减 1。
// k 设为 null 表示不限次数。
export class Budget {
  constructor(k) {
    this.k = k
    this.remaining = new Map()
  }
  left(sessionId) {
    if (this.k === null || this.k === undefined) return Infinity
    if (!this.remaining.has(sessionId)) this.remaining.set(sessionId, this.k)
    return this.remaining.get(sessionId)
  }
  consume(sessionId) {
    const left = this.left(sessionId)
    if (left <= 0) return false
    if (left !== Infinity) this.remaining.set(sessionId, left - 1)
    return true
  }
}
