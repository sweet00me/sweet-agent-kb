import { Buffer } from 'node:buffer'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// 执行真实 TypeScript 服务；只做类型擦除，不替换服务实现。
export async function moduleUrl(path, replacements = {}) {
  let source = await readFile(new URL(path, import.meta.url), 'utf8')
  for (const [from, to] of Object.entries(replacements)) source = source.replace(from, to)
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext },
  })
  return 'data:text/javascript;base64,' + Buffer.from(outputText).toString('base64')
}

const apiUrl = await moduleUrl('../src/services/api.ts')
const sseUrl = await moduleUrl('../src/services/sse.ts', { "from './api'": `from '${apiUrl}'` })
export const { postSse } = await import(sseUrl)
export const agentStreamUrl = await moduleUrl('../src/services/agentStream.ts', {
  "from './sse'": `from '${sseUrl}'`,
})
export const { runAgentStream } = await import(agentStreamUrl)
