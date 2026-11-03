// Screenshot helper for P5 verification.
// Launches the locally installed Edge through playwright-core, signs in with the
// admin account, then captures the overview page and the "My plan" page.
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

const errors = []
page.on('console', (msg) => {
  if (msg.type() === 'error') errors.push(msg.text())
})
page.on('pageerror', (err) => errors.push(String(err)))

async function signIn() {
  await page.goto(`${BASE}/sign-in?admin=1`, { waitUntil: 'networkidle' })
  await page.waitForSelector('input[name="username"]', { timeout: 20000 })
  await page.fill('input[name="username"]', USER)
  await page.fill('input[name="password"]', PASS)
  await page.click('button[type="submit"]')
  await page.waitForURL((url) => !url.pathname.includes('sign-in'), {
    timeout: 30000,
  })
  await page.waitForLoadState('networkidle')
}

async function shot(path, file, opts = {}) {
  await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle' })
  // let react-query settle + countdown timers tick once
  await page.waitForTimeout(opts.wait ?? 2500)
  const full = opts.full ?? false
  await page.screenshot({ path: `${OUT}/${file}`, fullPage: full })
  console.log(`shot ${file} <- ${path}`)
}

try {
  await signIn()
  console.log('signed in as', USER)
  await shot('/dashboard', 'p5-overview.png', { full: true })
  await shot('/my-plan', 'p5-my-plan.png', { full: true })
} finally {
  if (errors.length) {
    console.log('--- console errors ---')
    for (const e of errors.slice(0, 10)) console.log(e)
  }
  await browser.close()
}
