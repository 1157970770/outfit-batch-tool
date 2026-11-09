import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { build } from 'vite'

// Windows 下 esbuild 清理系统 TEMP 偶发 Access denied；统一改用项目内临时目录。
const tmp = resolve(import.meta.dirname, '.tmp')
mkdirSync(tmp, { recursive: true })
process.env.TEMP = tmp
process.env.TMP = tmp

await build()
