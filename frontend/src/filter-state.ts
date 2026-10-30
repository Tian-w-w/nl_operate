import type { Filters_Input as Filters, Sort } from './api-types'

export function previousWeek(reference: string) {
  const local = new Date(new Date(reference).getTime() + 8 * 3600000)
  const monday = new Date(Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() - (local.getUTCDay() + 6) % 7) - 8 * 3600000)
  return { created_at_gte: new Date(monday.getTime() - 7 * 86400000).toISOString(), created_at_lt: monday.toISOString() }
}

export function isPreviousWeek(filters: Filters, reference: string) {
  const bounds = previousWeek(reference)
  return Boolean(filters.created_at_gte && filters.created_at_lt
    && new Date(filters.created_at_gte).getTime() === new Date(bounds.created_at_gte).getTime()
    && new Date(filters.created_at_lt).getTime() === new Date(bounds.created_at_lt).getTime())
}

export function sortControlValue(sorts: Sort[]) {
  if (sorts.length === 1 && sorts[0].field === 'id' && sorts[0].direction === 'asc') return 'id'
  if (sorts[0]?.field === 'total_amount' && sorts.length <= 2
    && (sorts.length === 1 || (sorts[1].field === 'id' && sorts[1].direction === 'asc'))) return sorts[0].direction
  return 'custom'
}

export const filterLabels: Record<keyof Filters, string> = {
  order_id:'订单编号', shipping_cities:'多个城市', statuses:'多个状态', customer_name:'客户名称', product_keyword:'商品名称', product_category:'商品分类',
  additional_statuses:'额外包含状态（不受待发货时限限制）',
  shipping_city: '收货城市', status: '订单状态', priority: '优先处理', review_status: '复核状态',
  amount_gte: '金额下限', amount_lte: '金额上限', created_at_gte: '创建起点', created_at_lt: '创建终点（不含）',
  paid_at_gte: '付款起点', paid_at_lt: '付款终点（不含）', waiting_hours_gt: '等待发货超过（小时）',
}
