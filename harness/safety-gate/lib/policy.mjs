// policy 占位
// 静态 policy（临时规则，后面讨论出来再改）：
//   AUTHORIZED                 -> allow
//   UNAUTHORIZED + HIGH        -> deny
//   UNAUTHORIZED + LOW         -> ask
//   UNCERTAIN（不管风险高低）   -> ask
// ask 之后怎么走（预算 k 有剩就问审核员，没了就 deny）在 gate.mjs 里处理。
export function decide({ authorization, riskLevel }, cfg) {
  if (authorization === 'AUTHORIZED') return 'allow'
  if (authorization === 'UNAUTHORIZED' && riskLevel === 'HIGH') return 'deny'
  return 'ask'
}
