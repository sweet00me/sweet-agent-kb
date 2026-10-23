// 文件缩略图真实 React 浏览器回归：隔离 API、173 项分页、懒加载、回退及预览下载。
import assert from 'node:assert/strict'
import { createServer as httpServer } from 'node:http'
import { pathToFileURL, fileURLToPath } from 'node:url'
import { createServer as viteServer } from 'vite'

const playwright = await import(process.env.SWEET_PLAYWRIGHT_CORE_PATH
  ? pathToFileURL(process.env.SWEET_PLAYWRIGHT_CORE_PATH).href : 'playwright-core')
const root = fileURLToPath(new URL('../', import.meta.url))
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64')
const file = (name, kind = 'file') => ({ name, path: name, kind,
  size_bytes: kind === 'directory' ? null : 16 * 1024 * 1024, modified_at: '2026-10-22T08:00:00+00:00' })
const longPhotoName = '很长的照片文件名_保留名称与大小及修改时间_2026-10-22.JPEG'
const entries = [file('folder.JPG', 'directory'), file('DSC_0064.JPG'), file('DSC_0093.JPG'),
  file('transparent.PNG'), file('sample.webp'), file(longPhotoName), file('notes.md'), file('paper.pdf'), file('code.py')]
for (let index = entries.length; index < 173; index++) entries.push(file(`photo_${index}.jpg`))
let fixture
const api = httpServer(async (req, res) => {
  const state = fixture, url = new URL(req.url, 'http://localhost'), path = url.pathname
  state.requests.push(path + url.search)
  const json = (data, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(data)) }
  const image = () => { res.writeHead(200, { 'Content-Type': 'image/png', 'Cache-Control': 'no-store' }); res.end(png) }
  if (path === '/api/v1/threads') return json({ items: [] })
  if (path === '/api/v1/files/roots') return json({ count: 1, items: [{ root_id: 'fixture', label: '临时图片目录', path: '/fixture', kind: 'home', writable: true, filesystem: null }] })
  if (path === '/api/v1/files' || path === '/api/v1/files/search') {
    const offset = Number(url.searchParams.get('offset') ?? 0), limit = Number(url.searchParams.get('limit') ?? 100)
    const items = path.endsWith('/search') ? entries.filter(item => item.name.includes('DSC')) : entries.slice(offset, offset + limit)
    if (path.endsWith('/search')) return json({ root_id: 'fixture', path: '', query: 'DSC', recursive: true,
      max_depth: 5, scanned_entries: 173, returned: items.length, incomplete: false, stop_reason: null, depth_limited: false, skipped_directories: 0, items })
    return json({ root_id: 'fixture', path: '', parent_path: null, total: 173, offset, limit, has_more: offset + limit < 173, items })
  }
  if (path === '/api/v1/files/thumbnail') {
    if (url.searchParams.get('path') === 'DSC_0093.JPG') return json({ detail: '损坏的测试图片' }, 422)
    await state.release
    return image()
  }
  if (path === '/api/v1/files/preview/info') return json({ filename: url.searchParams.get('path'), kind: 'image',
    content_type: 'image/png', size_bytes: png.length, language: null, can_preview: true, reason: null, limit_bytes: 20 * 1024 * 1024 })
  if (path === '/api/v1/files/preview') return image()
  if (path === '/api/v1/files/download') {
    res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${url.searchParams.get('path')}"` })
    return res.end(png)
  }
  state.unexpected.push(req.method + ' ' + path)
  return json({}, 404)
})
await new Promise(resolve => api.listen(0, '127.0.0.1', resolve))
const target = `http://127.0.0.1:${api.address().port}`
const vite = await viteServer({ root, configFile: root + '/vite.config.ts', server: {
  host: '127.0.0.1', port: 0, proxy: { '/api': { target, changeOrigin: true } },
} })
assert.equal(vite.config.server.proxy['/api'].target, target)
await vite.listen()
const base = `http://127.0.0.1:${vite.httpServer.address().port}`
const results = []
try {
  for (const scenario of (process.env.SWEET_THUMBNAIL_UI_CASES || 'chromium-desktop,chromium-iphone,webkit-iphone').split(',')) {
    const engine = scenario.split('-')[0], mobile = scenario.endsWith('iphone')
    const browser = await playwright[engine].launch({ headless: true,
      ...(engine === 'webkit' && process.env.SWEET_WEBKIT_EXECUTABLE ? { executablePath: process.env.SWEET_WEBKIT_EXECUTABLE } : {}),
    })
    let release
    fixture = { requests: [], unexpected: [], release: new Promise(resolve => { release = resolve }) }
    const state = fixture
    const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 900 },
      ...(mobile ? { isMobile: true, hasTouch: true, deviceScaleFactor: 3 } : {}), acceptDownloads: true })
    const page = await context.newPage()
    page.setDefaultTimeout(5000)
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => ['127.0.0.1', 'localhost'].includes(new URL(route.request().url()).hostname) ? route.continue() : route.abort())
    const row = name => page.getByRole('button', { name, exact: true }).locator('..').locator('..')
    try {
      await page.goto(base)
      await page.getByRole('button', { name: '知识库', exact: true }).filter({ visible: true }).click()
      await page.getByRole('button', { name: /文档管理.*浏览服务器目录/ }).click()
      const first = row('DSC_0064.JPG')
      // 请求尚未返回时，必须有图标占位，且不请求原图。
      await first.locator('img').waitFor({ state: 'attached' })
      assert.equal(await first.locator('svg.lucide-file-text').isVisible(), true)
      assert.equal(state.requests.filter(path => path.startsWith('/api/v1/files/preview')).length, 0)
      const initial = state.requests.filter(path => path.startsWith('/api/v1/files/thumbnail')).length
      assert.ok(initial > 0 && initial < 30, `Lazy loading requested ${initial}/100 images at once`)
      release()
      await page.waitForFunction(() => [...document.querySelectorAll('img')].some(img => img.complete && img.naturalWidth > 0))
      await first.locator('svg.lucide-file-text').waitFor({ state: 'detached' })
      assert.equal(await first.locator('img').getAttribute('loading'), 'lazy')
      assert.equal(await first.locator('img').getAttribute('decoding'), 'async')
      const metrics = await first.locator('img').evaluate(img => ({ width: img.width, height: img.height, fit: getComputedStyle(img).objectFit }))
      assert.deepEqual(metrics, { width: 56, height: 56, fit: 'cover' })
      await row('DSC_0093.JPG').locator('img').waitFor({ state: 'detached' })
      assert.equal(await row('DSC_0093.JPG').locator('svg.lucide-file-text').isVisible(), true)
      assert.ok(state.requests.some(path => path.startsWith('/api/v1/files/thumbnail') && path.includes('DSC_0093.JPG')))
      for (const name of ['transparent.PNG', 'sample.webp', longPhotoName]) {
        await row(name).scrollIntoViewIfNeeded()
        await row(name).locator('img').waitFor()
        await row(name).locator('svg.lucide-file-text').waitFor({ state: 'detached' })
        const bounds = await row(name).evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }))
        assert.ok(bounds.scroll <= bounds.width, `Long image name overflows: ${JSON.stringify(bounds)}`)
      }
      assert.equal(await row('folder.JPG').locator('svg.lucide-folder').isVisible(), true)
      assert.equal(await row('folder.JPG').locator('img').count(), 0)
      for (const name of ['notes.md', 'paper.pdf', 'code.py']) {
        assert.equal(await row(name).locator('img').count(), 0)
        assert.equal(await row(name).locator('svg.lucide-file-text').isVisible(), true)
      }
      const beforePreview = state.requests.length
      await page.getByRole('button', { name: 'DSC_0064.JPG', exact: true }).click()
      await page.getByRole('dialog', { name: '文件预览' }).locator('img').waitFor()
      assert.ok(state.requests.slice(beforePreview).some(path => path.startsWith('/api/v1/files/preview?')))
      if (mobile) {
        const box = await page.getByRole('dialog').boundingBox()
        assert.equal(Math.round(box.width), 390)
      }
      await page.getByRole('button', { name: '关闭预览', exact: true }).click()
      await page.getByRole('button', { name: '预览 DSC_0064.JPG', exact: true }).click()
      await page.getByRole('dialog', { name: '文件预览' }).waitFor()
      await page.getByRole('button', { name: '关闭预览', exact: true }).click()
      const downloading = page.waitForEvent('download')
      await page.getByRole('link', { name: '下载 DSC_0064.JPG', exact: true }).click()
      assert.equal((await downloading).suggestedFilename(), 'DSC_0064.JPG')
      assert.equal(await page.getByRole('button', { name: /^photo_\d+\.jpg$/ }).count(), 91)
      await page.getByRole('button', { name: '下一页', exact: true }).click()
      await page.getByText('101–173 / 173', { exact: true }).waitFor()
      assert.equal(await page.getByRole('button', { name: /^photo_\d+\.jpg$/ }).count(), 73)
      assert.ok(!state.requests.some(path => path.startsWith('/api/v1/files/thumbnail') && decodeURIComponent(path).includes('photo_172.jpg')),
        'Offscreen final image must not be fetched before scrolling')
      await row('photo_172.jpg').scrollIntoViewIfNeeded()
      await row('photo_172.jpg').locator('img').waitFor()
      await page.getByRole('searchbox', { name: '搜索当前目录', exact: true }).fill('DSC')
      await page.getByRole('button', { name: '搜索', exact: true }).click()
      await page.getByText('返回 2 项', { exact: true }).waitFor()
      await row('DSC_0064.JPG').scrollIntoViewIfNeeded()
      await row('DSC_0064.JPG').locator('img').waitFor()
      const layout = await row('DSC_0064.JPG').evaluate(el => ({ width: el.clientWidth, scroll: el.scrollWidth }))
      assert.ok(layout.scroll <= layout.width, `File row overflows: ${JSON.stringify(layout)}`)
      assert.deepEqual(state.unexpected, [])
      assert.deepEqual(errors, [])
      results.push({ scenario, initial_thumbnail_requests: initial, passed: true })
    } finally {
      release()
      await context.close()
      await browser.close()
    }
  }
  console.log(JSON.stringify(results, null, 2))
} finally {
  await vite.close()
  await new Promise(resolve => api.close(resolve))
}
