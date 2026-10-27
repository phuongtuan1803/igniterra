/* Dựng bộ màn hình chuẩn UX từ fragments/ rồi chụp ảnh chuẩn 1440/768/390.
   Chạy từ gốc repo: node docs/ux/spec/build.cjs            (dựng pages/ và chụp shots/)
                     node docs/ux/spec/build.cjs --serve    (chỉ mở server để xem pages/)
   Dùng CSS thật của app (studio/web/static/style.css) nên giao diện chuẩn đi cùng giao diện thật. */
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');

const root = path.resolve(__dirname, '../../..');
const spec = __dirname;
const screens = JSON.parse(fs.readFileSync(path.join(spec, 'screens.json'), 'utf8'));
const widths = [1440, 768, 390];
const labels = {tasks: 'Tasks', series: 'Series Bible', gpu: 'GPU', comfyui: 'ComfyUI Workflows', monitor: 'Monitor', settings: 'Settings', start: 'Bắt đầu'};
const nav = [['PRODUCTION', [['start', 'Bắt đầu', 'grid'], ['tasks', 'Tasks', 'film'], ['series', 'Series Bible', 'stack']]],
  ['INFRASTRUCTURE', [['gpu', 'GPU', 'cpu'], ['comfyui', 'ComfyUI Workflows', 'flow'], ['monitor', 'Monitor', 'monitor']]],
  ['', [['settings', 'Settings', 'settings']]]];
const icon = name => `<svg class="ico" aria-hidden="true" focusable="false"><use href="/studio/web/static/icons.svg#ic-${name}"></use></svg>`;
const expand = html => html.replace(/\{\{icon:([a-z-]+)\}\}/g, (_, n) => icon(n));

function page(s) {
  const body = expand(fs.readFileSync(path.join(spec, 'fragments', s.id + '.html'), 'utf8'));
  const menu = nav.map(([cap, items]) => (cap ? `<div class="nav-caption">${cap}</div>` : '<div class="nav-divider"></div>') +
    items.map(([key, label, glyph]) => `<a href="#" class="nav-item ${key === s.group ? 'active' : ''}"${key === s.group ? ' aria-current="page"' : ''}>${icon(glyph)}<span>${label}</span></a>`).join('')).join('');
  return `<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${s.id} ${s.title} · Igniterra UX chuẩn</title>
<link rel="stylesheet" href="/studio/web/static/style.css">
<link rel="stylesheet" href="/docs/ux/spec/spec.css">
</head>
<body>
<div class="app-shell">
  <aside class="app-sidebar" aria-label="Điều hướng">
    <a class="brand" href="#"><span class="brand-symbol" aria-hidden="true"><span>i</span></span><span><span class="brand-wordmark">igniterra</span><span class="brand-sub" style="display:block">CREATIVE OPERATIONS</span></span></a>
    <nav class="nav-menu" aria-label="Workspace">${menu}</nav>
    <div class="sidebar-foot"><span class="status-dot" aria-hidden="true"></span><div><strong>Local workspace</strong><small>Trạng thái từ dữ liệu studio</small></div></div>
  </aside>
  <div class="app-content">
    <header class="utility-bar"><div class="utility-start"><button type="button" class="menu-button" aria-label="Mở điều hướng">${icon('menu')}</button><span>Studio <span class="utility-slash">/</span> <b>${labels[s.group]}</b></span></div><span class="utility-mode">LOCAL STUDIO</span></header>
    <main id="main" class="page-container" tabindex="-1">
${body}
    </main>
  </div>
</div>
<nav class="mobile-nav" aria-label="Truy cập nhanh">${[['tasks', 'film', 'Tasks'], ['gpu', 'cpu', 'GPU'], ['comfyui', 'flow', 'ComfyUI'], ['monitor', 'monitor', 'Monitor']].map(([k, g, l]) => `<a href="#" class="${k === s.group ? 'active' : ''}">${icon(g)}${l}</a>`).join('')}</nav>
</body>
</html>
`;
}

function index() {
  const rows = screens.map(s => `<tr><td><code>${s.id}</code></td><td><a href="pages/${s.id}.html">${s.title}</a></td><td>${s.journeys.join(', ')}</td><td>GĐ ${s.phase}</td><td>${s.state}</td><td>${widths.map(w => `<a href="shots/${w}-${s.id}.png">${w}</a>`).join(' · ')}</td></tr>`).join('\n');
  return `<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Igniterra · UX chuẩn</title><link rel="stylesheet" href="/studio/web/static/style.css"></head><body><main class="page-container"><div class="page-header"><div><p class="eyebrow">IGN-UX · MÀN HÌNH CHUẨN</p><h1>Bộ màn hình chuẩn</h1><p class="muted small">Nguồn: docs/ux/spec/fragments + screens.json. Yêu cầu từng màn: docs/src/design/10-ux-beginner.md.</p></div></div><section class="card"><div class="table-scroll"><table><thead><tr><th>ID</th><th>Màn hình</th><th>Hành trình</th><th>Giai đoạn</th><th>Trạng thái</th><th>Ảnh chuẩn</th></tr></thead><tbody>${rows}</tbody></table></div></section></main></body></html>\n`;
}

function checklist() {
  const out = ['# Checklist review UX theo màn hình chuẩn', '', 'File sinh từ `screens.json` bằng `node docs/ux/spec/build.cjs`; không sửa tay. Cách dùng: [README](README.md).', ''];
  for (const s of screens) {
    out.push(`## ${s.id} · ${s.title}`, '', `${s.purpose} Hành trình ${s.journeys.join(', ')} · giai đoạn ${s.phase} · ${s.state}. Ảnh chuẩn: ${widths.map(w => `[${w}](shots/${w}-${s.id}.png)`).join(' · ')}.`, '', 'Phải có:', '');
    for (const m of s.must) out.push(`- [ ] ${m}`);
    out.push('', 'Không được có:', '');
    for (const m of s.must_not) out.push(`- [ ] ${m}`);
    out.push('');
  }
  return out.join('\n');
}

const types = {'.html': 'text/html; charset=utf-8', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.js': 'text/javascript'};
function serve(port) {
  return new Promise(resolve => {
    const server = http.createServer((req, res) => {
      const file = path.join(root, decodeURIComponent(new URL(req.url, 'http://x').pathname));
      if (!file.startsWith(root) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end(); }
      res.writeHead(200, {'content-type': types[path.extname(file)] || 'application/octet-stream'});
      fs.createReadStream(file).pipe(res);
    });
    server.listen(port, '127.0.0.1', () => resolve(server));
  });
}

(async () => {
  fs.mkdirSync(path.join(spec, 'pages'), {recursive: true});
  for (const s of screens) fs.writeFileSync(path.join(spec, 'pages', s.id + '.html'), page(s));
  fs.writeFileSync(path.join(spec, 'index.html'), index());
  fs.writeFileSync(path.join(spec, 'CHECKLIST.md'), checklist());
  const port = Number(process.env.PORT || 8766);
  const server = await serve(port);
  const base = `http://127.0.0.1:${port}/docs/ux/spec`;
  if (process.argv.includes('--serve')) { console.log(`${base}/index.html`); return; }

  const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
  const browser = await chromium.launch();
  const context = await browser.newContext({reducedMotion: 'reduce'});
  await context.route('**/*', r => new URL(r.request().url()).hostname === '127.0.0.1' ? r.continue() : r.abort());
  const p = await context.newPage();
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  const shots = path.join(spec, 'shots');
  fs.mkdirSync(shots, {recursive: true});
  const results = [];
  for (const w of widths) {
    await p.setViewportSize({width: w, height: w === 768 ? 1024 : 1000});
    for (const s of screens) {
      await p.goto(`${base}/pages/${s.id}.html`);
      await p.waitForLoadState('networkidle');
      const m = await p.evaluate(() => ({
        document: document.documentElement.scrollWidth,
        missingLabels: [...document.querySelectorAll('input:not([type=hidden]),textarea,select')].filter(e => !e.labels?.length && !e.getAttribute('aria-label')).map(e => e.name || e.id || e.type),
        duplicateIds: [...document.querySelectorAll('[id]')].map(e => e.id).filter((id, i, a) => a.indexOf(id) !== i),
        css: getComputedStyle(document.querySelector('.app-shell')).display !== 'inline',
      }));
      const ok = m.document <= w && !m.missingLabels.length && !m.duplicateIds.length && m.css;
      results.push({width: w, id: s.id, ok, ...m});
      console.log(`${ok ? 'PASS' : 'FAIL'} ${w}px ${s.id}${ok ? '' : ' ' + JSON.stringify(m)}`);
      await p.screenshot({path: path.join(shots, `${w}-${s.id}.png`), fullPage: true});
    }
  }
  fs.writeFileSync(path.join(shots, 'results.json'), JSON.stringify({results, pageErrors: errors}, null, 1) + '\n');
  await browser.close();
  server.close();
  if (errors.length || results.some(r => !r.ok)) process.exitCode = 1;
})();
