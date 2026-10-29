import { test, expect, Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

async function settled(page: Page) {
  await expect(page.getByRole('button', { name: '筛选', exact: true })).toBeEnabled()
}

async function send(page: Page, text: string) {
  await settled(page)
  await page.getByRole('textbox', { name: '输入业务指令' }).fill(text)
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await settled(page)
}

test.beforeEach(async ({ page }) => {
  const response = await page.request.post('/api/auth/demo', { data: { actor: 'operator_a' }, headers: { 'X-LLM4Web-Client': 'web' } })
  expect(response.status()).toBe(200)
  await page.goto('/')
  await expect(page.locator('tbody tr')).toHaveCount(12)
  await settled(page)
})

test('manual filtering, natural language refinement, trusted confirmation and real CSV download', async ({ page }, testInfo) => {
  await page.getByLabel('收货城市', { exact: true }).selectOption('上海')
  await page.getByLabel('订单状态', { exact: true }).selectOption('paid')
  await page.getByRole('button', { name: '筛选', exact: true }).click()
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(6)
  await settled(page)
  await send(page, '找出上周已付款但未发货的上海订单，按金额从高到低展示')
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(4)
  await expect(page.locator('.orders-panel tbody tr').first()).toContainText('#000012')
  await send(page, '其中等待超过48小时的标为优先处理')
  const dialog = page.getByRole('dialog')
  await expect(dialog.getByRole('heading', { name: '确认修改 3 笔订单' })).toBeVisible()
  await expect(dialog.locator('tbody tr')).toHaveCount(3)
  await page.reload()
  await expect(dialog.getByRole('heading', { name: '确认修改 3 笔订单' })).toBeVisible()
  await expect(dialog.getByRole('button', { name: '确认并提交' })).toBeEnabled()
  await dialog.getByRole('button', { name: '确认并提交' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(3)
  await expect(page.locator('.orders-panel tbody .priority')).toHaveCount(3)
  await send(page, '导出这些订单')
  const messageBounds = await page.locator('.message.from-assistant').last().boundingBox()
  expect(messageBounds!.height).toBeLessThan(200)
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载 3 笔订单' }).click()
  const download = await downloadPromise
  const path = testInfo.outputPath('orders.csv')
  await download.saveAs(path)
  const text = await readFile(path, 'utf8')
  expect(text.charCodeAt(0)).toBe(0xfeff)
  expect(text.trim().split(/\r?\n/).slice(1).map(line => Number(line.split(',')[0]))).toEqual([12,3,1])
  await page.screenshot({ path: testInfo.outputPath('workflow-desktop.png'), fullPage: true })
})

test('read-only role cannot modify; unknown constraints ask for clarification', async ({ page }) => {
  await page.getByLabel('演示身份').selectOption('viewer_a')
  await settled(page)
  await send(page, '找出上海已付款订单')
  await send(page, '这些订单标为待复核')
  await expect(page.getByText('当前角色只有读取权限', { exact: true })).toBeVisible()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await send(page, '找出上海金额大于100元的订单')
  await expect(page.locator('.message.from-assistant').last()).toContainText('待澄清')
  await expect(page.getByRole('button', { name: '标记优先处理', exact: true })).toBeDisabled()
})

test('cancel prevents a write, details and mobile layout remain usable', async ({ page }, testInfo) => {
  await send(page, '找出北京已付款订单')
  await send(page, '这些订单标为待复核')
  await page.getByRole('button', { name: '取消修改', exact: true }).click()
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(page.locator('.orders-panel tbody .review')).toHaveCount(0)
  await page.getByRole('button', { name: '#000004', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '订单详情' })).toContainText('正常')
  await page.getByLabel('关闭详情').click()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: '订单工作台' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('workflow-mobile.png'), fullPage: true })
})

test('72-hour query keeps its exact threshold and hidden filters when submitted again', async ({ page }) => {
  await send(page, '找出上海已付款订单等待超过72小时金额不超过1000元')
  await expect(page.getByLabel('等待发货小时数')).toHaveValue('72')
  await expect(page.getByLabel('完整筛选条件')).toContainText('金额上限：1000')
  const before = await page.locator('.orders-panel tbody .order-link').allTextContents()
  const request = page.waitForRequest(r => r.url().endsWith('/api/orders/query') && r.method() === 'POST')
  await page.getByRole('button', { name:'筛选', exact:true }).click()
  const body = (await request).postDataJSON()
  expect(body.filters.waiting_hours_gt).toBe(72)
  expect(body.filters.amount_lte).toBe('1000')
  await settled(page)
  expect(await page.locator('.orders-panel tbody .order-link').allTextContents()).toEqual(before)
  await send(page, '查询上周付款的订单')
  await expect(page.getByRole('checkbox',{name:'上周创建'})).not.toBeChecked()
  const paidRequest = page.waitForRequest(r => r.url().endsWith('/api/orders/query') && r.method() === 'POST')
  await page.getByRole('button',{name:'筛选',exact:true}).click()
  const paidFilters = (await paidRequest).postDataJSON().filters
  expect(paidFilters.paid_at_gte).toBe('2026-10-19T00:00:00+08:00')
  expect(paidFilters.paid_at_lt).toBe('2026-10-26T00:00:00+08:00')
  expect(paidFilters.created_at_gte ?? null).toBeNull()
  await settled(page)
})

test('server failure does not create a new operator identity', async ({ page }) => {
  await page.getByLabel('演示身份').selectOption('viewer_a')
  await settled(page)
  let logins = 0
  page.on('request', request => { if (request.url().endsWith('/api/auth/demo')) logins++ })
  await page.route('**/api/session', route => route.fulfill({ status:503, contentType:'application/json', body:JSON.stringify({code:'SERVICE_UNAVAILABLE',message:'测试服务暂不可用'}) }))
  await page.reload()
  await expect(page.getByRole('alert')).toContainText('测试服务暂不可用')
  expect(logins).toBe(0)
  await page.unroute('**/api/session')
  expect((await (await page.request.get('/api/session')).json()).actor).toBe('viewer_a')
})
