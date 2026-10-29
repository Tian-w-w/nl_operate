export const statusLabels: Record<string,string> = { paid:'待发货', pending_payment:'待付款', shipped:'已发货', completed:'已完成', cancelled:'已取消' }
export const money = (value: string) => Number(value).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2})
export const fullDate = (value: string | null) => value ? new Date(value).toLocaleString('sv-SE',{timeZone:'Asia/Shanghai',hour12:false}) : '—'
export const changeLabel = (key: string,value: unknown) => key === 'priority' ? (value ? '优先处理' : '普通优先级') : (value === 'pending_review' ? '待复核' : '正常')
export const localInput = (value: string | null | undefined) => value ? fullDate(value).replace(' ','T') : ''
export const zonedInput = (value: string) => value ? `${value}+08:00` : null
