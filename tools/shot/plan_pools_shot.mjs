// Capture the member-facing quota panel after the two-pool rewrite.
// Proves the console shows MiniMax 用量 and GLM 用量 as separate hourly token
// budgets, not three USD windows.
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE || 'http://127.0.0.1:3001'
const USER = process.env.ADMIN_USER || 'admin'
const PASS = process.env.ADMIN_PASS || ''
const OUT = process.env.OUT || 'D:/DEVTools/bossapi/shots'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({
  viewport: { width: 1440, height: 1200 },
  deviceScaleFactor: 2,
  locale: 'zh-CN',
})
const page = await context.newPage()

await page.goto(`${BASE}/sign-in?admin=1`, { waitUntil: 'networkidle' })
await page.waitForSelector('input[name="username"]', { timeout: 20000 })
await page.fill('input[name="username"]', USER)
await page.fill('input[name="password"]', PASS)
await page.click('button[type="submit"]')
await page.waitForURL((url) => !url.pathname.includes('sign-in'), { timeout: 30000 })

await page.goto(`${BASE}/my-plan`, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)

await page.screenshot({ path: `${OUT}/plan-pools-full.png`, fullPage: true })

// Close-up of the two pool cards.
const card = page.locator('div.grid.gap-3').first()
await card.screenshot({ path: `${OUT}/plan-pools-cards.png` })

console.log('saved', `${OUT}/plan-pools-full.png`)
await browser.close()
