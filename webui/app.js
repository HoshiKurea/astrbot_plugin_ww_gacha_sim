import { createPortraits } from './portraits.mjs';
import { createResources } from './resources.mjs';
/* AstrBot Plugin Pages bridge is the only network boundary for this page. */
import { RARITIES, STANDARD_PROGRESSION, editorContent, percent, rateFromPercent,
  characterPercent, setRatePercent, setCandidate, setUp, rateError, pityError } from './pool-editor.mjs';
const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);
const state = { revision: 0, version: '', pools: [], selectedPool: null,
  groups: [], group: '', items: [], itemTotal: 0, itemPage: 1, selectedItem: null,
  editorContent: null, catalogByGroup: new Map(), catalogRequest: 0, advancedDirty: false };
const jsonFields = [
  ['pool-rates', 'probability_settings'],
  ['pool-progression', 'probability_progression'],
  ['pool-included', 'included_item_ids'],
  ['pool-up', 'rate_up_item_ids'],
];

function toast(message, error = false) {
  const box = $('toast');
  box.textContent = message;
  box.classList.toggle('error', error);
  box.classList.add('show');
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.remove('show'), 5200);
}

let confirmResolve = null;
function askConfirm(message) {
  if (confirmResolve) confirmResolve(false);
  $('confirm-message').textContent = message;
  $('confirm-overlay').classList.remove('hidden');
  $('confirm-accept').focus();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function closeConfirm(accepted) {
  $('confirm-overlay').classList.add('hidden');
  const resolve = confirmResolve;
  confirmResolve = null;
  if (resolve) resolve(accepted);
}

function resultBody(result) {
  if (result && typeof result === 'object' && 'code' in result) {
    if (result.code !== 0 && result.code !== 200) {
      throw new Error(result.message || result.msg || '请求失败');
    }
    return result.data ?? result;
  }
  return result?.data && result?.status === 'success' ? result.data : result;
}

async function get(path, params = {}) { return resultBody(await bridge.apiGet(path, params)); }
async function post(path, body) { return resultBody(await bridge.apiPost(path, body)); }
const portraits = createPortraits({get, post, async choose(row) {
  if (!state.group) throw new Error('请先选择配置组');
  const saved = await post('portraits/import', {group: state.group, source: row.portrait_url});
  $('item-portrait').value = saved.portrait_url;
  if (row.type !== 'other') $('item-type').value = row.type;
  previewItem(); toast('立绘已保存到插件本地。请填写名称和星级后保存物品。');
}});
const resources = createResources({root: $('resources-view'), get, post, pools: () => state.pools,
  thumbnail: portraits.thumbnail, toast,
  upload: async (path, file) => resultBody(await bridge.upload(path, file)),
  download: async (path, params, filename) => {
    if (typeof bridge.download !== 'function') throw new Error('当前 AstrBot 不支持页面下载，请更新宿主版本。');
    return bridge.download(path, params, filename);
  }});
function previewItem() {
  $('item-preview').replaceChildren(portraits.thumbnail($('item-portrait').value.trim(), $('item-name').value || '立绘预览', true));
}
function errorText(error) { return error?.message || String(error); }
function fail(error) {
  const message = errorText(error);
  toast(message.includes('409') ? '数据已变化，请刷新后再编辑。' : message, true);
}
function clone(value) { return JSON.parse(JSON.stringify(value)); }
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function clearErrors() { document.querySelectorAll('[data-error]').forEach((el) => { el.textContent = ''; }); }
function fieldErrors(errors) {
  clearErrors();
  for (const [field, message] of Object.entries(errors || {})) {
    const target = document.querySelector(`[data-error="${field}"]`);
    if (target) target.textContent = message;
  }
}

async function refresh(keepSelection = true) {
  state.catalogByGroup.clear();
  const data = await get('pools/list');
  state.revision = data.revision;
  state.version = data.version;
  state.pools = data.pools || [];
  resources.updatePools();
  $('revision').textContent = `R${state.revision}`;
  $('version').textContent = (state.version || '').slice(0, 16) || '—';
  $('pool-count').textContent = String(state.pools.length).padStart(2, '0');
  const selected = keepSelection && state.selectedPool
    ? state.pools.find((pool) => pool.filename === state.selectedPool.filename) : null;
  state.selectedPool = selected || state.pools[0] || null;
  renderPools();
  fillPool();
  state.groups = [...new Set(state.pools.map((pool) => pool.content.config_group || 'default'))].sort();
  if (!state.groups.includes(state.group)) state.group = state.groups[0] || '';
  renderGroups();
  await loadItems();
  const health = await get('health');
  $('health').textContent = health.database === 'ok' ? '运行正常' : '需要检查';
  $('connection').textContent = '已连接 AstrBot';
  $('connection').classList.remove('error');
}

function renderPools() {
  const list = $('pool-list'); list.replaceChildren();
  state.pools.forEach((pool, index) => {
    const card = node('button', 'pool-card'); card.type = 'button';
    if (pool.filename === state.selectedPool?.filename) card.classList.add('active');
    card.append(node('span', 'card-index', `POOL / ${String(index + 1).padStart(2, '0')}`),
      node('strong', 'card-title', pool.content.name || '未命名卡池'));
    const meta = node('span', 'card-meta');
    meta.append(node('span', '', pool.content.config_group || 'default'),
      node('span', pool.content.enable ? 'enabled' : 'disabled', pool.content.enable ? '● 已启用' : '○ 已停用'));
    card.append(meta);
    card.addEventListener('click', () => { state.selectedPool = pool; renderPools(); fillPool(); });
    list.append(card);
  });
  if (!state.pools.length) list.append(node('p', 'rail-foot', '暂无卡池。点击右上角 ＋ 新建。'));
}

function fillPool() {
  clearErrors();
  const pool = state.selectedPool;
  const content = pool?.content || {};
  state.editorContent = editorContent(content);
  state.advancedDirty = false;
  $('advanced-config').open = false;
  $('candidate-search').value = '';
  $('candidate-selected-only').checked = false;
  $('pool-editor-title').textContent = pool ? (content.name || '新卡池') : '新建卡池';
  $('pool-state').textContent = pool ? (content.enable ? '运行中' : '已停用') : '草稿';
  $('pool-name').value = content.name || '';
  $('pool-filename').value = pool?.filename || '';
  $('pool-filename').disabled = Boolean(pool?.filename);
  $('pool-group').value = content.config_group || 'default';
  $('pool-pity').value = content.pity_group_id || '';
  $('pool-enabled').checked = Boolean(content.enable);
  renderRates();
  renderPity();
  syncAdvancedFields();
  renderCandidates();
  loadCatalog($('pool-group').value.trim()).catch((error) => {
    $('catalog-status').textContent = '物品目录加载失败，请刷新后重试。';
    fail(error);
  });
  $('delete-pool').disabled = !pool?.filename;
}

function renderRates(skipNumber = null) {
  const rates = state.editorContent.probability_settings;
  document.querySelectorAll('.rate-control').forEach((row) => {
    const key = row.dataset.rate;
    const value = key === 'four_star_character_rate' ? characterPercent(state.editorContent)
      : percent(rates[key]);
    if (row.querySelector('.rate-number') !== skipNumber) row.querySelector('.rate-number').value = value;
    row.querySelector('.rate-slider').value = value;
    row.querySelector('.rate-slider').style.setProperty('--fill', `${Math.min(100, Math.max(0, value))}%`);
  });
  const warning = rateError(state.editorContent);
  const three = percent(rates.base_3star_rate);
  $('rate-summary').textContent = warning || `三星基础概率自动补齐：${three}% · 五星 ${percent(rates.base_5star_rate)}% + 四星 ${percent(rates.base_4star_rate)}% + 三星 ${three}% = 100%`;
  $('rate-summary').classList.toggle('invalid', Boolean(warning));
}

function renderPity() {
  const progression = state.editorContent.probability_progression;
  $('pity-4star').value = progression['4star'].hard_pity_pull;
  $('pity-5star').value = progression['5star'].hard_pity_pull;
  document.querySelector('[data-error="probability_progression"]').textContent = pityError(state.editorContent);
  const list = $('soft-pity-list');
  list.replaceChildren();
  const intervals = progression['5star'].soft_pity;
  if (!Array.isArray(intervals) || !intervals.length) {
    list.append(node('p', 'empty-hint', '尚未设置软保底；五星概率在硬保底前保持基础值。'));
    return;
  }
  intervals.forEach((interval, index) => {
    const row = node('div', 'soft-pity-row');
    row.append(node('span', 'soft-index', String(index + 1).padStart(2, '0')));
    for (const [field, label, value, step] of [
      ['start_pull', '起始抽数', interval.start_pull, '1'],
      ['end_pull', '结束抽数', interval.end_pull, '1'],
      ['increment', '每抽增加 %', percent(interval.increment), 'any'],
    ]) {
      const wrapper = node('label', 'soft-field');
      wrapper.append(node('span', '', label));
      const input = node('input');
      input.type = 'number'; input.min = field === 'increment' ? '0' : '1';
      input.max = field === 'increment' ? '100' : '';
      input.step = step; input.required = true; input.value = value;
      input.dataset.index = index; input.dataset.field = field;
      wrapper.append(input); row.append(wrapper);
    }
    const remove = node('button', 'button soft-remove', '移除');
    remove.type = 'button'; remove.dataset.index = index;
    row.append(remove); list.append(row);
  });
}

function syncAdvancedFields() {
  if (state.advancedDirty) return;
  for (const [element, field] of jsonFields) {
    $(element).value = JSON.stringify(state.editorContent[field], null, 2);
  }
}

function applyAdvanced() {
  const parsed = {};
  const errors = {};
  for (const [element, field] of jsonFields) {
    try {
      parsed[field] = JSON.parse($(element).value);
      if (!parsed[field] || Array.isArray(parsed[field]) || typeof parsed[field] !== 'object') {
        throw new Error('请输入 JSON 对象');
      }
    } catch (error) { errors[field] = errorText(error); }
  }
  if (Object.keys(errors).length) {
    fieldErrors(errors);
    $('advanced-config').open = true;
    return false;
  }
  state.editorContent = editorContent({ ...state.editorContent, ...parsed });
  state.advancedDirty = false;
  clearErrors();
  renderRates(); renderPity(); renderCandidates(); syncAdvancedFields();
  return true;
}

function prepareStructuredEdit() {
  if (!state.advancedDirty) return true;
  if (applyAdvanced()) return true;
  toast('请先修正高级 JSON，再继续调整表单。', true);
  return false;
}

async function loadCatalog(group) {
  const request = ++state.catalogRequest;
  if (!group) { renderCandidates(); return; }
  if (state.catalogByGroup.has(group)) { renderCandidates(); return; }
  $('catalog-status').textContent = `正在读取「${group}」的物品…`;
  const items = [];
  for (let page = 1; page <= 100; page += 1) {
    const result = await get('items/list', { group, page, page_size: 100 });
    items.push(...(result.items || []));
    if (items.length >= result.total || !(result.items || []).length) break;
  }
  state.catalogByGroup.set(group, items);
  if (request === state.catalogRequest && $('pool-group').value.trim() === group) renderCandidates();
}

function renderCandidates() {
  const group = $('pool-group').value.trim();
  $('catalog-group').textContent = group || '未填写';
  const catalog = state.catalogByGroup.get(group);
  const container = $('candidate-groups');
  const positions = new Map([...container.querySelectorAll('.rarity-panel')].map((panel) =>
    [panel.dataset.rarity, panel.querySelector('.candidate-rows')?.scrollTop || 0]));
  const active = container.contains(document.activeElement) ? {
    id: document.activeElement.dataset.id,
    rarity: document.activeElement.dataset.rarity,
    kind: document.activeElement.dataset.kind,
  } : null;
  container.replaceChildren();
  if (!group) { $('catalog-status').textContent = '请先填写物品配置组。'; return; }
  if (!catalog) { $('catalog-status').textContent = `正在读取「${group}」的物品…`; return; }
  $('catalog-status').textContent = `已加载 ${catalog.length} 项物品；勾选只会修改当前卡池，保存后生效。`;
  const search = $('candidate-search').value.trim().toLocaleLowerCase();
  const selectedOnly = $('candidate-selected-only').checked;
  for (const rarity of RARITIES) {
    const selected = state.editorContent.included_item_ids[rarity] || [];
    const up = state.editorContent.rate_up_item_ids[rarity] || [];
    const available = catalog.filter((item) => item.rarity === rarity);
    const known = new Set(available.map((item) => item.external_id));
    const missing = selected.filter((id) => !known.has(id))
      .map((id) => ({ external_id: id, name: id, missing: true, type: '' }));
    const entries = [...available, ...missing]
      .filter((item) => (!selectedOnly || selected.includes(item.external_id))
        && (!search || `${item.name} ${item.external_id}`.toLocaleLowerCase().includes(search)));
    const section = node('section', 'rarity-panel');
    section.dataset.rarity = rarity;
    const header = node('div', 'rarity-heading');
    header.append(node('strong', '', `${rarity[0]} 星物品`),
      node('span', '', `已选 ${selected.length} / 可用 ${available.length}`));
    section.append(header);
    const rows = node('div', 'candidate-rows');
    if (!entries.length) rows.append(node('p', 'empty-hint', search ? '没有匹配的物品。' : '此星级暂无物品，请先在「角色与武器」页登记。'));
    entries.forEach((item) => {
      const row = node('div', 'candidate-row');
      if (item.missing) row.classList.add('missing');
      const include = node('label', 'candidate-main');
      const checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.checked = selected.includes(item.external_id);
      checkbox.dataset.kind = 'include'; checkbox.dataset.rarity = rarity;
      checkbox.dataset.id = item.external_id;
      const name = node('span', 'candidate-name', item.name);
      const meta = node('small', '', item.missing ? '当前配置组中未找到，请移除或补齐物品'
        : `${item.type === 'character' ? '角色' : '武器'} · ${item.external_id}`);
      name.append(meta); include.append(checkbox, portraits.thumbnail(item.portrait_url, item.name), name); row.append(include);
      if (rarity !== '3star') {
        const upLabel = node('label', 'up-toggle');
        const upBox = node('input'); upBox.type = 'checkbox';
        upBox.checked = up.includes(item.external_id);
        upBox.disabled = !selected.includes(item.external_id);
        upBox.dataset.kind = 'up'; upBox.dataset.rarity = rarity;
        upBox.dataset.id = item.external_id;
        upLabel.append(upBox, node('span', '', 'UP')); row.append(upLabel);
      }
      rows.append(row);
    });
    section.append(rows); container.append(section);
    rows.scrollTop = positions.get(rarity) || 0;
  }
  if (active) {
    const next = [...container.querySelectorAll('input')].find((input) =>
      input.dataset.id === active.id && input.dataset.rarity === active.rarity
      && input.dataset.kind === active.kind);
    next?.focus({ preventScroll: true });
  }
}

function poolDraft() {
  if (state.advancedDirty && !applyAdvanced()) throw new Error('请修正高级 JSON 字段');
  if (!$('pool-form').checkValidity()) {
    $('pool-form').reportValidity();
    throw new Error('请先填写或修正表单中的数值');
  }
  const content = clone(state.editorContent);
  content.name = $('pool-name').value.trim();
  content.config_group = $('pool-group').value.trim();
  content.pity_group_id = $('pool-pity').value.trim();
  content.enable = $('pool-enabled').checked;
  const warning = rateError(content);
  if (warning && content.enable) {
    fieldErrors({ probability_settings: warning });
    throw new Error(warning);
  }
  const pityWarning = pityError(content);
  if (pityWarning && content.enable) {
    fieldErrors({ probability_progression: pityWarning });
    throw new Error(pityWarning);
  }
  return { filename: $('pool-filename').value.trim(), content };
}

async function validatePool() {
  try {
    const draft = poolDraft();
    const result = await post('pools/validate', draft);
    fieldErrors(result.errors);
    toast(result.valid ? '配置校验通过。' : '配置未通过校验，请查看字段提示。', !result.valid);
    return result.valid;
  } catch (error) { fail(error); return false; }
}

async function savePool(event) {
  event.preventDefault();
  try {
    const draft = poolDraft();
    const result = await post('pools/save', { ...draft, expected_revision: state.revision });
    state.selectedPool = { filename: draft.filename, content: result.pool };
    await refresh();
    toast('卡池已保存并生效。');
  } catch (error) { fail(error); }
}

async function deletePool() {
  const pool = state.selectedPool;
  if (!pool?.filename || !await askConfirm(`确定删除「${pool.content.name}」？此操作不可撤销。`)) return;
  try {
    await post('pools/delete', { filename: pool.filename, expected_revision: state.revision });
    state.selectedPool = null;
    await refresh(false);
    toast('卡池已删除。');
  } catch (error) { fail(error); }
}

function newPool() {
  const sample = state.pools[0]?.content;
  state.selectedPool = { filename: '', content: sample
    ? { ...clone(sample), cp_id: '', name: '', enable: false, pity_group_id: '' }
    : { cp_id: '', name: '', config_group: 'default', enable: false, pity_group_id: '',
      probability_settings: {}, probability_progression: {}, included_item_ids: {}, rate_up_item_ids: {} } };
  renderPools(); fillPool(); $('pool-name').focus();
}

function renderGroups() {
  const select = $('item-group'); select.replaceChildren();
  const suggestions = $('pool-group-options'); suggestions.replaceChildren();
  state.groups.forEach((group) => {
    const option = node('option', '', group); option.value = group; select.append(option);
    const suggestion = node('option'); suggestion.value = group; suggestions.append(suggestion);
  });
  select.value = state.group;
}

async function loadItems() {
  if (!state.group) { state.items = []; state.itemTotal = 0; renderItems(); return; }
  const data = await get('items/list', { group: state.group, page: state.itemPage, page_size: 30 });
  state.items = data.items || [];
  state.itemTotal = data.total || 0;
  state.selectedItem = state.items.find((item) => item.external_id === state.selectedItem?.external_id) || null;
  renderItems(); fillItem();
}

function renderItems() {
  $('item-count').textContent = String(state.itemTotal).padStart(2, '0');
  const list = $('item-list'); list.replaceChildren();
  state.items.forEach((item) => {
    const card = node('button', 'item-card'); card.type = 'button';
    if (item.external_id === state.selectedItem?.external_id) card.classList.add('active');
    card.append(portraits.thumbnail(item.portrait_url, item.name), node('strong', 'card-title', item.name));
    const meta = node('span', 'card-meta');
    meta.append(node('span', '', `${item.rarity} · ${item.type === 'character' ? '角色' : '武器'}`),
      node('span', '', item.external_id));
    card.append(meta);
    card.addEventListener('click', () => { state.selectedItem = item; renderItems(); fillItem(); });
    list.append(card);
  });
  if (!state.items.length) list.append(node('p', 'rail-foot', '此配置组暂无物品。'));
  $('items-page').textContent = `${state.itemPage} / ${Math.max(1, Math.ceil(state.itemTotal / 30))}`;
  $('items-prev').disabled = state.itemPage <= 1;
  $('items-next').disabled = state.itemPage * 30 >= state.itemTotal;
}

function fillItem() {
  const item = state.selectedItem || {};
  $('item-editor-title').textContent = item.name || '登记角色或武器';
  $('item-id').value = item.external_id || '';
  $('item-id').disabled = Boolean(item.external_id);
  $('item-name').value = item.name || '';
  $('item-rarity').value = item.rarity || '3star';
  $('item-type').value = item.type || 'character';
  $('item-affiliation').value = item.affiliated_type || '';
  $('item-portrait').value = item.portrait_url || '';
  previewItem();
  $('item-upload').value = '';
  $('delete-item').disabled = !item.external_id;
}

function itemDraft() {
  return { external_id: $('item-id').value.trim(), name: $('item-name').value.trim(),
    rarity: $('item-rarity').value, type: $('item-type').value,
    affiliated_type: $('item-affiliation').value.trim(), portrait_url: $('item-portrait').value.trim() };
}

async function uploadPortrait() {
  const file = $('item-upload').files?.[0];
  if (!file) return;
  if (!state.group) { toast('请先创建卡池配置组。', true); return; }
  if (file.size > 10 * 1024 * 1024) { toast('图片超过 10 MiB。', true); return; }
  try {
    const response = resultBody(await bridge.upload(`portraits/upload/${encodeURIComponent(state.group)}`, file));
    $('item-portrait').value = response.portrait_url;
    previewItem();
    toast('立绘已上传。保存物品后即可使用。');
  } catch (error) { fail(error); }
}

async function saveItem(event) {
  event.preventDefault();
  if (!state.group) { toast('请先创建卡池配置组。', true); return; }
  try {
    const result = await post('items/save', { group: state.group, item: itemDraft(), expected_revision: state.revision });
    state.catalogByGroup.delete(state.group);
    await refresh();
    state.selectedItem = state.items.find((item) => item.external_id === result.external_id) || null;
    renderItems(); fillItem();
    toast('物品已保存并生效。');
  } catch (error) { fail(error); }
}

async function deleteItem() {
  const id = state.selectedItem?.external_id;
  if (!id || !await askConfirm(`确定删除「${state.selectedItem.name}」？被卡池引用的物品不能删除。`)) return;
  try {
    await post('items/delete', { group: state.group, external_id: id, expected_revision: state.revision });
    state.catalogByGroup.delete(state.group);
    state.selectedItem = null;
    await refresh();
    toast('物品已删除。');
  } catch (error) { fail(error); }
}

function bind() {
  $('browse-art').onclick = () => portraits.open();
  $('item-portrait').addEventListener('change', previewItem);
  $('localize-portrait').onclick = async () => {
    const source = $('item-portrait').value.trim();
    if (!/^https?:\/\//.test(source)) {
      toast('先填写远程立绘地址，或从素材库选图。', true); return;
    }
    if (!state.group) { toast('请先选择配置组。', true); return; }
    const button = $('localize-portrait'); button.disabled = true;
    try {
      const result = await post('portraits/import', {group: state.group, source});
      $('item-portrait').value = result.portrait_url; previewItem();
      toast('立绘已保存到插件本地。请点击「保存物品」应用修改。');
    } catch (error) { fail(error); }
    finally { button.disabled = false; }
  };
  $('confirm-cancel').addEventListener('click', () => closeConfirm(false));
  $('confirm-accept').addEventListener('click', () => closeConfirm(true));
  $('confirm-overlay').addEventListener('click', (event) => {
    if (event.target === $('confirm-overlay')) closeConfirm(false);
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && confirmResolve) closeConfirm(false);
  });
  $('refresh').addEventListener('click', () => refresh().then(() => toast('数据已刷新。')).catch(fail));
  $('new-pool').addEventListener('click', newPool);
  document.querySelector('.rate-grid').addEventListener('input', (event) => {
    const row = event.target.closest('.rate-control');
    if (!row || !event.target.validity.valid || event.target.value === '') return;
    const key = row.dataset.rate;
    const value = event.target.value;
    if (!prepareStructuredEdit()) return;
    try {
      setRatePercent(state.editorContent, key, value);
      renderRates(event.target.matches('.rate-number') ? event.target : null);
      syncAdvancedFields();
      document.querySelector('[data-error="probability_settings"]').textContent = '';
    } catch (error) { fail(error); }
  });
  document.querySelector('.rate-grid').addEventListener('change', (event) => {
    if (event.target.matches('.rate-number') && event.target.validity.valid) renderRates();
  });
  $('preset-rates').addEventListener('click', () => {
    if (!prepareStructuredEdit()) return;
    setRatePercent(state.editorContent, 'base_5star_rate', 0.8);
    setRatePercent(state.editorContent, 'base_4star_rate', 6);
    renderRates(); syncAdvancedFields(); toast('已填入标准基础概率；保存后生效。');
  });
  for (const [id, rarity] of [['pity-4star', '4star'], ['pity-5star', '5star']]) {
    $(id).addEventListener('input', (event) => {
      if (!event.target.validity.valid || !event.target.value) return;
      const value = Number(event.target.value);
      if (!prepareStructuredEdit()) return;
      state.editorContent.probability_progression[rarity].hard_pity_pull = value;
      document.querySelector('[data-error="probability_progression"]').textContent =
        pityError(state.editorContent);
      syncAdvancedFields();
    });
  }
  $('preset-pity').addEventListener('click', () => {
    if (!prepareStructuredEdit()) return;
    for (const rarity of ['4star', '5star']) {
      state.editorContent.probability_progression[rarity] = {
        ...state.editorContent.probability_progression[rarity],
        ...clone(STANDARD_PROGRESSION[rarity]),
      };
    }
    renderPity(); syncAdvancedFields(); toast('已填入 80 抽保底预设；保存后生效。');
  });
  $('add-soft-pity').addEventListener('click', () => {
    if (!prepareStructuredEdit()) return;
    const five = state.editorContent.probability_progression['5star'];
    if (!Array.isArray(five.soft_pity)) five.soft_pity = [];
    const hard = Number(five.hard_pity_pull);
    const lastEnd = Math.max(0, ...five.soft_pity.map((part) => Number(part.end_pull) || 0));
    const start = five.soft_pity.length ? lastEnd + 1 : Math.max(1, hard - 14);
    if (start >= hard) { toast('软保底区间必须在五星硬保底之前。', true); return; }
    five.soft_pity.push({ start_pull: start, end_pull: Math.min(hard - 1, start + 4), increment: 0.04 });
    renderPity(); syncAdvancedFields();
  });
  $('soft-pity-list').addEventListener('click', (event) => {
    const button = event.target.closest('.soft-remove');
    if (!button) return;
    const index = Number(button.dataset.index);
    if (!prepareStructuredEdit()) return;
    state.editorContent.probability_progression['5star'].soft_pity.splice(index, 1);
    renderPity(); syncAdvancedFields();
  });
  $('soft-pity-list').addEventListener('change', (event) => {
    const input = event.target;
    if (!input.dataset.field || !input.validity.valid || !input.value) return;
    const { field, index } = input.dataset;
    const value = field === 'increment' ? rateFromPercent(input.value) : Number(input.value);
    if (!prepareStructuredEdit()) return;
    const interval = state.editorContent.probability_progression['5star'].soft_pity[Number(index)];
    if (!interval) { renderPity(); toast('软保底区间已变化，请重新编辑。', true); return; }
    interval[field] = value;
    document.querySelector('[data-error="probability_progression"]').textContent =
      pityError(state.editorContent);
    syncAdvancedFields();
  });
  $('candidate-search').addEventListener('input', renderCandidates);
  $('candidate-selected-only').addEventListener('change', renderCandidates);
  $('candidate-groups').addEventListener('change', (event) => {
    const input = event.target;
    if (input.type !== 'checkbox') return;
    const { kind, rarity, id } = input.dataset;
    const checked = input.checked;
    if (!prepareStructuredEdit()) return;
    try {
      if (kind === 'include') {
        setCandidate(state.editorContent, rarity, id, checked);
      } else {
        setUp(state.editorContent, rarity, id, checked);
      }
      renderCandidates(); syncAdvancedFields();
      document.querySelector('[data-error="included_item_ids"]').textContent = '';
      document.querySelector('[data-error="rate_up_item_ids"]').textContent = '';
    } catch (error) { fail(error); }
  });
  let groupTimer;
  $('pool-group').addEventListener('input', () => {
    clearTimeout(groupTimer);
    renderCandidates();
    groupTimer = setTimeout(() => loadCatalog($('pool-group').value.trim()).catch((error) => {
      $('catalog-status').textContent = '物品目录加载失败，请刷新后重试。'; fail(error);
    }), 350);
  });
  $('advanced-config').addEventListener('toggle', () => {
    if ($('advanced-config').open) syncAdvancedFields();
  });
  for (const [element] of jsonFields) {
    $(element).addEventListener('input', () => { state.advancedDirty = true; });
  }
  $('apply-advanced').addEventListener('click', () => {
    if (applyAdvanced()) toast('高级 JSON 已应用到表单；保存后生效。');
  });
  $('pool-form').addEventListener('submit', savePool);
  $('validate-pool').addEventListener('click', validatePool);
  $('delete-pool').addEventListener('click', deletePool);
  $('new-item').addEventListener('click', () => { state.selectedItem = null; renderItems(); fillItem(); $('item-name').focus(); });
  $('item-group').addEventListener('change', (event) => {
    state.group = event.target.value; state.itemPage = 1; state.selectedItem = null; loadItems().catch(fail);
  });
  $('item-form').addEventListener('submit', saveItem);
  $('delete-item').addEventListener('click', deleteItem);
  $('item-upload').addEventListener('change', uploadPortrait);
  $('items-prev').addEventListener('click', () => { state.itemPage -= 1; loadItems().catch(fail); });
  $('items-next').addEventListener('click', () => { state.itemPage += 1; loadItems().catch(fail); });
  document.querySelectorAll('.tab').forEach((tab) => tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
    $('pools-view').classList.toggle('hidden', tab.dataset.view !== 'pools');
    $('items-view').classList.toggle('hidden', tab.dataset.view !== 'items');
    $('resources-view').classList.toggle('hidden', tab.dataset.view !== 'resources');
    resources.setVisible(tab.dataset.view === 'resources');
  }));
}

bind();
if (!bridge) {
  $('connection').textContent = '请从 AstrBot 插件页面打开';
  $('connection').classList.add('error');
  toast('未找到 AstrBot 页面桥接。请从控制台的插件页面打开。', true);
} else {
  bridge.ready().then(() => refresh()).catch((error) => {
    $('connection').textContent = '连接失败'; $('connection').classList.add('error'); fail(error);
  });
}
