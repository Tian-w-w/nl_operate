import { useEffect, useState } from 'react'
import { Package, Truck, X } from 'lucide-react'
import type { OrderDetail } from '../api-types'
import { api } from '../api'
import { changeLabel, fullDate, money, statusLabels } from '../format'

export function OrderDetailDialog({id,onClose}:{id:number;onClose:()=>void}) {
  const [detail,setDetail]=useState<OrderDetail|null>(null)
  const [error,setError]=useState('')
  const [tab,setTab]=useState<'detail'|'audit'>('detail')
  useEffect(()=>{let active=true;void api<OrderDetail>(`/orders/${id}/full`).then(value=>{if(active)setDetail(value)}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[id])
  const order=detail?.order
  return <div className="modal-backdrop" onClick={onClose}><section className="modal full-detail-modal" role="dialog" aria-modal="true" aria-label="订单详情" onClick={e=>e.stopPropagation()}><div className="section-head"><div><div className="eyebrow">ORDER DETAIL</div><h2>订单 #{String(id).padStart(6,'0')}</h2></div><button className="icon-button" aria-label="关闭详情" onClick={onClose}><X size={18}/></button></div>
    {error&&<div className="error" role="alert">{error}</div>}{!detail&&!error&&<p>正在读取订单与商品明细…</p>}
    {detail&&order&&<><div className="detail-tabs"><button className={tab==='detail'?'active':''} onClick={()=>setTab('detail')}>订单与商品</button><button className={tab==='audit'?'active':''} onClick={()=>setTab('audit')}>修改记录（{detail.audit.total}）</button></div>
    {tab==='detail'?<><div className="detail-overview"><div><span>订单金额</span><strong>¥{money(order.total_amount)}</strong></div><div><span>订单状态</span><strong>{statusLabels[order.status]}</strong></div><div><span>合成客户</span><strong>{detail.customer?.name||'未关联客户'}</strong></div></div>
    <dl className="detail-grid">{[['收货城市',order.shipping_city],['创建时间',fullDate(order.created_at)],['付款时间',fullDate(order.paid_at)],['发货时间',fullDate(order.shipped_at)],['优先处理',order.priority?'是':'否'],['复核状态',changeLabel('review_status',order.review_status)]].map(([key,value])=><div key={key}><dt>{key}</dt><dd>{value}</dd></div>)}</dl>
    <h3><Package size={16}/>商品明细</h3><div className="detail-items table-wrap"><table><thead><tr><th>商品 / 分类</th><th className="number">数量</th><th className="number">单价</th><th className="number">小计</th></tr></thead><tbody>{detail.items.map(item=><tr key={item.id}><td><strong>{item.product_name}</strong><small>{item.category} · 商品 #{item.product_id}</small></td><td className="number">{item.quantity}</td><td className="number">¥{money(item.unit_price)}</td><td className="number">¥{money(item.line_total)}</td></tr>)}</tbody></table>{!detail.items.length&&<p className="small muted">没有可访问的商品明细。</p>}</div>
    <h3><Truck size={16}/>物流信息</h3>{detail.shipment?<dl className="detail-grid"><div><dt>物流公司</dt><dd>{detail.shipment.carrier}</dd></div><div><dt>模拟运单号</dt><dd>{detail.shipment.tracking_no}</dd></div><div><dt>发货时间</dt><dd>{fullDate(detail.shipment.shipped_at)}</dd></div></dl>:<p className="detail-note">暂无发货记录。</p>}
    <h3>订单备注</h3><blockquote className="detail-note">{detail.note||'无备注'}</blockquote><p className="small muted">数据版本 v{order.version} · 所有客户和物流信息均为合成数据</p></>:<div className="detail-audit">{detail.audit.rows.map(entry=><div key={entry.id}><div><strong>{entry.actor_id}</strong><span>{fullDate(entry.created_at)}</span></div>{Object.entries(entry.after).map(([key,value])=><p key={key}>{changeLabel(key,entry.before[key])} → <strong>{changeLabel(key,value)}</strong></p>)}<small>操作 {entry.operation_id.slice(0,12)}</small></div>)}{!detail.audit.rows.length&&<p className="detail-note">该订单尚无字段修改记录。</p>}{detail.audit.total>detail.audit.rows.length&&<p className="small muted">此处展示最近 {detail.audit.rows.length} 条，完整记录可在操作审计中分页查看。</p>}</div>}
    </>}
  </section></div>
}
