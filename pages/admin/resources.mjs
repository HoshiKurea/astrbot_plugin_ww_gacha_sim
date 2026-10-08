/* Pool artwork readiness and portable offline artwork, through the host bridge. */
export function jobPresentation(job) {
  const states = {running: '资源处理中', done: '资源处理完成', partial: '部分立绘准备失败',
    cancelled: '已停止准备', failed: '资源处理失败'};
  return {active: job?.state === 'running', text: job ? states[job.state] || '等待处理' : '尚未开始资源准备',
    progress: job?.total ? Math.min(100, Math.round(job.completed / job.total * 100)) : 0};
}

export function createResources({root, get, post, upload, download, pools, thumbnail, toast}) {
  root.innerHTML = `<aside class="rail resource-rail"><div class="section-head"><div><small>ARTWORK READINESS</small><h2>卡池资源</h2></div></div>
    <label class="field" for="resource-pool">选择卡池<select id="resource-pool"></select></label>
    <p>提前准备该卡池的立绘，让首次抽卡也能直接发图。准备资源不会启用卡池或改变抽卡记录。</p>
    <div class="resource-guide"><small>离线使用 / 3 步</small><ol><li>在可联网的 AstrBot 中准备卡池资源。</li><li>导出离线 ZIP，并传到目标服务器。</li><li>在目标 AstrBot 中导入 ZIP，立绘即可离线使用。</li></ol><p>资源包只包含立绘。两端应使用相同的卡池和物品配置。</p></div>
    </aside><div class="editor-panel"><div class="section-head editor-head"><div><small>RESOURCE CONTROL</small><h2>让立绘提前就绪</h2></div><button class="button ghost" type="button" data-refresh>刷新状态</button></div>
    <div class="resource-metrics"><div><strong data-total>—</strong><span>所需立绘</span></div><div><strong data-ready>—</strong><span>当前可用</span></div><div><strong data-pinned>—</strong><span>已离线保存</span></div></div>
    <p class="resource-note" data-coverage role="status">选择卡池后查看资源状态。</p>
    <div class="resource-actions"><button type="button" class="button primary" data-prepare>准备卡池资源 ↗</button><button type="button" class="button ghost" data-cancel disabled>停止准备</button><button type="button" class="button ghost" data-export disabled>导出离线 ZIP</button></div>
    <section class="resource-job" aria-label="资源处理进度"><div><span data-job-title>尚未开始资源准备</span><span data-progress-text></span></div><progress data-progress max="100" value="0" aria-label="资源准备进度"></progress><p data-job-note role="status" aria-live="polite"></p><ul data-errors class="resource-errors"></ul></section>
    <section class="resource-import"><div><small>OFFLINE ARTWORK</small><h3>导入已有资源包</h3><p>使用本插件导出的 ZIP，最大 64 MiB。导入后长期保存，不受普通缓存过期影响。</p></div><button type="button" class="button ghost" data-upload>选择离线 ZIP</button><input id="resource-upload" type="file" accept=".zip,application/zip" hidden></section>
    <div class="resource-cards" data-items></div></div>`;
  const q = selector => root.querySelector(selector);
  const selection = q('#resource-pool');
  let visible = false, serial = 0, timer = null, watched = null, coverage = null, action = false;
  const notified = new Set();
  function element(tag, text, className = '') {
    const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
  }
  function buttons() {
    const active = jobPresentation(watched).active;
    q('[data-prepare]').disabled = action || active || !selection.value || !coverage?.total;
    q('[data-prepare]').textContent = watched?.state === 'partial' || watched?.state === 'failed' ? '重试准备资源 ↗' : '准备卡池资源 ↗';
    q('[data-cancel]').disabled = action || !active || watched?.cancel || watched?.kind !== 'prepare';
    q('[data-export]').disabled = action || !coverage?.total || coverage.ready !== coverage.total;
    q('#resource-upload').disabled = action || active;
    q('[data-upload]').disabled = action || active;
  }
  function showJob(job) {
    watched = job;
    const view = jobPresentation(job);
    q('[data-job-title]').textContent = (job?.kind === 'import' ? '离线包 · ' : '') + view.text;
    q('[data-progress-text]').textContent = job?.total ? `${job.completed} / ${job.total}` : view.active ? '处理中…' : '';
    q('[data-progress]').value = view.progress;
    q('[data-job-note]').textContent = view.active ? (job.cancel ? '正在停止，已开始的共享下载会继续保存。' : '任务在后台运行，可以离开此页；已就绪的立绘会自动复用。')
      : job?.state === 'done' ? '资源已保存，抽卡和物品预览可以直接使用。'
      : job?.state === 'partial' ? '已成功的立绘保留，重试只会准备尚未就绪的资源。' : '';
    q('[data-errors]').replaceChildren(...(job?.failed || []).map(row => element('li', `${row.name}：${row.message}`)));
    buttons();
  }
  function showCoverage(data) {
    coverage = data;
    for (const key of ['total', 'ready', 'pinned']) q(`[data-${key}]`).textContent = data[key];
    q('[data-coverage]').textContent = `${data.name} · ${data.ready} / ${data.total} 张立绘可用${data.without_portrait ? ` · ${data.without_portrait} 个物品未配置立绘` : ''}。已离线保存的素材长期保留。`;
    const cards = q('[data-items]'); cards.replaceChildren();
    for (const item of data.items || []) {
      const card = element('div', '', 'resource-card');
      if (item.ready) card.append(thumbnail(item.source, item.name));
      else card.append(element('span', '待准备', 'portrait-thumb'));
      const info = element('div', '');
      info.append(element('strong', item.name), element('span', item.pinned ? '已离线保存' : item.ready ? '缓存可用' : '尚未下载'));
      card.append(info); cards.append(card);
    }
    buttons();
  }
  function schedule() {
    clearTimeout(timer);
    if (visible && jobPresentation(watched).active) timer = setTimeout(refresh, 1200);
  }
  async function refresh() {
    clearTimeout(timer);
    const id = ++serial, poolId = selection.value;
    if (!poolId) { coverage = null; buttons(); return; }
    try {
      const data = await get('resources/status', {pool_id: poolId});
      if (id !== serial || poolId !== selection.value) return;
      showCoverage(data);
      const job = data.import_job && (!data.job || data.import_job.started_at >= data.job.started_at)
        ? data.import_job : data.job;
      if (job && !jobPresentation(job).active && !notified.has(job.id)) {
        // A very fast import can finish between the coverage and job requests.
        // Refresh coverage once after completion before stopping the poll.
        const finalData = await get('resources/status', {pool_id: poolId});
        if (id !== serial) return;
        showCoverage(finalData);
        notified.add(job.id);
        if (job.state === 'done') toast(job.kind === 'import' ? `已导入 ${job.ready} 张立绘。` : '卡池资源已准备完成。');
      }
      showJob(job);
    } catch (error) {
      if (id === serial) { q('[data-coverage]').textContent = error.message; buttons(); }
    } finally { if (id === serial) schedule(); }
  }
  async function run(callback) {
    action = true; buttons();
    try { await callback(); }
    catch (error) { toast(error.message, true); }
    finally { action = false; buttons(); schedule(); }
  }
  q('[data-prepare]').onclick = () => run(async () => {
    const data = await post('resources/prepare', {pool_id: selection.value});
    showJob(data.job); await refresh();
  });
  q('[data-cancel]').onclick = () => run(async () => {
    const data = await post('resources/cancel', {job_id: watched.id}); showJob(data.job);
  });
  q('[data-export]').onclick = () => run(async () => {
    q('[data-export]').textContent = '正在生成 ZIP…';
    try { await download('resources/export', {pool_id: selection.value}, 'wwg-offline-resources.zip'); toast('离线资源包已导出。'); }
    finally { q('[data-export]').textContent = '导出离线 ZIP'; }
  });
  q('#resource-upload').onchange = () => run(async () => {
    const input = q('#resource-upload'), file = input.files?.[0];
    try {
      if (!file) return;
      if (file.size > 64 * 1024 * 1024) throw new Error('离线资源包超过 64 MiB');
      const result = await upload('resources/import', file);
      showJob(result.job); await refresh();
    } finally { input.value = ''; }
  });
  q('[data-upload]').onclick = () => q('#resource-upload').click();
  selection.onchange = () => { watched = null; coverage = null; serial++; refresh(); };
  q('[data-refresh]').onclick = refresh;
  window.addEventListener('beforeunload', () => { visible = false; clearTimeout(timer); serial++; });
  function updatePools() {
    const previous = selection.value;
    selection.replaceChildren(...pools().map(pool => {
      const option = element('option', pool.content.name + (pool.content.enable ? '' : '（停用）'));
      option.value = pool.content.cp_id; return option;
    }));
    if ([...selection.options].some(row => row.value === previous)) selection.value = previous;
    if (visible) refresh();
  }
  return {updatePools, setVisible(value) { visible = value; clearTimeout(timer); if (visible) { updatePools(); } }};
}
