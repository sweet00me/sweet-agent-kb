
/**
 * 文件：src/pages/RecordsPage.tsx
 *
 * 用途：
 *   Sweet Research Agent 执行记录页面。
 *
 * 未来功能：
 *   1. 查看 Agent 节点级执行轨迹。
 *   2. 查看工具调用记录。
 *   3. 查看人工审批及审计记录。
 *   4. 查看运行耗时、异常及指标统计。
 *
 * 当前阶段：
 *   仅实现页面结构，尚未连接运行记录 API。
 */

import {
  Activity,
  Workflow,
  Wrench,
  ShieldCheck,
  ChartNoAxesCombined,
} from 'lucide-react'

const recordSections = [
  {
    title: '节点执行轨迹',
    description: '查看 LangGraph 节点的执行顺序、状态和耗时。',
    Icon: Workflow,
  },
  {
    title: '工具调用记录',
    description: '查看 Agent 调用了哪些工具及执行结果。',
    Icon: Wrench,
  },
  {
    title: '审批与审计',
    description: '查看工具操作审批、拒绝及相关审计事件。',
    Icon: ShieldCheck,
  },
  {
    title: '运行指标',
    description: '查看响应耗时、调用统计和异常信息。',
    Icon: ChartNoAxesCombined,
  },
]

export function RecordsPage() {
  return (
    <section className="min-h-0 flex-1 overflow-y-auto px-4 py-6">

      <div className="mx-auto w-full max-w-3xl">

        {/* 页面标题 */}
        <div className="mb-7">

          <div className="mb-3 flex items-center gap-3">
            <Activity size={26} />

            <h1 className="text-2xl font-semibold">
              执行记录
            </h1>
          </div>

          <p className="text-sm leading-7 text-slate-500">
            查看 Agent 的执行过程、工具调用及运行状态。
          </p>

        </div>

        {/* 功能区域 */}
        <div className="space-y-3">

          {recordSections.map(({ title, description, Icon }) => (
            <div
              key={title}
              className="flex items-start gap-4 rounded-2xl border border-slate-200 bg-white p-4"
            >

              <div className="rounded-xl bg-slate-100 p-3">
                <Icon size={21} />
              </div>

              <div className="min-w-0 flex-1">

                <h2 className="font-semibold">
                  {title}
                </h2>

                <p className="mt-1 text-sm leading-6 text-slate-500">
                  {description}
                </p>

              </div>

            </div>
          ))}

        </div>

        <p className="mt-6 text-center text-xs text-slate-400">
          页面框架已就绪 · Audit / Tracing / Metrics API 待接入
        </p>

      </div>
    </section>
  )
}
