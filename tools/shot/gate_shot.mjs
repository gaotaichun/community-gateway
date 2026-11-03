// Capture what a rejected visitor actually sees.
//
// Drives the real SPA callback route (/oauth/discord) with a non-member code so
// the gate rejects the login, then screenshots the sign-in page where the
// persistent notice is rendered.
import { chromium } from 'playwright-core'
import { mkdirSync } from 'node:fs'

const BASE = process.env.BASE || 'http://127.0.0.1:3000'
const CODE = process.env.CODE || 'code-bob'
const OUT = process.env.OUT || 'D:/DEVTools/bossapi/shots'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch({ channel: 'msedge', headless: true })
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
  locale: 'zh-CN',
})
const page = await context.newPage()

const errors = []
page.on('pageerror', (err) => errors.push(String(err)))

await page.goto(`${BASE}/sign-in`, { waitUntil: 'domcontentloaded' })

// Same call the "Discord 登录" button makes, so the flow token is genuine.
const flowToken = await page.evaluate(async (base) => {
  const resp = await fetch(`${base}/api/oauth/state`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider: 'discord', intent: 'login' }),
  })
  const body = await resp.json()
  return body?.data?.flow_token ?? null
}, BASE)

if (!flowToken) {
  console.error('could not obtain a flow token')
  process.exit(1)
}

await page.goto(
  `${BASE}/oauth/discord?code=${encodeURIComponent(CODE)}&state=${encodeURIComponent(flowToken)}`,
  { waitUntil: 'networkidle' }
)

// The callback screen navigates to /sign-in once the exchange fails.
await page
  .waitForURL((url) => url.pathname.includes('sign-in'), { timeout: 30000 })
  .catch(() => {})
await page.waitForTimeout(1200)

const banner = await page
  .locator('[data-slot="alert"]')
  .first()
  .innerText()
  .catch(() => '(no banner)')

await page.screenshot({ path: `${OUT}/gate-rejected.png`, fullPage: false })
console.log('banner text:', JSON.stringify(banner))
console.log('page errors:', errors.length ? errors : 'none')
await browser.close()
