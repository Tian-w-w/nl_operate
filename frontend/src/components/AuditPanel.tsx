import { useEffect, useState } from 'react'
import { ChevronLeft, ChevronRight, Search } from 'lucide-react'
import type { AuditQuery, AuditResponse } from '../api-types'
import { api } from '../api'
import { changeLabel, fullDate } from '../format'

export function AuditPanel({onDetail,initialOrderId}:{onDetail:(id:number)=>Promise<void>;initialOrderId:number|null}) {
  const [order,setOrder]=useState(initialOrderId ? String(initialOrderId) : '')
  const [actor,setActor]=useState('')
  const [query,setQuery]=useState<AuditQuery>({page:1,page_size:15,order_id:initialOrderId})
  const [data,setData]=useState<AuditResponse|null>(null)
  const [error,setError]=useState('')
  const [loading,setLoading]=useState(false)
  useEffect(()=>{let active=true;setLoading(true);setError('');void api<AuditResponse>('/audit/query',query)
    .then(value=>{if(active)setData(value)}).catch(e=>{if(active)setError(e.message)}).finally(()=>{if(active)setLoading(false)})
    return()=>{active=false}
  },[query])
  return <section className="audit-workspace"><div className="workspace-page-head"><div><div className="eyebrow">OPERATION AUDIT</div><h2>操作审计</h2><p>查看当前租户的真实字段修改记录，包含操作者、修改前后值及关联操作。</p></div></div>
    <form className="audit-filters" onSubmit={e=>{e.preventDefault();setQuery({page:1,page_size:15,order_id:order?Number(order):null,actor_id:actor||null})}}><label>订单编号<input aria-label="审计订单编号" type="number" min="1" value={order} onChange={e=>setOrder(e.target.value)} placeholder="全部订单"/></label><label>操作者<input aria-label="审计操作者" value={actor} onChange={e=>setActor(e.target.value)} placeholder="例如 operator_a"/></label><button className="button primary" disabled={loading}><Search size={14}/>查询记录</button></form>
    {error&&<div className="error" role="alert">{error}</div>}<div className="audit-summary">共 {data?.total ?? 0} 条订单变更记录 <span>时间采用 Asia/Shanghai</span></div>
    <div className="audit-feed">{data?.rows.map(entry=><article key={entry.id}><div className="audit-entry-head"><button className="order-link" onClick={()=>void onDetail(entry.order_id)}>#{String(entry.order_id).padStart(6,'0')}</button><span>{entry.actor_id}</span><time>{fullDate(entry.created_at)}</time></div><div className="audit-changes">{Object.entries(entry.after).map(([key,value])=><div key={key}><span>{key==='priority'?'优先级':'复核状态'}</span><del>{changeLabel(key,entry.before[key])}</del><span>→</span><strong>{changeLabel(key,value)}</strong></div>)}</div><small>关联操作 {entry.operation_id}</small></article>)}{!loading&&data?.total===0&&<div className="empty"><strong>暂无匹配的修改记录</strong><span>完成一次预览确认后的订单标记，即可在此查看。</span></div>}{loading&&<p className="panel-loading">正在读取审计记录…</p>}</div>
    <div className="pagination"><span>每页 {data?.page_size||15} 条</span><div><button aria-label="上一页审计" disabled={loading||!data||data.page<=1} onClick={()=>setQuery(old=>({...old,page:(data?.page||1)-1}))}><ChevronLeft size={15}/></button><span>{data?.page||1} / {Math.max(1,Math.ceil((data?.total||0)/(data?.page_size||15)))}</span><button aria-label="下一页审计" disabled={loading||!data||data.page*data.page_size>=data.total} onClick={()=>setQuery(old=>({...old,page:(data?.page||1)+1}))}><ChevronRight size={15}/></button></div></div>
  </section>
}
