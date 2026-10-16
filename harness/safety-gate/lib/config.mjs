// 读取 config.json（和 gate.mjs 同目录）。也可以用环境变量 GATE_CONFIG 指定别的配置文件。
import { readFileSync } from 'node:fs'
import { join, dirname, isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'

export const PLUGIN_DIR = join(dirname(fileURLToPath(import.meta.url)), '..')

export function loadConfig() {
  const path = process.env.GATE_CONFIG || join(PLUGIN_DIR, 'config.json')
  const cfg = JSON.parse(readFileSync(path, 'utf-8'))
  // log_dir 如果是相对路径，就相对插件目录
  if (cfg.log_dir && !isAbsolute(cfg.log_dir)) cfg.log_dir = join(PLUGIN_DIR, cfg.log_dir)
  return cfg
}
