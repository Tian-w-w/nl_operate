import type { AgentRunResponse } from '../api-types'

const labels:Record<string,string>={RUNNING:'执行中',SUCCEEDED:'已完成',FAILED:'失败',WAITING_CLARIFICATION:'等待澄清',QUEUED:'已排队',DECIDING:'模型决策中',VALIDATING:'校验决策',TOOL_RUNNING:'工具执行中',OBSERVED:'已保存观察',DECIDED:'已决策',STARTING:'开始执行'}
function record(value:unknown):Record<string,unknown>{return value&&typeof value==='object'?value as Record<string,unknown>:{}}

export function AgentRunPanel({run}:{run:AgentRunResponse}){
  return <section className="agent-run" aria-label="只读Agent任务" aria-live="polite">
    <div className="agent-run-title"><strong>只读 Agent</strong><span className={`run-state ${run.state}`}>{labels[run.state]}</span></div>
    <p>{run.goal}</p><p className="agent-phase">{labels[run.phase]||run.phase} · {run.message}</p>
    <div className="small muted">决策 {run.model_calls}/{run.max_model_calls} · 工具 {run.tool_calls}/{run.max_tool_calls} · {(run.elapsed_ms/1000).toFixed(1)}秒 / {run.max_seconds}秒</div>
    <div className="small muted">输入 {run.input_tokens} / 输出 {run.output_tokens} token · 估算 ${run.estimated_cost_usd}{run.unknown_usage_calls>0&&` · ${run.unknown_usage_calls}次用量待确认`}</div>
    {run.error&&<p role="alert" className="agent-failure">停止原因：{run.error}</p>}
    <ol className="agent-steps">{run.steps.map(step=>{
      const decision=record(step.decision),observation=record(step.observation),output=record(observation.output)
      const title=decision.kind==='call_tool'?String(decision.tool):decision.kind==='finish'?'核对完成依据':decision.kind==='clarify'?'请求澄清':'等待模型决策'
      return <li key={step.id}><div><strong>{step.ordinal}. {title}</strong><span>{labels[step.state]||step.state}</span></div>
        {typeof output.total==='number'&&<p>真实查询：{output.total}条 · 总额 ¥{String(output.total_amount)}</p>}
        {typeof output.row_count==='number'&&<p>文件已生成：{output.row_count}条</p>}
        {step.error&&<small className="agent-failure">{step.error}</small>}
        {step.observation&&<details><summary>工具观察</summary><pre>{JSON.stringify(step.observation,null,2)}</pre></details>}
        {run.state!=='SUCCEEDED'&&observation.status==='SUCCEEDED'&&observation.tool==='export_orders'&&typeof output.download_url==='string'&&<a className="download" href={output.download_url}>下载已生成的文件（任务尚未成功结束）</a>}
      </li>
    })}</ol>
    {run.state==='SUCCEEDED'&&run.export&&<a className="download" href={run.export.download_url}>下载 {run.export.row_count} 笔订单</a>}
    <small className="muted">任务 {run.id.slice(0,12)} · 步骤已保存；澄清后请发起新任务。</small>
  </section>
}
