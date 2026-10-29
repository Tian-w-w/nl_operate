import { test,expect,Page } from '@playwright/test'

const sh='上海等待发货超过48小时的订单按金额降序，超过10条就导出，否则展示列表并告诉我数量。'
const bj='北京等待发货超过48小时的订单按金额降序，超过10条导出，否则展示数量和列表。'
const ambiguous='查询上海等了很久还没发货的订单，数量比较多就导出。'
const bad='查询上海等待发货超过48小时的订单，按金额降序，只展示列表和数量。'

async function send(page:Page,goal:string){
  await page.getByLabel('助手执行模式').selectOption('agent')
  await page.getByLabel('输入业务指令').fill(goal)
  await page.getByRole('button',{name:'发送',exact:true}).click()
  await expect(page.getByRole('region',{name:'只读Agent任务'})).toBeVisible()
  await expect(page.getByRole('button',{name:'筛选',exact:true})).toBeEnabled({timeout:15000})
}

test.beforeEach(async({page})=>{
  expect((await page.request.post('/api/auth/demo',{data:{actor:'operator_a'},headers:{'X-LLM4Web-Client':'web'}})).status()).toBe(200)
  await page.goto('/')
  await expect(page.getByRole('button',{name:'筛选',exact:true})).toBeEnabled()
})

test('scripted Agent performs query, export and evidence finish with a real download',async({page},info)=>{
  await send(page,sh)
  const panel=page.getByRole('region',{name:'只读Agent任务'})
  await expect(panel.locator('.agent-run-title')).toContainText('已完成')
  await expect(panel.locator('.agent-steps li')).toHaveCount(3)
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(12)
  const download=panel.getByRole('link',{name:'下载 12 笔订单'})
  const href=await download.getAttribute('href')
  const response=await page.request.get(href!)
  expect(response.status()).toBe(200)
  const lines=(await response.body()).toString('utf8').replace(/^\uFEFF/,'').trim().split(/\r?\n/)
  expect(lines.slice(1).map(line=>Number(line.split(',')[0]))).toEqual([112,111,110,109,108,107,106,105,104,103,102,101])
  await panel.screenshot({path:info.outputPath('agent-complete.png')})
  await page.reload()
  await page.getByLabel('助手执行模式').selectOption('agent')
  await expect(panel.locator('.agent-run-title')).toContainText('已完成')
  await expect(panel.getByRole('link',{name:'下载 12 笔订单'})).toBeVisible()
})

test('scripted exact-ten branch ends without exporting and shows the real count',async({page})=>{
  await send(page,bj)
  const panel=page.getByRole('region',{name:'只读Agent任务'})
  await expect(panel.locator('.agent-run-title')).toContainText('已完成')
  await expect(panel).toContainText('已查询到10条订单')
  await expect(panel.locator('.agent-steps li')).toHaveCount(2)
  await expect(panel.getByRole('link')).toHaveCount(0)
  await expect(page.locator('.orders-panel tbody tr')).toHaveCount(10)
})

test('scripted clarification and invalid decisions stop without showing success',async({page})=>{
  await send(page,ambiguous)
  const panel=page.getByRole('region',{name:'只读Agent任务'})
  await expect(panel.locator('.agent-run-title')).toContainText('等待澄清')
  await expect(panel.locator('.agent-steps li')).toHaveCount(1)
  await send(page,bad)
  await expect(panel.locator('.agent-run-title')).toContainText('失败')
  await expect(panel).toContainText('UNKNOWN_TOOL')
  await expect(panel.getByRole('link')).toHaveCount(0)
  await page.getByLabel('演示身份').selectOption('operator_b')
  await expect(panel).toHaveCount(0)
})

test('mobile step panel fits the viewport',async({page})=>{
  await page.setViewportSize({width:390,height:844})
  await send(page,bj)
  await expect(page.locator('.agent-run-title')).toContainText('已完成')
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
})
