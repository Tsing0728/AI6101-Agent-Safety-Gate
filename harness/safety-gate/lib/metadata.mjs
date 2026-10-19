// 工具对照表：tool_metadata.json
// 同名工具在不同 suite 里标注一致，所以按工具名直接查。
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { PLUGIN_DIR } from './config.mjs'

const table = {}
const raw = JSON.parse(readFileSync(join(PLUGIN_DIR, 'tool_metadata.json'), 'utf-8'))
for (const suite of Object.values(raw)) {
  for (const [name, meta] of Object.entries(suite)) {
    table[name] = { read_only: meta.read_only, touches_remote: meta.touches_remote }
  }
}

// 返回 { meta, found }。表里没有的工具暂按"不是只读、会影响外部"处理，并在日志里标出来。
export function lookupMetadata(toolName) {
  if (Object.hasOwn(table, toolName)) return { meta: table[toolName], found: true }
  return { meta: { read_only: false, touches_remote: true }, found: false }
}
