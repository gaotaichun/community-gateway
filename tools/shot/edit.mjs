// P5 acceptance: edit the three windows in the admin editor on /my-plan,
// save, reload, and screenshot. Proves the console writes what it shows and
// that a plain refresh picks up the new numbers.
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE || 'http://127.0.0.1:3000'
const USER = process.env.ADMIN_USER || 'admin'
const PASS = process.env.ADMIN_PASS || ''
const OUT = process.env.OUT || 'D:/DEVTools/bossapi/shots'
const VALUES = (process.env.VALUES || '5,15,45').split(',').map((v) => v.trim())

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({
  viewport: { width: 1440, height: 1100 },
  deviceScaleFactor: 2,
  locale: 'en-US',
})
const page = await context.newPage()

await page.goto(`${BASE}/sign-in?admin=1`, { waitUntil: 'networkidle' })
await page.waitForSelector('input[name="username"]', { timeout: 20000 })
await page.fill('input[name="username"]', USER)
await page.fill('input[name="password"]', PASS)
await page.click('button[type="submit"]')
await page.waitForURL((url) => !url.pathname.includes('sign-in'), { timeout: 30000 })

await page.goto(`${BASE}/my-plan`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2000)

const inputs = page.locator('input[type="number"]')
const count = await inputs.count()
console.log('number inputs found:', count)
if (count < 3) throw new Error('expected three quota inputs')

const before = []
for (let i = 0; i < 3; i++) before.push(await inputs.nth(i).inputValue())
console.log('before:', before.join(' / '))

for (let i = 0; i < 3; i++) {
  await inputs.nth(i).fill(VALUES[i])
}
await page.getByRole('button', { name: /^Save$/ }).first().click()
await page.waitForTimeout(2500)

// plain refresh, exactly what a human does
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

const after = []
const refreshed = page.locator('input[type="number"]')
for (let i = 0; i < 3; i++) after.push(await refreshed.nth(i).inputValue())
console.log('after refresh:', after.join(' / '))

await page.screenshot({ path: `${OUT}/p5-b-my-plan.png`, fullPage: true })
await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
await page.screenshot({ path: `${OUT}/p5-b-overview.png`, fullPage: true })

await browser.close()
console.log(after.join(',') === VALUES.join(',') ? 'UI_EDIT_OK' : 'UI_EDIT_MISMATCH')
