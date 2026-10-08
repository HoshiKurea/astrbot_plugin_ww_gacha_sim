// All image bytes go through the authenticated host bridge, including local art.
export function createPortraits({ get, post, choose }) {
  const cache = new Map(), queue = [];
  let active = 0;
  const el = (tag, cls, text) => {
    const n = document.createElement(tag); n.className = cls;
    if (text) n.textContent = text;
    return n;
  };
  function pump() {
    while (active < 4 && queue.length) {
      const {source, resolve, reject} = queue.shift(); active++;
      post('portraits/preview', {source}).then(resolve, reject).finally(() => { active--; pump(); });
    }
  }
  function load(source) {
    if (!cache.has(source)) {
      const result = new Promise((resolve, reject) => { queue.push({source, resolve, reject}); pump(); });
      cache.set(source, result);
      result.catch(() => cache.delete(source));
      if (cache.size > 256) cache.delete(cache.keys().next().value);
    }
    return cache.get(source);
  }
  const observed = new Set();
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {
    if (!entry.isIntersecting) return;
    observer.unobserve(entry.target);
    observed.delete(entry.target);
    entry.target.loadPreview();
  }), {rootMargin: '100px'});
  function thumbnail(source, name = '立绘', large = false) {
    const box = el('span', `portrait-thumb${large ? ' large' : ''}`, source ? '加载中' : '暂无立绘');
    if (!source) return box;
    box.loadPreview = async () => {
      try {
        const data = await load(source);
        const img = el('img', ''); img.alt = name; img.src = data.data_url;
        box.replaceChildren(img); box.title = name;
      } catch {
        box.textContent = '暂不可用'; box.title = '图片加载失败，可重新打开或检查网络与地址';
      }
    };
    observer.observe(box); observed.add(box);
    return box;
  }
  // Discard detached nodes when lists are rebuilt instead of retaining observers.
  new MutationObserver(() => {
    for (const n of observed) if (!n.isConnected) { observer.unobserve(n); observed.delete(n); }
  }).observe(document.body, {childList:true, subtree:true});

  const dialog = el('dialog', 'art-dialog');
  dialog.innerHTML = `<div class="art-heading"><div><small>VERSIONED ART LIBRARY</small><h2>选择角色与武器立绘</h2></div><button type="button" class="button ghost" data-close>关闭 ×</button></div>
    <p>素材来源：TomyJan / WutheringWaves-UIResources。选版本、看图，点击素材查看大图并选用。名称与星级需自行填写。</p>
    <div class="art-toolbar"><label>版本 / 标签 / 提交<input data-ref list="art-versions" placeholder="例如 3.6"><datalist id="art-versions"></datalist></label><button type="button" class="button primary" data-load>加载素材</button><label>查找文件<input type="search" data-search placeholder="文件名，如 Changli"></label><label>类别<select data-kind><option value="">全部素材</option><option value="character">角色相关</option><option value="weapon">武器相关</option><option value="other">其他素材</option></select></label></div>
    <label><input type="checkbox" data-art-only checked> 仅显示抽卡立绘（取消可查看背景等全部素材）</label><p data-status role="status"></p><div class="art-body"><div data-grid class="art-grid"></div><aside data-detail class="art-detail"><p>选择一张图片进行预览</p></aside></div><div class="pagination"><button type="button" class="button ghost" data-prev>上一页</button><span data-page></span><button type="button" class="button ghost" data-next>下一页</button></div>`;
  dialog.setAttribute('aria-label', '版本立绘素材库'); document.body.append(dialog);
  const q = selector => dialog.querySelector(selector);
  let rows = [], page = 1, requestId = 0, versionReady = false;
  function render() {
    const search = q('[data-search]').value.toLowerCase(), kind = q('[data-kind]').value;
    const filtered = rows.filter(r => (!kind || r.type === kind) && (!q('[data-art-only]').checked || /_UI\.png$/i.test(r.name)) && r.name.toLowerCase().includes(search));
    const pages = Math.max(1, Math.ceil(filtered.length / 24)); page = Math.min(page, pages);
    const grid = q('[data-grid]'); grid.replaceChildren();
    for (const row of filtered.slice((page - 1) * 24, page * 24)) {
      const button = el('button', 'art-tile'); button.type = 'button';
      button.append(thumbnail(row.portrait_url, row.name, true), el('span', '', row.name));
      button.onclick = () => {
        const use = el('button', 'button primary', '使用此立绘'); use.type = 'button';
        use.onclick = async () => {
          use.disabled = true; use.textContent = '正在保存到插件本地…';
          try { await choose(row); dialog.close(); }
          catch (error) { q('[data-status]').textContent = `保存失败：${error.message}`; use.disabled = false; use.textContent = '重试保存'; }
        };
        q('[data-detail]').replaceChildren(thumbnail(row.portrait_url, row.name, true), el('p', '', row.name), use);
      };
      grid.append(button);
    }
    if (!filtered.length) grid.append(el('p', '', '暂无匹配素材，尝试其他类别或关键词。'));
    q('[data-page]').textContent = `${page} / ${pages} · 共 ${filtered.length} 张`;
    q('[data-prev]').disabled = page <= 1; q('[data-next]').disabled = page >= pages;
  }
  async function refresh() {
    const id = ++requestId, ref = q('[data-ref]').value.trim();
    q('[data-load]').disabled = true;
    rows = []; page = 1; render(); q('[data-detail]').replaceChildren();
    q('[data-status]').textContent = '正在读取该版本目录…';
    try {
      let data = await get('resources/portraits', {ref});
      const started = Date.now();
      while (data.pending) {
        if (id !== requestId) return;
        q('[data-status]').textContent = `正在读取版本目录… 已等待 ${Math.floor((Date.now() - started) / 1000)} 秒。首次读取或 API 限流时较慢，请稍候。`;
        await new Promise(resolve => setTimeout(resolve, 1200));
        if (id !== requestId) return;
        data = await get('resources/portraits', {ref});
      }
      if (id !== requestId) return;
      rows = data.items; render();
      q('[data-status]').textContent = `版本 ${data.ref} · 固定提交 ${data.commit.slice(0, 12)} · ${rows.length} 张素材${data.source === "github-page" ? " · API 限流，已改用 GitHub 公开目录" : ""}`;
    } catch (error) { if (id === requestId) q('[data-status]').textContent = error.message; }
    finally { if (id === requestId) q('[data-load]').disabled = false; }
  }
  q('[data-close]').onclick = () => dialog.close();
  q('[data-load]').onclick = refresh;
  q('[data-art-only]').onchange = q('[data-search]').oninput = q('[data-kind]').onchange = () => { page = 1; render(); };
  q('[data-prev]').onclick = () => { page--; render(); };
  q('[data-next]').onclick = () => { page++; render(); };
  return {thumbnail, async open() {
    dialog.showModal();
    if (versionReady) return;
    q('[data-status]').textContent = '正在获取可选版本…';
    try {
      const data = await get('resources/versions');
      q('#art-versions').replaceChildren(...data.versions.map(v => { const option = el('option', ''); option.value = v; return option; }));
      if (!q('[data-ref]').value) q('[data-ref]').value = data.default;
      versionReady = true; await refresh();
    } catch (error) { q('[data-status]').textContent = error.message + '；也可手动填写版本后加载。'; }
  }};
}
