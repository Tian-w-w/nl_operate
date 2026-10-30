import React, { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { ArrowDownToLine, ArrowRight, BarChart3, BookOpen, Check, ChevronLeft, ChevronRight, ClipboardList, Command, FileCheck2, Layers3, LoaderCircle, MessageSquare, Search, Send, ShieldCheck, Sparkles, X } from 'lucide-react'
import './style.css'
import './workspace.css'
import './theme.css'
import './typography.css'
import { AdvancedFilters } from './components/AdvancedFilters'
import { SavedViews } from './components/SavedViews'
import { OrderDetailDialog } from './components/OrderDetailDialog'
import { AnalyticsPanel } from './components/AnalyticsPanel'
import { AuditPanel } from './components/AuditPanel'
import { AgentRunPanel } from './components/AgentRunPanel'
import { AssistantEvidence, KnowledgeLibrary } from './components/AssistantEvidence'
import type { AgentRunResponse, ConversationMessage, KnowledgeResponse, StatisticResponse } from './api-types'

import type { OrderRow as Order, QueryResponse as Result, PreviewResponse as Preview, AggregateResponse as Aggregate,
  SessionResponse as Session, PlanSummary as Plan, ChatResponse as Reply, Filters_Input as Filters, Sort, Patch, QueryRequest, CallUsage } from './api-types'
import { api, ApiError } from './api'
import { filterLabels, isPreviousWeek, previousWeek, sortControlValue } from './filter-state'

type Message = { role: 'user' | 'assistant'; text: string; state?: string; usage?: CallUsage; download?: { download_url: string; row_count: number }; knowledge?: KnowledgeResponse; statistic?: StatisticResponse }

const labels: Record<string, string> = { paid: '待发货', pending_payment: '待付款', shipped: '已发货', completed: '已完成', cancelled: '已取消' }
const states: Record<string, string> = { SUCCEEDED: '已完成', WAITING_CONFIRMATION: '待确认', WAITING_CLARIFICATION: '待澄清', FAILED: '失败', CANCELLED: '已取消' }
const money = (value: string) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const date = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }) : '—'
const patchLabel = (key: string, value: unknown) => key === 'priority' ? (value ? '优先处理' : '普通优先级') : (value === 'pending_review' ? '待复核' : '正常')

function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [view,setView] = useState<'orders'|'analytics'|'audit'>(() => location.hash === '#analytics' ? 'analytics' : location.hash === '#audit' ? 'audit' : 'orders')
  useEffect(() => { history.replaceState(null,'',`#${view}`) },[view])
  const [busy, setBusy] = useState(false)
  const [assistantMode,setAssistantMode]=useState<'single'|'agent'>('single')
  const [agentRun,setAgentRun]=useState<AgentRunResponse|null>(null)
  const watching=useRef<string|null>(null)
  const [auditOrderId,setAuditOrderId]=useState<number|null>(null)
  const [analyticsRun,setAnalyticsRun]=useState(0)
  const [error, setError] = useState('')
  const [draft, setDraft] = useState<Filters>({})
  const [draftSort, setDraftSort] = useState<Sort[]>([{ field: 'id', direction: 'asc' }])
  const [input, setInput] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [plans, setPlans] = useState<Plan[]>([])
  const [tab, setTab] = useState<'assistant' | 'history' | 'knowledge'>('assistant')
  const [aggregate, setAggregate] = useState<Aggregate | null>(null)
  const [detail, setDetail] = useState<Order | null>(null)
  const [exportLink, setExportLink] = useState('')
  const generation = useRef(0)
  const scroll = useRef<HTMLDivElement>(null)
  const result = session?.current_result
  const preview = session?.preview

  function sync(next: Session) {
    setSession(next)
    if (next.current_result) {
      const query = next.current_result.query
      setDraft({ ...query.filters })
      setDraftSort(query.sort.map(item => ({ ...item })))
    }
  }
  async function refresh() {
    const currentGeneration = generation.current
    const [next, runs,agents,conversation] = await Promise.all([api<Session>('/session'), api<Plan[]>('/plans'),api<AgentRunResponse[]>('/agent/runs'),api<ConversationMessage[]>('/conversation')])
    if (currentGeneration === generation.current) {
      sync(next); setPlans(runs);setAgentRun(agents[0]??null)
      setMessages(conversation.map(item=>({...item,state:item.state??undefined,usage:item.usage??undefined,download:item.download??undefined,knowledge:item.knowledge??undefined,statistic:item.statistic??undefined})))
    }
    return next
  }
  async function work(fn: () => Promise<void>) {
    if (busy) return
    setBusy(true); setError('')
    try { await fn() } catch (e) { setError((e as Error).message); await refresh().catch(() => {}) } finally { setBusy(false) }
  }
  async function login(actor: string) {
    generation.current += 1
    setAgentRun(null);watching.current=null
    setMessages([]); setPlans([]); setAggregate(null); setDetail(null); setExportLink('');setAuditOrderId(null)
    const next = await api<Session>('/auth/demo', { actor })
    await api<Result>('/orders/query', { expected_context_version: next.context_version })
    await refresh()
  }
  useEffect(() => {
    void work(async () => {
      try {
        const next = await api<Session>('/session')
        if (!next.current_result && !next.snapshot_error) await api('/orders/query', { expected_context_version: next.context_version })
        await refresh()
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) await login('operator_a')
        else throw error
      }
    })
  }, [])
  useEffect(() => {
    const conversation = scroll.current?.parentElement
    conversation?.scrollTo({ top: conversation.scrollHeight, behavior: 'smooth' })
  }, [messages, busy])
  useEffect(()=>{
    if(agentRun?.state==='RUNNING'&&!busy&&watching.current!==agentRun.id){
      setAssistantMode('agent');watching.current=agentRun.id
      void work(()=>watchAgent(agentRun.id,generation.current))
    }
  },[agentRun?.id,agentRun?.state,busy])

  async function watchAgent(id:string,identity:number){
    watching.current=id
    while(identity===generation.current){
      const current=await api<AgentRunResponse>(`/agent/runs/${id}`)
      if(identity!==generation.current)return
      setAgentRun(current)
      const next=await api<Session>('/session')
      if(identity!==generation.current)return
      sync(next)
      if(current.result_set_id===next.current_result?.result_set_id)setView('orders')
      if(current.state!=='RUNNING'){
        setMessages(old=>[...old,{role:'assistant',text:current.message,state:current.state}])
        await refresh();return
      }
      await new Promise(resolve=>setTimeout(resolve,350))
    }
  }

  async function query() {
    if (!session) return
    const request: QueryRequest = { filters: draft, sort: draftSort, page_size: result?.query.page_size || 20, expected_context_version: session.context_version }
    await api('/orders/query', request)
    setAggregate(null); setExportLink(''); await refresh()
  }
  async function applyFilters(filters: Filters,current=false) {
    if (!session) return
    if (current) await api('/orders/refine',{ filters, expected_context_version:session.context_version })
    else await api('/orders/query',{ filters, expected_context_version:session.context_version })
    setAggregate(null);setExportLink('');await refresh();setView('orders')
  }
  async function send(text: string) {
    if (!session || !text.trim()) return
    if(assistantMode==='agent'&&!session.agent_available)throw new Error('当前适配器不支持多步决策，请配置真实模型；离线规则仅保留单步功能。')
    setInput(''); setMessages(old => [...old, { role: 'user', text }])
    setAggregate(null); setDetail(null); setExportLink('')
    if(assistantMode==='agent'){
      const run=await api<AgentRunResponse>('/agent/runs',{goal:text,expected_context_version:session.context_version})
      setAgentRun(run);await watchAgent(run.id,generation.current);return
    }
    const reply = await api<Reply>('/assistant', { message: text, expected_context_version: session.context_version })
    setMessages(old => [...old, { role: 'assistant', text: reply.message, state: reply.state, usage: reply.model_usage || undefined, download: reply.export || undefined,knowledge:reply.knowledge??undefined,statistic:reply.statistic??undefined }])
    if (reply.aggregate) setAggregate(reply.aggregate)
    if (reply.detail) setDetail(reply.detail)
    await refresh()
    if (reply.analytics) {setAnalyticsRun(value=>value+1);setView('analytics')}
    else if (reply.audit) {setAuditOrderId(reply.audit_order_id??null);setView('audit')}
    else if (reply.ui_patch) setView('orders')
  }
  async function select(id: number) {
    if (!session) return
    const ids = session.selected_ids.includes(id) ? session.selected_ids.filter(i => i !== id) : [...session.selected_ids, id]
    await api('/session/selection', { ids, expected_context_version: session.context_version }); await refresh()
  }
  async function mark(patch: Patch) {
    if (!result || !session) return
    await api('/previews', { result_set_id: result.result_set_id, patch, scope: session.selected_ids.length ? 'selected' : 'all', expected_context_version: session.context_version })
    await refresh()
  }
  async function confirm() {
    if (!preview) return
    await api(`/previews/${preview.preview_id}/confirm`, { digest: preview.digest })
    const operation = await api<{ affected_ids: number[]; operation_id: string }>(`/previews/${preview.preview_id}/apply`, { idempotency_key: `ui-${preview.preview_id}` })
    await refresh()
    if (!preview.parent_plan_id) setMessages(old => [...old, { role: 'assistant', text: `已更新 ${operation.affected_ids.length} 条订单；操作编号 ${operation.operation_id.slice(0, 8)}。`, state: 'SUCCEEDED' }])
  }
  async function exportRows() {
    if (!result) return
    const item = await api<{ download_url: string }>('/exports', { result_set_id: result.result_set_id, idempotency_key: crypto.randomUUID() })
    setExportLink(item.download_url)
  }
  async function page(delta: number) {
    if (!result || !session) return
    await api(`/results/${result.result_set_id}/page`, { page: result.query.page + delta, expected_context_version: session.context_version })
    await refresh()
  }

  return <div className="app">
    <nav className="rail">
      <a className="brand" href="/" aria-label="订单管理平台首页"><Layers3 size={24}/><span>W</span></a>
      <div className="rail-items"><button className={view==='orders'?'rail-active':''} title="订单工作台" aria-label="打开订单工作台" disabled={!session||busy} onClick={()=>setView('orders')}><ClipboardList size={21}/></button><button className={view==='audit'?'rail-active':''} title="操作审计" aria-label="打开操作审计" disabled={!session||busy} onClick={()=>setView('audit')}><FileCheck2 size={21}/></button><button className={view==='analytics'?'rail-active':''} title="统计看板" aria-label="打开统计看板" disabled={!session||busy} onClick={()=>setView('analytics')}><BarChart3 size={21}/></button></div>
      <div className="rail-footer" title="本地合成数据环境"><ShieldCheck size={21}/><span>LAB</span></div>
    </nav>
    <div className="workspace">
      <header className="topbar"><div className="breadcrumb"><strong className="platform-name">订单管理平台</strong><ChevronRight size={14}/><span>{view==='orders'?'订单列表':view==='analytics'?'统计看板':'操作审计'}</span></div><div className="top-right"><span className="demo-dot"/>合成数据环境<span className="divider"/><select aria-label="演示身份" value={session?.actor || 'operator_a'} disabled={busy} onChange={e => void work(() => login(e.target.value))}><option value="operator_a">租户 A · 操作员</option><option value="viewer_a">租户 A · 只读</option><option value="operator_b">租户 B · 操作员</option></select><div className="avatar">{session?.tenant.toUpperCase() || 'A'}</div></div></header>
      <div className="body">
        <main>
          <div className="heading"><div><div className="eyebrow">OPERATIONS / 订单运营</div><h1>{view==='orders'?'订单工作台':view==='analytics'?'统计与洞察':'操作审计'}<span className="version">v0.5</span></h1><p>从筛选到处理，每一步都有据可查。</p></div>{view==='orders'&&<button className="button secondary" disabled={!result || busy} onClick={() => void work(exportRows)}><ArrowDownToLine size={16}/>导出当前结果</button>}</div>
          <div className="workspace-tabs"><button className={view==='orders'?'active':''} disabled={!session||busy} onClick={()=>setView('orders')}>订单列表</button><button className={view==='analytics'?'active':''} disabled={!session||busy} onClick={()=>setView('analytics')}>统计看板</button><button className={view==='audit'?'active':''} disabled={!session||busy} onClick={()=>setView('audit')}>操作审计</button></div><div className="reference"><span className="tag"><span className="demo-dot"/>固定实验时钟</span><span>{session ? new Date(session.reference_time).toLocaleString('sv-SE', { timeZone: 'Asia/Shanghai', hour12: false }).slice(0,16) : '—'} · Asia/Shanghai</span><span className="reference-note">所有时间条件基于此刻计算</span></div>
          {error && <div className="error global-error" role="alert"><X size={15}/>{error}<button onClick={()=>setError('')}>关闭</button></div>}
          {view==='orders'&&<>{session&&<SavedViews key={`${session.tenant}:${session.actor}`} result={result} version={session.context_version} disabled={busy} onApply={id=>work(async()=>{await api(`/saved-views/${id}/apply`,{expected_context_version:session.context_version});setAggregate(null);setExportLink('');await refresh()})}/>}
          <div className="quick-views"><span>快捷工作队列</span>{[{name:'待发货',filters:{status:'paid'}},{name:'超48小时',filters:{waiting_hours_gt:48}},{name:'优先处理',filters:{priority:true}},{name:'待复核',filters:{review_status:'pending_review'}}].map(item=><button key={item.name} disabled={!session||busy} onClick={()=>void work(()=>applyFilters(item.filters as Filters))}>{item.name}</button>)}</div>
          <div className="metrics"><div><span>当前结果</span><strong>{result?.total.toLocaleString() ?? '—'}<small>笔订单</small></strong><div>完整筛选范围，包含所有分页</div></div><div><span>结果集总金额</span><strong><small>¥</small>{result ? money(result.total_amount) : '—'}</strong><div>人民币 CNY · 订单金额快照</div></div><div><span>当前已选</span><strong>{session?.selected_ids.length ?? 0}<small>笔订单</small></strong><div>勾选范围与完整结果集独立</div></div></div>
          <section className="orders-panel"><div className="section-head"><h2><ClipboardList size={17}/>订单列表</h2><span className="small muted">{result ? `结果集 ${result.result_set_id.slice(0, 8)}` : '等待查询'}</span></div>
            <form onSubmit={e => { e.preventDefault(); void work(query) }} className="filters"><label>收货城市<select aria-label="收货城市" value={draft.shipping_city || ''} onChange={e => setDraft(old => ({ ...old, shipping_city: e.target.value || null, shipping_cities:null }))}><option value="">全部城市</option>{['上海','北京','杭州','深圳','成都','广州'].map(c => <option key={c}>{c}</option>)}</select></label><label>订单状态<select aria-label="订单状态" value={draft.status || ''} onChange={e => setDraft(old => ({ ...old, status: (e.target.value || null) as Filters['status'], statuses:null }))}><option value="">全部状态</option>{Object.entries(labels).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label><label>排序方式<select aria-label="排序方式" value={sortControlValue(draftSort)} onChange={e => setDraftSort(e.target.value === 'id' ? [{ field: 'id', direction: 'asc' }] : [{ field: 'total_amount', direction: e.target.value as 'asc' | 'desc' }, { field: 'id', direction: 'asc' }])}><option value="id">订单编号升序</option>{sortControlValue(draftSort) === 'custom' && <option value="custom">保持当前排序</option>}<option value="desc">金额从高到低</option><option value="asc">金额从低到高</option></select></label><button className="button primary" disabled={busy}><Search size={15}/>筛选</button><div className="extra-filters"><label className="check-label"><input type="checkbox" checked={Boolean(session && isPreviousWeek(draft, session.reference_time))} onChange={e => setDraft(old => e.target.checked && session ? { ...old, ...previousWeek(session.reference_time) } : { ...old, created_at_gte: null, created_at_lt: null })}/>上周创建</label><label className="amount-label">等待发货 &gt; <input aria-label="等待发货小时数" type="number" min="0" max="87600" step="1" value={draft.waiting_hours_gt ?? ''} onChange={e => setDraft(old => ({ ...old, waiting_hours_gt: e.target.value === '' ? null : Number(e.target.value) }))} placeholder="不限"/> 小时</label><label className="amount-label">金额 ≥ <input aria-label="金额下限" type="number" step="0.01" min="0" value={draft.amount_gte ?? ''} onChange={e => setDraft(old => ({ ...old, amount_gte: e.target.value === '' ? null : e.target.value }))} placeholder="不限"/> 元</label><button type="button" className="clear-filters" onClick={() => { setDraft({}); setDraftSort([{ field: 'id', direction: 'asc' }]) }}>清空条件</button></div><div className="filter-summary" aria-label="完整筛选条件">{Object.entries(draft).filter(([,v]) => v !== null && v !== undefined).map(([key,value]) => <span key={key}>{filterLabels[key as keyof Filters]}：{typeof value === 'boolean' ? (value ? '是' : '否') : String(value)}</span>)}</div><AdvancedFilters value={draft} onChange={setDraft}/></form>
            <div className="batchbar"><span>{session?.selected_ids.length ? `已选 ${session.selected_ids.length} 笔` : '未勾选时，标记作用于完整结果集'}</span><div><button disabled={busy || !result || session?.role !== 'operator'} onClick={() => void work(() => mark({ priority: true }))}>标记优先处理</button><button disabled={busy || !result || session?.role !== 'operator'} onClick={() => void work(() => mark({ review_status: 'pending_review' }))}>标记待复核</button></div></div>
            {session?.snapshot_error && <div className="error" role="alert">{session.snapshot_error.message}，请点击筛选刷新。</div>}
            {exportLink && <div className="success"><Check size={15}/>文件已生成，<a href={exportLink}>下载 CSV 清单</a>（完整结果集）</div>}
            <div className="table-wrap"><table><thead><tr><th className="checkbox-col">选择</th><th>订单编号</th><th>收货城市</th><th>订单状态</th><th className="number">金额 / ¥</th><th>付款时间</th><th>处理标记</th></tr></thead><tbody>{result?.rows.map(o => <tr key={o.id}><td><input aria-label={`选择订单${o.id}`} type="checkbox" disabled={busy} checked={session?.selected_ids.includes(o.id) || false} onChange={() => void work(() => select(o.id))}/></td><td><button className="order-link" onClick={() => void work(async () => setDetail(await api(`/orders/${o.id}`)))}>#{String(o.id).padStart(6, '0')}</button><span className="row-version">v{o.version}</span></td><td>{o.shipping_city}</td><td><span className={`status ${o.status}`}><i/>{labels[o.status]}</span></td><td className="number">{money(o.total_amount)}</td><td className="date">{date(o.paid_at)}</td><td>{o.priority && <span className="priority">优先</span>}{o.review_status === 'pending_review' && <span className="review">待复核</span>}{!o.priority && o.review_status === 'normal' && <span className="muted">—</span>}</td></tr>)}</tbody></table>{result && !result.rows.length && <div className="empty"><Search size={26}/><strong>没有符合条件的订单</strong><span>试试调整城市、状态或时间条件。</span></div>}</div>
            <div className="pagination"><span>共 {result?.total ?? 0} 条 · 每页 {result?.query.page_size ?? 20} 条</span><div><button aria-label="上一页" disabled={busy || !result || result.query.page <= 1} onClick={() => void work(() => page(-1))}><ChevronLeft size={15}/></button><span>{result?.query.page ?? 1} / {Math.max(1, Math.ceil((result?.total ?? 0) / (result?.query.page_size ?? 20)))}</span><button aria-label="下一页" disabled={busy || !result || result.query.page * result.query.page_size >= result.total} onClick={() => void work(() => page(1))}><ChevronRight size={15}/></button></div></div>
          </section>
          {aggregate && <section className="analysis-card"><div className="section-head"><h2><BarChart3 size={18}/>当前结果统计</h2><button className="icon-button" aria-label="关闭统计" onClick={() => setAggregate(null)}><X size={16}/></button></div><div className="aggregate-list">{aggregate.groups.map(g => <div key={g.group}><span>{labels[g.group] || g.group}</span><div className="bar"><i style={{ width: `${g.count / Math.max(...aggregate.groups.map(x => x.count), 1) * 100}%` }}/></div><strong>{g.count} 笔</strong><span>¥{money(g.total_amount)}</span></div>)}</div><p className="small muted">{aggregate.basis}</p></section>}
          </>}
          {view==='analytics'&&session&&<AnalyticsPanel key={`${session.tenant}:${session.actor}:${analyticsRun}`} resultId={result?.result_set_id} disabled={busy} onDrill={(filters,current)=>work(()=>applyFilters(filters,current))}/>}
          {view==='audit'&&session&&<AuditPanel key={`${session.tenant}:${session.actor}:${auditOrderId}`} initialOrderId={auditOrderId} onDetail={id=>work(async()=>setDetail(await api(`/orders/${id}`)))}/>}
          <footer className="main-footer"><ShieldCheck size={14}/>本地研究原型 · 合成订单 · 修改须确认<span>LLM4Web / Research workspace</span></footer>
        </main>
        <aside className="assistant"><div className="assistant-head"><div className="assistant-icon"><Sparkles size={20}/></div><div><h2>操作助手</h2><span><span className="demo-dot"/>{session?.adapter === 'rules-v1-offline' ? '离线规则模式 · 无模型调用' : `模型：${session?.adapter || '加载中'}`}</span></div></div><div className="agent-mode"><label>执行模式<select aria-label="助手执行模式" value={assistantMode} disabled={busy} onChange={e=>setAssistantMode(e.target.value as 'single'|'agent')}><option value="single">单步操作</option><option value="agent">只读 Agent · 多步决策</option></select></label>{assistantMode==='agent'&&!session?.agent_available&&<small>离线规则不提供多步决策。配置模型后可执行查询、条件判断与导出。</small>}</div><div className="assistant-context">当前操作对象：订单列表的 {result?.total ?? 0} 笔订单{view!=='orders'&&<small>看板范围独立设置；点击分组可切换到对应订单。</small>}</div><div className="assistant-tabs"><button className={tab === 'assistant' ? 'active' : ''} onClick={() => setTab('assistant')}><MessageSquare size={14}/>助手</button><button className={tab === 'history' ? 'active' : ''} onClick={() => setTab('history')}><FileCheck2 size={14}/>执行记录 <small>{plans.length}</small></button><button className={tab === 'knowledge' ? 'active' : ''} onClick={() => setTab('knowledge')}><BookOpen size={14}/>知识库</button></div>
          {tab === 'assistant' ? <><div className="conversation">{assistantMode==='agent'&&agentRun&&<AgentRunPanel run={agentRun}/>}<div className="welcome"><div className="welcome-label"><Command size={16}/> 用一句话，推进工作</div><p>可以查询订单、继续调整条件、统计城市数量，或询问发货与快递规则。</p><div className="suggestions">{['找出上周已付款但未发货的上海订单，按金额从高到低展示', '其中等待超过48小时的标为优先处理', '导出这些订单', '按城市统计这些订单'].map((text, i) => <button key={text} disabled={busy} onClick={() => void work(() => send(text))}><span className="suggestion-num">0{i + 1}</span>{text}<ArrowRight size={14}/></button>)}</div><div className="offline-note">{session?.adapter === 'rules-v1-offline' ? '当前使用有限规则验证业务流程。未识别的表达会请求澄清。' : '模型只提出受限操作计划；修改仍需在页面检查并确认。'}</div></div>{messages.map((m, i) => <div className={`message from-${m.role}`} key={i}><div className="message-label">{m.role === 'user' ? '你' : '操作助手'}{m.state && <span className={`run-state ${m.state}`}>{states[m.state] || m.state}</span>}</div><p>{m.text}</p><AssistantEvidence knowledge={m.knowledge} statistic={m.statistic}/>{m.usage && <div className="call-usage">{m.usage.real_model || m.usage.billable ? `模型 ${m.usage.duration_ms} ms · 输入 ${m.usage.input_tokens ?? '未知'} / 输出 ${m.usage.output_tokens ?? '未知'} token · ${m.usage.estimated_cost_usd === null ? '费用待确认，保留预算占用' : `估算 $${m.usage.estimated_cost_usd}`}` : `离线解析 ${m.usage.duration_ms} ms`}<span title={m.usage.request_id}> · 请求 {m.usage.request_id.slice(0,8)}</span></div>}{m.download && <a className="download" href={m.download.download_url}><ArrowDownToLine size={14}/>下载 {m.download.row_count} 笔订单</a>}</div>)}{busy && <div className="working"><LoaderCircle size={15} className="spin"/>正在读取实际状态…</div>}<div ref={scroll}/></div><form className="composer" onSubmit={e => { e.preventDefault(); void work(() => send(input)) }}><textarea aria-label="输入业务指令" placeholder="例如：找出上海已付款未发货的订单" value={input} disabled={busy} onChange={e => setInput(e.target.value)} onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!busy && input.trim()) void work(() => send(input)) } }}/><div><span>Enter 发送 · Shift + Enter 换行</span><button aria-label="发送" disabled={busy || !input.trim()}><Send size={16}/></button></div></form><div className="assistant-foot"><ShieldCheck size={12}/>操作结果以业务接口和页面实际状态为准</div></> : tab==='knowledge' ? <KnowledgeLibrary key={`${session?.tenant}:${session?.actor}`}/> : <div className="history">{plans.length === 0 && <p className="muted">还没有执行记录。发送指令后可查看。</p>}{plans.map(p => <div className="history-item" key={p.id}><div><span className={`run-state ${p.state}`}>{states[p.state] || p.state}</span><span className="small muted">{date(p.created_at)}</span></div><p>{p.instruction}</p><code>{p.id.slice(0, 12)} · {p.intent}</code>{typeof p.result?.message === 'string' && <small>{p.result.message}</small>}</div>)}</div>}
        </aside>
      </div>
    </div>
    {preview && <div className="modal-backdrop"><section className="modal" role="dialog" aria-modal="true" aria-labelledby="preview-title"><div className="modal-title"><div className="confirm-icon"><ShieldCheck size={22}/></div><div><div className="eyebrow">REVIEW & CONFIRM</div><h2 id="preview-title">确认修改 {preview.count} 笔订单</h2></div></div><p>请检查全部目标订单及字段变化。只有点击下方确认按钮才会提交。</p><div className="preview-table"><table><thead><tr><th>订单</th><th>修改前</th><th>修改后</th></tr></thead><tbody>{preview.changes.map(c => <tr key={c.id}><td>#{String(c.id).padStart(6,'0')}</td><td>{Object.entries(c.before).map(([k,v]) => patchLabel(k,v)).join('、')}</td><td><span className="priority">{Object.entries(c.after).map(([k,v]) => patchLabel(k,v)).join('、')}</span></td></tr>)}</tbody></table></div><div className="preview-meta">预览有效至 {date(preview.expires_at)} · 摘要 {preview.digest.slice(0,12)}</div>{error && <div role="alert" className="error">{error}</div>}<div className="modal-actions"><button className="button secondary" disabled={busy} onClick={() => void work(async () => { await api(`/previews/${preview.preview_id}/cancel`, {}); await refresh() })}>取消修改</button><button className="button primary" disabled={busy} onClick={() => void work(confirm)}>{busy ? <LoaderCircle size={16} className="spin"/> : <Check size={16}/>}确认并提交</button></div></section></div>}
    {detail && <OrderDetailDialog key={detail.id} id={detail.id} onClose={()=>setDetail(null)}/>}
  </div>
}

createRoot(document.getElementById('root')!).render(<App/>)
