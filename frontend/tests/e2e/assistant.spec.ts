import { test, expect, Page } from '@playwright/test'

const first = '查询上海等待发货超过48小时的订单，按金额降序，只展示列表和数量，不要导出。'

async function tell(page:Page,text:string) {
  const input=page.getByRole('textbox',{name:'输入业务指令'})
  await expect(input).toBeEnabled()
  await input.fill(text)
  await page.getByRole('button',{name:'发送',exact:true}).click()
  await expect(input).toBeEnabled()
}

test.beforeEach(async({page})=>{
  const login=await page.request.post('/api/auth/demo',{data:{actor:'operator_a'},headers:{'X-LLM4Web-Client':'web'}})
  expect(login.status()).toBe(200)
  await page.goto('/')
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(12)
  await expect(page.getByRole('button',{name:'筛选',exact:true})).toBeEnabled()
})

test('follow-up includes shipped orders in the same city and restores conversation after reload',async({page})=>{
  await tell(page,first)
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(4)
  await tell(page,'把已发货的也一并查出来')
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(5)
  await expect(page.getByLabel('收货城市',{exact:true})).toHaveValue('上海')
  await expect(page.getByLabel('排序方式',{exact:true})).toHaveValue('desc')
  const state=await(await page.request.get('/api/session')).json()
  expect(state.current_result.rows.map((row:{id:number})=>row.id)).toEqual([12,3,5,1,10])
  await page.reload()
  await expect(page.locator('.message.from-user').first()).toContainText(first)
  await tell(page,'那只看已发货的')
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(1)
  await expect(page.locator('.orders-panel tbody')).toContainText('#000005')
  await page.getByLabel('演示身份').selectOption('operator_b')
  await expect(page.locator('.message')).toHaveCount(0)
})

test('city cardinality returns a statistic and leaves the current order list intact',async({page},testInfo)=>{
  await tell(page,first)
  const before=await(await page.request.get('/api/session')).json()
  await tell(page,'统计一下一共有多少个收获城市')
  await expect(page.locator('.statistic-evidence')).toContainText('4')
  await expect(page.locator('.message.from-assistant').last()).toContainText('4个不同的收货城市')
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(4)
  const after=await(await page.request.get('/api/session')).json()
  expect(after.current_result.result_set_id).toBe(before.current_result.result_set_id)
  expect(after.context_version).toBe(before.context_version)
  await page.screenshot({path:testInfo.outputPath('v05-city-count.png'),fullPage:true})
})

test('knowledge answers show sources and demo labels and abstain on a new unknown topic',async({page},testInfo)=>{
  await tell(page,'规定的发货时间是多少')
  await expect(page.locator('.message.from-assistant').last()).toContainText('48小时')
  await expect(page.locator('.knowledge-evidence').first()).toContainText('知识库检索原文')
  await page.locator('.knowledge-source').first().locator('summary').click()
  await expect(page.locator('.knowledge-source').first().locator('blockquote')).toBeVisible()
  await tell(page,'默认发什么快递')
  await expect(page.locator('.message.from-assistant').last()).toContainText('顺丰速运')
  await expect(page.locator('.message.from-assistant').last()).toContainText('演示')
  await page.screenshot({path:testInfo.outputPath('v05-knowledge-answer.png'),fullPage:true})
  await tell(page,'公司的退货政策是什么')
  await expect(page.locator('.message.from-assistant').last()).toContainText('没有足够依据')
  await page.getByRole('button',{name:'知识库',exact:true}).click()
  await expect(page.getByRole('heading',{name:'业务知识库'})).toBeVisible()
  await expect(page.locator('.knowledge-library .knowledge-source')).toHaveCount(4)
  await page.setViewportSize({width:390,height:844})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:testInfo.outputPath('v05-knowledge-mobile.png'),fullPage:true})
})
