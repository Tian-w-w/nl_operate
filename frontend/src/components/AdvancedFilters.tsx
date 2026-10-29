import type { Filters_Input as Filters } from '../api-types'
import { localInput, statusLabels, zonedInput } from '../format'

export function AdvancedFilters({value,onChange}:{value:Filters;onChange:(value:Filters)=>void}) {
  function text(key:keyof Filters,text:string) { onChange({...value,[key]:text || null}) }
  return <details className="advanced-filters"><summary>高级筛选 <span>商品、客户、多选条件与时间范围</span></summary><div className="advanced-grid">
    <label>订单编号<input aria-label="订单编号筛选" type="number" min="1" placeholder="精确匹配" value={value.order_id ?? ''} onChange={e=>onChange({...value,order_id:e.target.value ? Number(e.target.value) : null})}/></label>
    <label>客户名称<input aria-label="客户名称" placeholder="包含客户名称" maxLength={80} value={value.customer_name || ''} onChange={e=>text('customer_name',e.target.value)}/></label>
    <label>商品名称<input aria-label="商品名称" placeholder="包含商品名称" maxLength={80} value={value.product_keyword || ''} onChange={e=>text('product_keyword',e.target.value)}/></label>
    <label>商品分类<select aria-label="商品分类" value={value.product_category || ''} onChange={e=>text('product_category',e.target.value)}><option value="">全部分类</option>{['办公','数码','生活'].map(c=><option key={c}>{c}</option>)}</select></label>
    <label>金额上限<input aria-label="金额上限" type="number" min="0" step="0.01" placeholder="不限" value={value.amount_lte ?? ''} onChange={e=>text('amount_lte',e.target.value)}/></label>
    <label>处理优先级<select aria-label="处理优先级" value={value.priority === null || value.priority === undefined ? '' : String(value.priority)} onChange={e=>onChange({...value,priority:e.target.value ? e.target.value === 'true' : null})}><option value="">全部优先级</option><option value="true">优先处理</option><option value="false">普通优先级</option></select></label>
    <label>复核状态<select aria-label="复核状态" value={value.review_status || ''} onChange={e=>onChange({...value,review_status:(e.target.value || null) as Filters['review_status']})}><option value="">全部复核状态</option><option value="normal">正常</option><option value="pending_review">待复核</option></select></label>
    <label>多个城市<select aria-label="多个城市" multiple value={value.shipping_cities || []} onChange={e=>{const cities=Array.from(e.target.selectedOptions,o=>o.value);onChange({...value,shipping_city:null,shipping_cities:cities.length ? cities : null})}}>{['上海','北京','杭州','深圳','成都','广州'].map(c=><option key={c}>{c}</option>)}</select></label>
    <label>多个状态<select aria-label="多个状态" multiple value={value.statuses || []} onChange={e=>{const statuses=Array.from(e.target.selectedOptions,o=>o.value) as NonNullable<Filters['statuses']>;onChange({...value,status:null,statuses:statuses.length ? statuses : null})}}>{Object.entries(statusLabels).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
    {(['created_at_gte','created_at_lt','paid_at_gte','paid_at_lt'] as const).map((key,i)=><label key={key}>{['创建时间起点','创建时间终点（不含）','付款时间起点','付款时间终点（不含）'][i]}<input aria-label={['创建时间起点','创建时间终点','付款时间起点','付款时间终点'][i]} type="datetime-local" step="1" value={localInput(value[key])} onChange={e=>onChange({...value,[key]:zonedInput(e.target.value)})}/></label>)}
  </div><p>时间采用上海时区；同一字段多选为“或”，不同条件为“且”。按 Ctrl / Command 可选择多项。</p></details>
}
