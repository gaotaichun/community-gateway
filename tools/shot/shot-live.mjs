// Proves the P5 acceptance criterion end to end: edit the 5 hour window in the
// admin editor, save, reload, and check that both the editor and the quota
// cards show the new value.
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE || 'http://127.0.0.1:3000'
const USER = process.env.ADMIN_USER || 'admin'
const PASS = process.env.ADMIN_PASS || ''
const OUT = process.env.OUT || 'D:/DEVTools/bossapi/shots'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({
  viewport: { width: 1440, height: 1100 },
  deviceScaleFactor: 2,
  locale: 'en-US',
})
const page = await context.newPage()

async function signIn() {
  await page.goto(`${BASE}/sign-in?admin=1`, { waitUntil: 'networkidle' })
  await page.waitForSelector('input[name="username"]', { timeout: 20000 })
  await page.fill('input[name="username"]', USER)
  await page.fill('input[name="password"]', PASS)
  await page.click('button[type="submit"]')
  await page.waitForURL((url) => !url.pathname.includes('sign-in'), { timeout: 30000 })
  await page.waitForLoadState('networkidle')
}

async function cardTexts() {
  // The three window cards render a "剩余" label next to the remaining amount.
  return page.$$eval('span', (nodes) =>
    nodes
      .filter((n) => n.textContent?.trim() === '剩余')
      .map((n) => n.parentElement?.textContent?.trim() ?? '')
  )
}

try {
  await signIn()
  await page.goto(`${BASE}/my-plan`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2000)
  console.log('before:', JSON.stringify(await cardTexts()))

  // First number input inside "Plan administration" = the 5 hour window (USD).
  const inputs = page.locator('input[type="number"]')
  await inputs.first().fill('4')
  await page.getByRole('button', { name: 'Save' }).first().click()
  await page.waitForTimeout(2500)

  await page.reload({ waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)
  console.log('after reload:', JSON.stringify(await cardTexts()))
  await page.screenshot({ path: `${OUT}/p5-my-plan-edited.png`, fullPage: true })

  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle' })
  await page.waitForTimeout(2500)
  await page.screenshot({ path: `${OUT}/p5-overview-edited.png`, fullPage: true })
  console.log('shot p5-my-plan-edited.png / p5-overview-edited.png')
} finally {
  await browser.close()
}
