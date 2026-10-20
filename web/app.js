'use strict';
const $ = (id) => document.getElementById(id);
const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = (name) => '<svg class="icon" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
const art = (name) => '<svg viewBox="0 0 300 190" aria-hidden="true"><use href="/food.svg#' + esc(name) + '"/></svg>';
const money = (fen) => (fen / 100).toLocaleString('zh-CN', {maximumFractionDigits:2});
function priceText(plan, perPerson) {
  const exact = perPerson ? plan.per_person : plan.total;
  if (exact != null) return '¥' + money(exact);
  const low = perPerson ? plan.per_person_low : plan.total_low;
  const high = perPerson ? plan.per_person_high : plan.total_high;
  if (low == null) return '待核价';
  if (high == null) return '>¥' + money(low);
  if (!low) return '≤¥' + money(high);
  return '¥' + money(low) + '–' + money(high);
}
function metricValue(plan,field) {
  const value = plan.ratings && plan.ratings[field];
  if (typeof value === 'number' || /^[1-5]$/.test(String(value))) return state.catalog.descriptors[field][Number(value)-1];
  if (typeof value === 'string') return value;
  return {distance:plan.imported ? '已到店':'未提供',price:'按菜单',taste:'未评价',portion:'待核实',speed:'未评价'}[field];
}
function metricFields(plan) {
  return [['distance','距离'],['price','价格'],['taste','味道'],['portion','饭量'],['speed','吃完速度']].map(([key,label]) => ({key:key,label:label,value:metricValue(plan,key)}));
}
function ratingText(plan) { return metricFields(plan).map((item) => item.label + ' ' + item.value).join(' · '); }
function feedbackValue(field,value) {
  return typeof value === 'number' || /^[1-5]$/.test(String(value)) ? state.catalog.feedback_terms[field][Number(value)-1]:value;
}
function assistantText(text) {
  return text.replace(/(距离|价格|味道|饭量|吃完速度|速度|口味|分量|性价比)(?:评分)?\s*[:：]?\s*([1-5])(?:\s*\/\s*5|\s*分)/g,(match,label,value) => {
    const field = {距离:'distance',价格:'price',味道:'taste',饭量:'portion',吃完速度:'speed',速度:'speed',口味:'taste',分量:'portion',性价比:'value'}[label];
    const terms = ['口味','分量','性价比'].includes(label) ? state.catalog.feedback_terms:state.catalog.descriptors;
    return label + ' ' + terms[field][Number(value)-1];
  });
}
function actualPrices(plan) {
  return plan.rated ? Object.fromEntries(plan.items.filter((item) => item.price != null).map((item) => [item.dish_id,item.price/100])) : undefined;
}
const uid = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(36).slice(2);
const modeNames = {chat:'聊一聊',meal:'帮我选一餐',feedback:'记忆反馈'};
const state = {catalog:null,data:{history:[],memories:[],meals:0,feedback_count:0},scene:'canteen',tastes:[],inferred:{},plans:[],messages:[],mode:'chat',recommendMode:'basic',preview:true,stale:false,busy:false,pending:false,seen:[],drawerTab:'history',detail:null,quoteVersion:0,retry:null};
state.model = 'GLM-4-Flash';
state.defaultModel = 'GLM-4-Flash';
state.models = [];
state.activeMeal = null;
state.recommendationMealId = null;
state.primaryId = null;
state.menuImages = [];
state.visionModel = 'GLM-4V-Plus-0111';
let toastTimer;
let sidebarRefreshVersion = 0;

function persistMeal() {
  try { sessionStorage.setItem('eat-active-meal',JSON.stringify(state.activeMeal)); } catch (_) {}
}
function mealSession() {
  const previous = state.activeMeal && state.data.history.find((meal) => meal.id === state.activeMeal.mealId);
  if (!state.activeMeal || (previous && previous.completed) || Date.now() - state.activeMeal.started > 3 * 60 * 60 * 1000) {
    state.activeMeal = {session:uid(),started:Date.now(),mealId:null};
    persistMeal();
  }
  return state.activeMeal.session;
}
function finishMeal(id) {
  if (state.activeMeal && state.activeMeal.mealId === id) { state.activeMeal = null; persistMeal(); }
}

async function api(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(),path === '/api/menus/import' ? 90000:45000);
  try {
    const response = await fetch(path,{method:body === undefined ? 'GET':'POST',headers:body === undefined ? {}:{'Content-Type':'application/json'},body:body === undefined ? undefined:JSON.stringify(body),signal:controller.signal});
    let result;
    try { result = await response.json(); } catch (_) { throw new Error('本地服务返回异常，请确认服务已启动。'); }
    if (!response.ok) { const error = new Error(result.error || '请求失败，请重试。'); error.code = result.code; error.result = result; throw error; }
    return result;
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('响应超时，消息已保留，可以重试。');
    if (err instanceof TypeError) throw new Error('无法连接本地服务，请检查服务是否仍在运行。');
    throw err;
  } finally { clearTimeout(timer); }
}
function toast(text) {
  clearTimeout(toastTimer);
  $('toast').textContent = text;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; },4000);
}
function showError(text,retry,allowBasic) {
  $('error-text').textContent = text;
  $('error-box').hidden = false;
  $('fallback-button').hidden = !allowBasic;
  state.retry = retry;
}
function clearError() { $('error-box').hidden = true; state.retry = null; }
function note(text) { $('result-notice').textContent = text || ''; $('result-notice').hidden = !text; }
function readNeeds() {
  const optionalNumber = (id) => $(id).value === '' ? null:Number($(id).value);
  const words = (id) => $(id).value.split(/[,，、；;\n]+/).map((v) => v.trim()).filter(Boolean);
  return {scene:state.scene,venue_id:state.scene === 'menu' ? $('venue-select').value:'',budget:optionalNumber('budget'),minutes:optionalNumber('minutes'),people:optionalNumber('people'),tastes:state.tastes.slice(),avoid:words('avoid'),foods:words('foods'),avoid_foods:words('avoid-foods'),purpose:$('purpose').value,portion:$('portion-goal').value,pace:$('pace-goal').value,inferred:JSON.parse(JSON.stringify(state.inferred))};
}
function applyNeeds(needs) {
  state.scene = needs.scene;
  state.tastes = needs.tastes.slice();
  state.inferred = JSON.parse(JSON.stringify(needs.inferred || {}));
  ['budget','minutes','people','purpose'].forEach((key) => { $(key).value = needs[key] == null ? '':needs[key]; });
  $('portion-goal').value = needs.portion || '不限'; $('pace-goal').value = needs.pace || '不限';
  if (needs.venue_id) $('venue-select').value = needs.venue_id;
  $('avoid').value = needs.avoid.join('、');
  $('foods').value = (needs.foods || []).join('、');
  $('avoid-foods').value = (needs.avoid_foods || []).join('、');
  syncControls();
}
function syncControls() {
  syncSliders();
  document.querySelectorAll('[data-scene]').forEach((b) => {
    const yes = b.dataset.scene === state.scene;
    b.classList.toggle('selected',yes); b.setAttribute('aria-pressed',String(yes));
  });
  document.querySelectorAll('[data-taste]').forEach((b) => {
    const yes = state.tastes.includes(b.dataset.taste);
    b.classList.toggle('selected',yes); b.setAttribute('aria-pressed',String(yes));
  });
  $('venue-field').hidden = state.scene !== 'menu';
  renderConditions();
  syncMenuContext();
}
function renderConditions() {
  const n = readNeeds();
  const scene = {canteen:'食堂选择',outside:'校外餐厅',menu:'菜单点餐'}[n.scene];
  const badge = (field) => n.inferred[field] ? '<span class="inference-badge" title="' + esc(n.inferred[field].reason) + '">推测</span>':'';
  const rows = [['场景',scene,'scene'],['预算',n.budget == null ? '未设置':'¥' + n.budget + ' / 人','budget'],['时间参考',n.minutes == null ? '未设置':n.minutes + ' 分钟','minutes'],['人数',n.people == null ? '未说明':n.people + ' 人','people']];
  $('needs-summary').innerHTML = rows.map((r) => '<div class="summary-row"><span>' + r[0] + '</span><strong>' + esc(r[1]) + badge(r[2]) + '</strong></div>').join('');
  $('condition-tags').innerHTML = n.tastes.map((t) => '<span class="condition-tag">' + esc(t) + '</span>').join('') + n.avoid.map((a) => '<span class="condition-tag avoid">不吃' + esc(a) + '</span>').join('');
  $('condition-tags').insertAdjacentHTML('beforeend',n.foods.map((food) => '<span class="condition-tag">想吃：' + esc(food) + '</span>').join('') + n.avoid_foods.map((food) => '<span class="condition-tag avoid">这次不吃：' + esc(food) + '</span>').join(''));
  if (n.portion !== '不限') $('condition-tags').insertAdjacentHTML('beforeend','<span class="condition-tag">饭量：' + esc(n.portion) + badge('portion') + '</span>');
  if (n.pace !== '不限') $('condition-tags').insertAdjacentHTML('beforeend','<span class="condition-tag">' + esc(n.pace) + badge('pace') + '</span>');
  const inferred = Object.entries(n.inferred);
  $('inferred-conditions').hidden = !inferred.length;
  $('inferred-summary').textContent = inferred.length + ' 项条件按语义推测';
  const labels = {budget:'预算',minutes:'时间',people:'人数',portion:'饭量',pace:'节奏',purpose:'目的',foods:'想吃的餐品',avoid_foods:'这次不想吃'};
  $('inferred-reasons').innerHTML = inferred.map(([field,entry]) => '<p>' + esc(labels[field] || field) + '：' + esc(entry.reason) + '</p>').join('');
  ['budget','minutes','people','purpose','portion-goal','pace-goal'].forEach((id) => {
    const field = {'portion-goal':'portion','pace-goal':'pace'}[id] || id;
    $(id).classList.toggle('inferred-value',Boolean(n.inferred[field]));
    $(id).title = n.inferred[field] ? n.inferred[field].reason:'';
  });
}
function syncSliders() {
  const units = {budget:'元',minutes:'分钟',people:'人'};
  const maxima = {budget:100,minutes:120,people:12};
  Object.keys(units).forEach((id) => {
    const input = $(id), slider = $(id + '-slider');
    const value = input.value === '' ? null:Number(input.value);
    slider.max = id === 'people' ? maxima[id]:Math.max(maxima[id],Math.ceil((value || 0)/10)*10);
    slider.step = id === 'budget' && value != null && !Number.isInteger(value) ? '.01':'1';
    slider.value = value == null ? 0:value;
    slider.classList.toggle('unset',value == null);
    const label = value == null ? '未设置':value + ' ' + units[id] + (state.inferred[id] ? ' · 推测':'');
    $(id + '-slider-value').textContent = label;
    slider.setAttribute('aria-valuetext',label);
  });
}
function priceBasis(plan) { return plan.price_basis === 'serving' || plan.people == null ? '每份参考':'人均'; }
function conditionText(needs) {
  return [needs.people == null ? '人数未说明':needs.people + ' 人',
    needs.budget == null ? '预算未设置':'人均预算 ≤ ¥' + needs.budget,
    needs.minutes == null ? '时间未设置':'可用时间 ' + needs.minutes + ' 分钟'].join(' · ');
}
function changed(eventOrField) {
  const id = typeof eventOrField === 'string' ? eventOrField:eventOrField && eventOrField.target && eventOrField.target.id;
  const field = {'portion-goal':'portion','pace-goal':'pace','avoid-foods':'avoid_foods'}[id] || id;
  if (field) delete state.inferred[field];
  state.stale = true; state.seen = [];
  syncControls(); renderPlans();
}
function checkNeeds() {
  if ($('needs-form').checkValidity()) return true;
  editConditions();
  $('needs-form').reportValidity();
  return false;
}
function editConditions() {
  $('needs-settings').open = true;
  if (matchMedia('(max-width:999px)').matches) openPanel('left');
  $('budget').focus();
}
function closePanels() {
  $('left-sidebar').classList.remove('panel-open');
  $('right-sidebar').classList.remove('panel-open');
  $('side-backdrop').hidden = true;
  const target = state.panelTrigger;
  state.panelTrigger = null;
  if (target && document.contains(target)) target.focus();
}
function openPanel(side) {
  closePanels();
  state.panelTrigger = document.activeElement;
  const panel = $(side + '-sidebar');
  panel.classList.add('panel-open');
  $('side-backdrop').hidden = false;
  panel.querySelector('.mobile-close').focus();
}
function setBusy(busy) {
  state.busy = busy;
  $('send-button').disabled = busy;
  $('new-chat').disabled = busy;
  $('chat-mode').disabled = busy;
  $('model-button').disabled = busy;
  $('needs-fields').disabled = busy;
  $('feedback-meal-select').disabled = busy;
  $('edit-conditions').disabled = busy;
  ['upload-menu','current-menu','exit-menu','menu-import-submit','menu-edit-save','menu-delete','menu-replace','menu-files','menu-text','menu-requirements','menu-wait','menu-dine'].forEach((id) => { $(id).disabled = busy; });
  document.querySelectorAll('[data-suggestion]').forEach((b) => { b.disabled = busy; });
  $('shuffle-button').disabled = busy || state.stale || !state.plans.length;
  $('send-button').setAttribute('aria-label',busy ? '正在等待回复':'发送消息');
}

function renderPlans() {
  const count = state.plans.length;
  $('result-mode').textContent = state.stale ? '条件已变更 · 请重新推荐' : (state.preview ? '灵感预览' : state.recommendMode === 'ai' ? 'AI 推荐置顶' : '规则推荐置顶') + ' · ' + count + ' 个可选';
  $('shuffle-button').disabled = state.busy || state.stale || !count;
  const query = $('option-search').value.trim().toLowerCase();
  const options = state.plans.map((plan,index) => ({plan:plan,index:index})).filter(({plan:p}) => ((p.full_name || '') + ' ' + p.venue_name + ' ' + p.items.map((item) => item.name).join(' ')).toLowerCase().includes(query));
  if (!options.length) {
    $('recommendation-grid').innerHTML = '<div class="empty-state"><h3>' + (count ? '没有匹配的餐品' : '暂时没有合适的方案') + '</h3><p>' + esc(count ? '换个关键词试试。' : (state.conflicts || []).join('、') || '说说你想怎么吃。') + '</p>' + (count ? '' : '<button class="text-button" data-edit-conditions>调整用餐条件</button>') + '</div>';
    return;
  }
  $('recommendation-grid').innerHTML = options.map(({plan:p,index}) => {
    const chosen = state.recommendationMealId && p.id === state.primaryId;
    const disabled = state.stale || state.busy ? ' disabled' : '';
    const badge = chosen ? '<span class="chosen-label">本次已记录</span>' : p.needs_price_check ? '<span class="card-badge">待核价</span>' : p.recommended ? '<span class="card-badge">推荐</span>' : '';
    const dishes = p.items.map((i) => esc(i.name) + '<span class="card-quantity"> ×' + i.quantity + '</span>').join('<span class="card-separator">＋</span>');
    const time = p.rated ? '价格区间' : p.minutes + ' 分钟（估计）';
    const facts = p.rated ? ratingText(p) + '。总价区间 ' + priceText(p,false) + '，具体耗时和配料未提供。' : '共 ' + priceText(p,false) + ' · 步行 ' + p.walk + ' + 等待 ' + p.wait + ' + 用餐 ' + p.dine + ' 分钟';
    return '<article class="recommendation-card" data-plan-id="' + esc(p.id) + '" data-recommended="' + Boolean(p.recommended) + '"><div class="card-heading"><h3 class="card-dishes">' + dishes + '</h3>' + badge + '</div><p class="card-venue">' + esc(p.venue_name) + '</p><div class="card-metrics">' + metricFields(p).map((item) => '<div class="card-metric" data-metric="' + item.key + '"><span class="metric-label">' + item.label + '</span><strong class="metric-value">' + esc(item.value) + '</strong></div>').join('') + '</div><div class="card-footer"><div class="card-facts"><strong>' + priceText(p,true) + '<small>' + (p.people == null ? '/份参考':'/人') + '</small></strong><span>' + time + '</span></div><div class="card-actions"><button class="card-action" data-detail="' + index + '"' + disabled + '>' + (p.rated ? '详情':'菜单') + '</button>' + (!chosen ? '<button class="card-action choose-option" data-choose="' + index + '"' + disabled + '>' + (p.needs_price_check ? '核对价格':'换成这份') + '</button>' : '') + '</div></div><details class="card-explanation"><summary>推荐理由与详情 ⌄</summary>' + (p.recommended ? '<p>' + esc(p.strategy) + '</p>' : '') + '<p>' + esc(p.variety_note || '') + '</p><p>' + esc(p.reason) + '</p><p>' + esc(p.tradeoff) + '</p><p>' + esc(facts) + '</p></details></article>';
  }).join('');
}

function persistMessages() {
  try { sessionStorage.setItem('eat-chat-v2',JSON.stringify(state.messages.slice(-60).map((m) => ({role:m.role,text:m.text,mode:m.mode,model:m.model})))); } catch (_) {}
}
function renderMessages() {
  const hasMessages = state.messages.length || state.pending;
  $('welcome').hidden = Boolean(hasMessages);
  $('chat-scroll').classList.toggle('empty',!hasMessages);
  $('messages').innerHTML = state.messages.map((m) => {
    if (m.role === 'user') return '<article class="message user"><div class="message-content">' + esc(m.text) + '</div></article>';
    let actions = '';
    if (m.plan) actions += '<button type="button" data-chat-plan="' + m.id + '">查看这份菜单 ' + icon('arrow') + '</button>';
    if (m.menuId) actions += '<button type="button" data-edit-imported-menu="' + esc(m.menuId) + '">查看 / 修改识别菜单 ' + icon('arrow') + '</button>';
    if (m.restoreMealId) actions += '<button type="button" data-restore-meal="' + esc(m.restoreMealId) + '">恢复这条记录</button>';
    if (m.edit) actions += '<button type="button" data-edit-conditions>调整用餐条件</button>';
    const label = (m.mode !== 'chat' ? modeNames[m.mode]:'') + (m.model ? (m.mode !== 'chat' ? ' · ':'') + m.model:'');
    return '<article class="message assistant"><span class="message-avatar">' + icon('bowl') + '</span><div>' + (label ? '<div class="message-mode">' + esc(label) + '</div>':'') + '<div class="message-content">' + esc(assistantText(m.text)) + '</div>' + (actions ? '<div class="message-actions">' + actions + '</div>':'') + '</div></article>';
  }).join('') + (state.pending ? '<article class="message assistant pending"><span class="message-avatar">' + icon('bowl') + '</span><div class="typing-dots" role="status" aria-label="正在思考"><span></span><span></span><span></span></div></article>':'');
  $('chat-scroll').scrollTop = $('chat-scroll').scrollHeight;
}
function appendMessage(role,text,extra) {
  state.messages.push(Object.assign({id:uid(),role:role,text:text,mode:state.mode},extra || {}));
  renderMessages(); persistMessages();
}
function conversationContext() {
  return state.messages.slice(-12).map((m) => ({role:m.role,content:m.text.slice(0,5000)}));
}
function syncMode() {
  state.mode = $('chat-mode').value;
  $('feedback-context').hidden = state.mode !== 'feedback';
  $('chat-input').placeholder = {chat:'聊聊这顿想怎么吃，右侧推荐会跟着更新…',meal:'比如：人均20元，吃少一些，慢慢吃…',feedback:'说说这顿的感受，或告诉我下次想怎么吃…'}[state.mode];
  $('mode-icon').querySelector('use').setAttribute('href','#i-' + {chat:'spark',meal:'bowl',feedback:'heart'}[state.mode]);
}
function syncModel() {
  $('model-label').textContent = state.model;
  $('model-button').title = '选择 LLM · 当前：' + state.model;
  $('model-button').setAttribute('aria-label','选择 LLM 模型，当前 ' + state.model);
}
function renderModels() {
  const query = $('model-search').value.trim().toLowerCase();
  const models = Array.from(new Set([state.model].concat(state.models))).filter((name) => name.toLowerCase().includes(query));
  $('model-list').innerHTML = models.length ? models.map((name) => '<button type="button" class="model-option' + (name === state.model ? ' selected':'') + '" data-model="' + esc(name) + '" aria-pressed="' + (name === state.model) + '"><span>' + esc(name) + (name === state.defaultModel ? '<small>服务默认</small>':'') + '</span>' + (name === state.model ? icon('check'):'') + '</button>').join(''):'<p class="model-empty">没有找到匹配的模型</p>';
}
async function openModels(refresh) {
  if (state.busy) return;
  if (!$('model-dialog').open) {
    $('model-search').value = '';
    $('model-dialog').showModal();
  }
  renderModels();
  if (state.models.length && !refresh) return;
  $('refresh-models').disabled = true;
  $('model-list-status').textContent = '正在读取账户目录…';
  $('model-notice').textContent = '';
  try {
    const result = await api('/api/models' + (refresh ? '?refresh=1':''));
    state.models = result.models;
    state.defaultModel = result.default_model;
    $('model-list-status').textContent = (result.source === 'platform' ? '账户模型目录':'常用文本模型') + ' · ' + result.models.length + ' 个';
    $('model-notice').textContent = result.notice || '模型目录由并行科技提供，实际调用以账户权限为准。';
    renderModels();
  } catch (err) {
    $('model-list-status').textContent = '目录暂时不可用';
    $('model-notice').textContent = err.message;
  } finally { $('refresh-models').disabled = false; }
}
function feedbackContext() {
  const current = $('feedback-meal-select').value;
  $('feedback-meal-select').innerHTML = state.data.history.length ? state.data.history.map((m) => '<option value="' + esc(m.id) + '">' + esc(m.plan.venue_name) + ' · ' + esc(dateText(m.created)) + '</option>').join(''):'<option value="">暂无记录，可先聊聊长期偏好</option>';
  if (state.data.history.some((m) => m.id === current)) $('feedback-meal-select').value = current;
}

function syncPersonalData() {
  feedbackContext();
}

function updateData(data) {
  const changedMemory = JSON.stringify(state.data.memories) !== JSON.stringify(data.memories);
  state.data = data;
  syncPersonalData();
  if ($('drawer').open) renderDrawer();
  if (changedMemory && state.recommendationNeeds) {
    state.stale = true; state.seen = [];
    renderPlans();
    note('偏好已更新，正在调整推荐。');
    if (!state.busy) refreshSidebar();
  }
}

function activeMealRecord() {
  if (!state.activeMeal || Date.now() - state.activeMeal.started > 3 * 60 * 60 * 1000) return null;
  return state.data.history.find((m) => m.id === state.activeMeal.mealId && !m.feedback && !m.completed) || null;
}

async function refreshSidebar() {
  const version = ++sidebarRefreshVersion;
  const needs = readNeeds();
  try {
    const result = await api('/api/recommend',{needs:needs,mode:'basic'});
    if (version !== sidebarRefreshVersion || state.busy || JSON.stringify(needs) !== JSON.stringify(readNeeds())) return;
    applyChatRecommendations({recommendations:result,needs:needs});
  } catch (err) {
    if (version === sidebarRefreshVersion && !state.busy) note('推荐暂未更新：' + err.message);
  }
}

function applyChatRecommendations(response) {
  sidebarRefreshVersion += 1;
  const result = response.recommendations;
  if (!result) return;
  const needs = response.needs || readNeeds();
  applyNeeds(needs);
  state.plans = result.options || result.plans;
  state.conflicts = result.conflicts;
  state.recommendMode = result.mode; state.preview = false; state.stale = false;
  state.recommendationNeeds = JSON.parse(JSON.stringify(needs));
  state.seen = result.plans.map((p) => p.id);
  const active = activeMealRecord();
  state.recommendationMealId = active ? active.id : null;
  state.primaryId = active ? active.plan.id : null;
  $('option-search').value = '';
  note(response.recommendation_notice || result.notice || '');
  renderPlans();
}

async function requestPlans(mode,needs,exclude,model,session,record,request) {
  const shouldRecord = record !== false;
  const result = await api('/api/recommend',{needs:needs,mode:mode,exclude:exclude || [],model:model,record:shouldRecord,meal_session:session,text:request ? request.text:'',messages:request ? request.context:[]});
  if (result.needs) { needs = result.needs; applyNeeds(needs); }
  if (!shouldRecord) {
    applyChatRecommendations({recommendations:result,needs:needs});
    state.seen = Array.from(new Set((exclude || []).concat(result.plans.map((p) => p.id)))).slice(-180);
    return result;
  }
  state.plans = result.options || result.plans; state.conflicts = result.conflicts;
  state.recommendMode = mode; state.preview = false; state.stale = false;
  state.primaryId = result.primary_id; state.recommendationMealId = result.meal_id || null;
  state.recommendationNeeds = JSON.parse(JSON.stringify(needs));
  state.seen = Array.from(new Set((exclude || []).concat(result.plans.map((p) => p.id)))).slice(-180);
  if (result.state) {
    updateData(result.state);
    $('feedback-meal-select').value = result.meal_id;
    if (state.activeMeal && state.activeMeal.session === session) { state.activeMeal.mealId = result.meal_id; persistMeal(); }
  }
  note([result.notice,result.exhausted ? '本轮推荐已看过，右侧仍保留全部可选方案。' : ''].filter(Boolean).join(' '));
  renderPlans();
  return result;
}


function mealReply(result,needs) {
  if (!result.plans.length) {
    appendMessage('assistant','按目前的预算、时间和忌口，暂时没有找到合适的方案。\n\n' + (result.conflicts || []).join('、') + '。你可以调整条件后再试。',{mode:'meal',edit:true});
    return;
  }
  const p = result.plans.find((plan) => plan.id === result.primary_id) || result.plans[0];
  const reply = (result.mode === 'basic' ? '按当前条件，':'这餐可以吃') + '「' + p.venue_name + '」的' + p.items.map((i) => i.name).join('、') + '。\n\n' + p.reason + ' ' + (p.variety_note || '') + '\n\n' + priceBasis(p) + ' ' + priceText(p,true) + (p.rated ? '，吃完速度' + metricValue(p,'speed') + '；未提供实际耗时。' : '，预计 ' + p.minutes + ' 分钟吃完。') + '\n\n已默认记作这一餐；想改的话，直接告诉我，或从右侧全部可选方案中换一份。';
  appendMessage('assistant',reply,{mode:'meal',model:result.model,plan:p,mealId:result.meal_id,needs:JSON.parse(JSON.stringify(needs))});
}

async function execute(request,engine) {
  if (state.busy) return;
  sidebarRefreshVersion += 1;
  clearError(); setBusy(true); state.pending = true; renderMessages(); renderPlans();
  try {
    if (request.mode === 'meal') {
      let needs = readNeeds();
      if (request.text && !request.shuffle) {
        const parsed = await api('/api/parse',{text:request.text,needs:needs,mode:engine,model:request.model});
        needs = parsed.needs; applyNeeds(needs); state.seen = []; state.stale = true; renderPlans();
      }
      const result = await requestPlans(engine,needs,request.shuffle ? state.seen:[],request.model,request.mealSession,undefined,request);
      state.pending = false;
      mealReply(result,result.needs || needs);
    } else if (request.shuffle) {
      const result = await requestPlans(engine,readNeeds(),state.seen,request.model,null,false,request);
      state.pending = false;
      appendMessage('assistant',result.plans.length ? '右侧推荐已换一批，可以继续聊聊或直接挑选。' : '当前条件下没有合适的餐品，可以调整预算、时间或忌口后再试。',{mode:request.mode});
    } else {
      const active = activeMealRecord();
      const result = await api('/api/chat',{text:request.text,messages:request.context,mode:request.mode,meal_id:request.mealId || null,model:request.model,needs:request.needs || readNeeds(),active_meal_id:active ? active.id:null});
      state.pending = false;
      if (result.state) updateData(result.state);
      if (result.saved_feedback) finishMeal(result.meal_id);
      applyChatRecommendations(result);
      const saved = [result.saved_feedback ? '这次反馈已记下。' : '',result.saved_memory ? '长期偏好已更新。' : ''].filter(Boolean).join(' ');
      appendMessage('assistant',result.reply + (saved ? '\n\n' + saved : ''),{mode:request.mode,model:result.model});
    }
    if ($('chat-input').value.trim() === request.text) $('chat-input').value = '';
  } catch (err) {
    if (err.result && err.result.recommendations) {
      applyChatRecommendations(err.result);
      note('AI 对话未完成，右侧已按基础规则更新；输入已保留，可以重试或切换模型。');
    }
    if (!$('chat-input').value && !request.shuffle) $('chat-input').value = request.text;
    if (err.code === 'meal_closed') { state.activeMeal = null; persistMeal(); request.mealSession = mealSession(); }
    state.failedRequest = request;
    showError(err.message,() => execute(request,engine),request.mode === 'meal');
  } finally {
    state.pending = false; setBusy(false); renderMessages(); renderPlans();
  }
}
async function sendMessage() {
  if (state.busy || !state.catalog) return;
  const text = $('chat-input').value.trim();
  if (!text && state.mode !== 'meal') { $('chat-input').focus(); return; }
  if (state.mode === 'meal' && !checkNeeds()) return;
  const request = {text:text,mode:state.mode,model:state.model,needs:readNeeds(),context:conversationContext(),mealId:$('feedback-meal-select').value,mealSession:state.mode === 'meal' ? mealSession():null};
  appendMessage('user',text || '按当前条件，帮我选一餐。');
  $('chat-input').value = '';
  closePanels();
  await execute(request,'ai');
}
async function shuffle() {
  if (state.busy || !checkNeeds() || state.stale) return;
  const request = {text:'',mode:state.mode,model:state.model,shuffle:true,context:conversationContext(),mealSession:state.mode === 'meal' ? mealSession():null};
  appendMessage('user','再帮我换一批方案。',{mode:state.mode});
  await execute(request,state.recommendMode);
}
async function startNewChat() {
  if (state.busy || !state.catalog) return;
  sidebarRefreshVersion += 1;
  setBusy(true); renderPlans();
  try {
    const response = await api('/api/new-chat',{active_meal_id:state.activeMeal ? state.activeMeal.mealId:null});
    state.messages = []; state.pending = false;
    state.activeMeal = null; persistMeal();
    persistMessages();
    state.failedRequest = null;
    $('chat-input').value = '';
    updateData(response.state);
    applyChatRecommendations(response);
    state.preview = true; state.seen = [];
    $('welcome-description').textContent = response.completed_meal_id ? '上一餐已完成并保存在记录中，现在开始推荐新一餐。' : '开始新一餐，用餐条件已清空，历史记录与长期偏好保留。';
    clearError(); renderMessages();
    closePanels(); $('chat-input').focus();
    toast(response.completed_meal_id ? '上一餐已完成，现在开始新一餐；历史记录与长期偏好已保留。' : '已开始新一餐，聊天和条件已清空，历史记录与长期偏好已保留。');
  } catch (err) {
    toast('新对话未能开始，原有内容已保留：' + err.message);
  } finally { setBusy(false); renderPlans(); }
}
async function boot() {
  try {
    const results = await Promise.all([api('/api/catalog'),api('/api/state'),api('/api/status')]);
    state.catalog = results[0];
    state.defaultModel = results[2].model;
    state.model = state.defaultModel;
    try {
      const savedModel = localStorage.getItem('eat-llm-model');
      if (savedModel && /^[A-Za-z0-9_./:@+-]{1,160}$/.test(savedModel)) state.model = savedModel;
    } catch (_) {}
    syncModel();
    renderVenueOptions();
    state.visionModel = results[2].vision_model || 'GLM-4V-Plus-0111';
    $('purpose').innerHTML = '<option value="">未说明</option>' + state.catalog.purposes.map((p) => '<option>' + esc(p) + '</option>').join('');
    $('taste-options').innerHTML = state.catalog.tastes.map((t) => '<button class="taste-chip" type="button" data-taste="' + esc(t) + '" aria-pressed="false">' + esc(t) + '</button>').join('');
    $('ai-status').textContent = results[2].configured ? 'AI 已配置':'AI 待配置';
    updateData(results[1]);
    try {
      const savedMenu = localStorage.getItem('eat-imported-menu');
      if (state.catalog.venues.some((v) => v.id === savedMenu && v.imported)) {
        state.scene = 'menu'; $('venue-select').value = savedMenu;
      }
    } catch (_) {}
    syncControls(); syncMode();
    try {
      const saved = JSON.parse(sessionStorage.getItem('eat-chat-v2') || '[]');
      if (Array.isArray(saved)) state.messages = saved.filter((m) => m && ['user','assistant'].includes(m.role) && typeof m.text === 'string' && m.text.length <= 5000 && modeNames[m.mode]).slice(-60).map((m) => ({id:uid(),role:m.role,text:m.text,mode:m.mode,model:typeof m.model === 'string' ? m.model:undefined}));
    } catch (_) {}
    renderMessages();
    const preview = await api('/api/recommend',{needs:readNeeds(),mode:'basic'});
    state.plans = preview.options || preview.plans; state.conflicts = preview.conflicts;
    try {
      const active = JSON.parse(sessionStorage.getItem('eat-active-meal') || 'null');
      if (active && typeof active.session === 'string' && typeof active.started === 'number' && Date.now() - active.started < 3 * 60 * 60 * 1000) {
        const existing = state.data.history.find((meal) => meal.id === active.mealId && state.catalog.venues.some((v) => v.id === meal.plan.venue_id));
        if (!active.mealId || (existing && !existing.feedback && !existing.completed)) state.activeMeal = active;
        else { state.activeMeal = null; persistMeal(); }
        if (existing && !existing.feedback && !existing.completed) {
          applyNeeds(existing.needs);
          const restored = await api('/api/recommend',{needs:existing.needs,mode:'basic'});
          state.plans = [Object.assign({},existing.plan,{recommended:true,strategy:'本次选择',reason:'已记录的这一餐，可以直接修改。',tradeoff:'可随时从其他可选方案中替换。'})].concat((restored.options || restored.plans).filter((plan) => plan.id !== existing.plan.id));
          state.primaryId = existing.plan.id; state.recommendationMealId = existing.id;
          state.preview = false; state.recommendMode = 'basic';
        }
      }
    } catch (_) {}
    state.recommendationNeeds = readNeeds(); state.seen = preview.plans.map((p) => p.id);
    renderPlans();
  } catch (err) { showError(err.message,boot,false); }
}

function dateText(date) { return new Date(date).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',month:'long',day:'numeric',hour:'2-digit',minute:'2-digit'}); }
function openDrawer(tab) {
  state.drawerTab = tab;
  renderDrawer();
  if (!$('drawer').open) $('drawer').showModal();
}

function feedbackText(feedback) {
  const fields = [['taste','口味'],['portion','分量'],['value','性价比']];
  const parts = fields.filter(([key]) => feedback[key] !== undefined).map(([key,label]) => label + ' ' + feedbackValue(key,feedback[key]));
  if (feedback.wait !== undefined) parts.push('实际等候 ' + feedback.wait + ' 分钟');
  return parts.join(' · ');
}
function renderDrawer() {
  const history = state.drawerTab === 'history';
  $('drawer-title').textContent = history ? '用餐记录 · ' + state.data.meals : '偏好记忆 · ' + state.data.memories.length;
  document.querySelectorAll('[data-drawer-tab]').forEach((b) => b.classList.toggle('selected',b.dataset.drawerTab === state.drawerTab));
  if (history) {
    $('drawer-content').innerHTML = state.data.history.length ? state.data.history.map((m) => '<article class="history-item"><p class="history-date">' + esc(dateText(m.created)) + ' · ' + (m.plan.people == null ? '人数未说明':m.plan.people + ' 人用餐') + (m.assumed ? ' · 默认记录' : '') + (m.completed ? ' · 已完成' : '') + '</p><h3>' + esc(m.plan.venue_name) + '</h3><p class="history-description">' + m.plan.items.map((i) => esc(i.name) + ' ×' + i.quantity).join('、') + '</p><p class="history-description">共 ' + priceText(m.plan,false) + ' · ' + priceBasis(m.plan) + ' ' + priceText(m.plan,true) + '</p>' + (m.feedback ? '<div class="history-feedback">' + esc(feedbackText(m.feedback)) + (m.feedback.note ? '<br>' + esc(m.feedback.note) : '') + '</div>' : '') + '<div class="history-actions"><button class="text-button" data-feedback="' + esc(m.id) + '">' + (m.feedback ? '修改反馈' : '补充感受（可选）') + '</button>' + (m.feedback ? '<button class="text-button muted-button" data-learning="' + esc(m.id) + '" data-enabled="' + !m.learn_enabled + '">' + (m.learn_enabled ? '撤销本次学习' : '恢复本次学习') + '</button>' : '') + '<button class="text-button muted-button" data-cancel-meal="' + esc(m.id) + '">没吃这餐</button></div></article>').join('') : '<div class="drawer-empty">还没有用餐记录。</div>';
  } else {
    $('drawer-content').innerHTML = '<div class="memory-education">明确的口味反馈会直接更新软偏好，本次需求始终优先。<br>反馈和记忆都可修改、删除或撤销。</div>' + (state.data.memories.length ? state.data.memories.map((m) => '<article class="memory-item"><div class="memory-item-top"><h3>' + esc(m.label) + '</h3><span class="subtle">' + (m.manual ? '明确偏好' : '来自反馈') + '</span></div><details class="memory-source"><summary>' + m.sources.length + ' 次反馈来源</summary><ul>' + m.sources.map((source) => '<li>' + esc(dateText(source.created)) + ' · ' + esc(source.venue) + ' · ' + (source.direction > 0 ? '正向口味反馈' : '负向口味反馈') + '</li>').join('') + '</ul></details><div class="memory-actions"><button class="text-button" data-memory-action="flip" data-tag="' + esc(m.tag) + '">改为' + (m.direction > 0 ? '较少推荐' : '喜欢') + '</button><button class="text-button muted-button" data-memory-action="delete" data-tag="' + esc(m.tag) + '">删除记忆</button>' + (m.manual ? '<button class="text-button" data-memory-action="reset" data-tag="' + esc(m.tag) + '">恢复自动</button>' : '') + '</div></article>').join('') : '<div class="drawer-empty">还没有长期偏好。</div>') + '<form id="preference-form" class="preference-form"><h3>直接设置偏好</h3><div class="preference-row"><select id="preference-tag" aria-label="偏好口味或品类">' + state.catalog.memory_tags.map((t) => '<option>' + esc(t) + '</option>').join('') + '</select><select id="preference-direction" aria-label="偏好方向"><option value="1">喜欢</option><option value="-1">较少推荐</option></select><button type="submit" class="outline-button">保存</button></div><button type="button" id="restore-auto" class="text-button">为所选标签恢复自动学习</button></form>';
  }
  $('drawer-content').insertAdjacentHTML('beforeend','<button id="clear-data" class="clear-data-button" type="button">清空本地记录与偏好</button>');
}
function openDetail(index, explicitPlan, explicitNeeds, explicitMealId) {
  const plan = explicitPlan || state.plans[index];
  if (!plan || (!explicitPlan && state.stale)) return;
  const venue = state.catalog.venues.find((v) => v.id === plan.venue_id);
  if (!venue) { toast('这份菜单已删除，原用餐记录仍保留。'); return; }
  state.detail = {venue:venue,needs:JSON.parse(JSON.stringify(explicitNeeds || state.recommendationNeeds)),items:Object.fromEntries(plan.items.map((i) => [i.dish_id,i.quantity])),request_id:uid(),quote:null,mealId:explicitPlan ? explicitMealId : state.recommendationMealId};
  if (venue.rated) {
    const dish = venue.dishes[0];
    const item = plan.items[0];
    $('detail-title').textContent = venue.full_name;
    $('detail-content').innerHTML = '<div class="detail-banner"><div><h3>你的常吃食堂统计</h3><p>' + esc(ratingText(plan)) + '</p><p>速度表示吃完快慢；饭量和速度按本次需求取舍。</p></div></div><p class="detail-constraint">' + esc(conditionText(state.detail.needs)) + '。' + priceBasis(plan) + '价格 ' + priceText(plan,true) + '；具体路程、耗时、口味类型和配料未提供。</p><div class="menu-row"><div><p class="menu-name">' + esc(dish.name) + '</p><p class="menu-meta">餐食份数，不代表重量</p></div><div class="menu-quantity"><button type="button" data-quantity="-1" data-dish="' + esc(dish.id) + '" aria-label="减少份数">−</button><output id="quantity-' + esc(dish.id) + '">' + item.quantity + '</output><button type="button" data-quantity="1" data-dish="' + esc(dish.id) + '" aria-label="增加份数">＋</button></div></div><div class="field"><label for="rated-actual-price">实际单价 / 元（可选）</label><input id="rated-actual-price" type="number" min="0" max="5000" step="0.01" placeholder="区间不确定是否超预算时，请填写实际单价" value="' + (item.price == null ? '' : item.price/100) + '"></div><p class="field-hint">未填写时保留价格区间，不用区间中点当实际价格。此处填写的单价只用于本餐。</p><p id="quote-error" class="inline-error" role="alert"></p><div class="quote-footer"><div id="quote-total" class="quote-total">正在核验预算…</div><button id="confirm-meal" type="button" class="primary-button" disabled>保存本餐选择 ' + icon('check') + '</button></div>';
    $('rated-actual-price').addEventListener('input',quote);
    $('detail-dialog').showModal(); quote(); return;
  }
  $('detail-title').textContent = '把这一餐，搭配成你喜欢的样子';
  $('detail-content').innerHTML = '<div class="detail-banner">' + art(venue.art) + '<div><h3>' + esc(venue.name) + '</h3><p>' + esc(venue.description) + '</p><p>菜单明细 · 请核实菜品与份量</p></div></div><p class="detail-constraint">本次条件：' + esc(conditionText(state.detail.needs)) + '。' + (state.detail.needs.avoid.length ? '忌口：' + esc(state.detail.needs.avoid.join('、')) : '') + '</p><div class="menu-list">' + venue.dishes.map((d) => '<div class="menu-row"><div><p class="menu-name">' + esc(d.name) + '</p><p class="menu-meta">' + esc(d.taste) + ' · ' + esc(d.portion) + '<br>食材：' + esc(d.ingredients.join('、')) + '</p></div><span class="menu-price">¥' + money(d.price) + '</span><div class="menu-quantity"><button type="button" data-quantity="-1" data-dish="' + esc(d.id) + '" aria-label="减少' + esc(d.name) + '">−</button><output id="quantity-' + esc(d.id) + '">' + (state.detail.items[d.id] || 0) + '</output><button type="button" data-quantity="1" data-dish="' + esc(d.id) + '" aria-label="增加' + esc(d.name) + '">＋</button></div></div>').join('') + '</div><p id="quote-error" class="inline-error" role="alert"></p><div class="quote-footer"><div id="quote-total" class="quote-total" aria-live="polite">正在计算金额…</div><button id="confirm-meal" type="button" class="primary-button" disabled>保存本餐修改 ' + icon('check') + '</button></div>';
  if (venue.imported) {
    $('detail-content').querySelector('.detail-banner p:last-child').textContent = '来自你发送的菜单 · 共享菜份量请核实 · 时间为手动估计';
    $('detail-content').querySelector('.detail-constraint').insertAdjacentHTML('afterend','<button type="button" class="text-button" data-edit-imported-menu="' + esc(venue.id) + '">修正菜名、价格或配料</button>');
    venue.dishes.forEach((dish) => {
      const row = $('quantity-' + dish.id).closest('.menu-row');
      row.querySelector('.menu-meta').textContent = dish.taste + ' · ' + dish.portion + ' · ' + (dish.ingredients_known ? '配料已核实' : '配料未核实');
      if (dish.price === null) {
        row.querySelector('.menu-price').textContent = '未标价';
        row.querySelectorAll('[data-quantity]').forEach((button) => { button.disabled = true; });
      }
    });
  }
  $('detail-dialog').showModal();
  quote();
}
function detailItems() { return Object.entries(state.detail.items).filter((entry) => entry[1] > 0).map((entry) => ({dish_id:entry[0],quantity:entry[1]})); }
function detailActualPrices() {
  if (!state.detail.venue.rated) return undefined;
  const input = $('rated-actual-price');
  return input && input.value !== '' ? {[state.detail.venue.dishes[0].id]:Number(input.value)} : {};
}
async function quote() {
  const version = ++state.quoteVersion;
  const detail = state.detail;
  $('confirm-meal').disabled = true;
  if (!detailItems().length) {
    $('quote-total').textContent = '共 ¥0 · 请至少选择一份餐食';
    $('quote-error').textContent = '';
    return;
  }
  try {
    const result = await api('/api/quote',{venue_id:detail.venue.id,items:detailItems(),needs:detail.needs,actual_prices:detailActualPrices()});
    if (version !== state.quoteVersion || state.detail !== detail) return;
    detail.quote = result.plan;
    $('quote-total').innerHTML = '总计 <strong>' + priceText(result.plan,false) + '</strong> · 人均 ' + priceText(result.plan,true) + (result.plan.rated ? '<small>共 ' + result.plan.servings + ' 份 · 具体耗时未提供</small>' : '<small>共 ' + result.plan.servings + ' 份 · 步行 ' + result.plan.walk + ' + 等待 ' + result.plan.wait + ' + 用餐 ' + result.plan.dine + ' = ' + result.plan.minutes + ' 分钟</small>');
    $('quote-error').textContent = result.violations.join('、');
    $('confirm-meal').disabled = result.violations.length > 0;
  } catch (err) {
    if (version === state.quoteVersion && state.detail === detail) {
      $('quote-error').textContent = err.message;
      $('quote-total').textContent = '金额暂时无法计算，请调整数量后重试';
    }
  }
}

async function chooseOption(index) {
  const plan = state.plans[index];
  if (!plan || state.busy || state.stale) return;
  if (plan.needs_price_check) { openDetail(index); return; }
  setBusy(true); renderPlans();
  try {
    const session = mealSession();
    const result = await api('/api/meals',{venue_id:plan.venue_id,items:plan.items,needs:state.recommendationNeeds,meal_id:state.recommendationMealId,meal_session:session,request_id:uid(),actual_prices:actualPrices(plan)});
    updateData(result.state);
    state.primaryId = plan.id; state.recommendationMealId = result.id; state.stale = false;
    state.activeMeal.mealId = result.id; persistMeal();
    $('feedback-meal-select').value = result.id;
    appendMessage('assistant','本餐已选择「' + plan.venue_name + '」的' + plan.items.map((item) => item.name).join('、') + '，用餐记录已保存。',{mode:'meal'});
  } catch (err) { toast(err.message); }
  finally { setBusy(false); renderPlans(); }
}
async function confirmMeal() {
  const button = $('confirm-meal');
  if (!state.detail || button.disabled) return;
  button.disabled = true;
  const d = state.detail;
  $('detail-content').querySelectorAll('[data-quantity]').forEach((b) => { b.disabled = true; });
  try {
    const result = await api('/api/meals',{venue_id:d.venue.id,items:detailItems(),needs:d.needs,meal_id:d.mealId,meal_session:d.mealId ? undefined:mealSession(),request_id:d.request_id,actual_prices:detailActualPrices()});
    updateData(result.state);
    $('feedback-meal-select').value = result.id;
    if (!d.mealId || d.mealId === state.recommendationMealId) {
      state.primaryId = d.quote.id; state.recommendationMealId = result.id; state.stale = false;
      state.plans = [Object.assign({},d.quote,{recommended:true,strategy:'本次选择',reason:'已按你的修改记录。',tradeoff:'可继续更改。'})].concat(state.plans.filter((p) => p.id !== d.quote.id));
      if (state.activeMeal) { state.activeMeal.mealId = result.id; persistMeal(); }
      renderPlans();
    }
    $('detail-dialog').close();
    appendMessage('assistant','本餐菜单已保存。',{mode:'meal'});
  } catch (err) {
    $('quote-error').textContent = err.message;
    button.disabled = false;
  } finally {
    $('detail-content').querySelectorAll('[data-quantity]').forEach((b) => { b.disabled = d.venue.imported && d.venue.dishes.some((dish) => dish.id === b.dataset.dish && dish.price === null); });
  }
}
function openFeedback(id) {
  const meal = state.data.history.find((m) => m.id === id);
  if (!meal) return;
  state.feedbackId = id;
  $('feedback-error').textContent = '';
  $('feedback-meal').textContent = meal.plan.venue_name + ' · ' + dateText(meal.created);
  $('rating-fields').innerHTML = [['taste','口味合不合心意？'],['portion','分量合不合适？'],['value','觉得划不划算？']].map(([field,label]) => '<fieldset class="rating-row"><legend>' + label + '</legend><div class="rating-options">' + state.catalog.feedback_terms[field].map((value) => '<label><input type="radio" name="' + field + '" value="' + esc(value) + '"' + (value === feedbackValue(field,meal.feedback && meal.feedback[field]) ? ' checked' : '') + '><span>' + esc(value) + '</span></label>').join('') + '</div></fieldset>').join('');
  $('actual-wait').value = meal.feedback && meal.feedback.wait !== undefined ? meal.feedback.wait : '';
  $('feedback-note').value = meal.feedback ? meal.feedback.note || '' : '';
  $('feedback-dialog').showModal();
}
async function saveFeedback(event) {
  event.preventDefault();
  const button = $('save-feedback');
  if (button.disabled) return;
  button.disabled = true;
  $('feedback-error').textContent = '';
  const form = new FormData($('feedback-form'));
  const feedback = {};
  ['taste','portion','value'].forEach((key) => { if (form.get(key) !== null) feedback[key] = form.get(key); });
  if ($('actual-wait').value.trim()) feedback.wait = Number($('actual-wait').value);
  if ($('feedback-note').value.trim()) feedback.note = $('feedback-note').value.trim();
  const before = state.data.memories.length;
  try {
    updateData(await api('/api/feedback',{meal_id:state.feedbackId,feedback:feedback}));
    $('feedback-dialog').close();
    finishMeal(state.feedbackId);
    appendMessage('assistant','这次餐后反馈已保存。你可以在记忆与反馈中查看、修改或撤销本次学习。',{mode:'feedback'});
    toast(state.data.memories.length > before ? '反馈已保存，学到了新的偏好。下次推荐会参考它。' : '谢谢你的反馈，每一次感受都会被认真记下。');
  } catch (err) { $('feedback-error').textContent = err.message; }
  finally { button.disabled = false; }
}
async function preference(tag, direction, reset) {
  try {
    updateData(await api('/api/preferences', reset ? {tag:tag,action:'reset'} : {tag:tag,direction:direction}));
    toast(reset ? '已恢复自动学习，偏好已重新计算。' : direction === 0 ? '已删除这项记忆。可在下方恢复自动学习。' : '偏好已更新，下次推荐会参考你的设置。');
  } catch (err) { toast(err.message); }
}

function renderVenueOptions(preferred) {
  const current = preferred || $('venue-select').value;
  $('venue-select').innerHTML = state.catalog.venues.map((v) => '<option value="' + esc(v.id) + '">' + (v.imported ? '我的菜单 · ':'') + esc(v.full_name || v.name) + '</option>').join('');
  if (state.catalog.venues.some((v) => v.id === current)) $('venue-select').value = current;
}
function importedMenu() {
  return state.catalog && state.scene === 'menu' ? state.catalog.venues.find((v) => v.id === $('venue-select').value && v.imported) : null;
}
function syncMenuContext() {
  const menu = importedMenu();
  $('menu-context').hidden = !menu;
  if (menu) $('current-menu').textContent = menu.name + ' · ' + menu.dishes.length + ' 道菜';
  $('composer-hint').textContent = menu ? '当前按你发送的菜单选菜，价格可核对修改，时间为手动估计。' : '按你的常吃食堂统计推荐 · 价格为区间，五项指标以文字描述。';
  $('recommendations-footer').innerHTML = icon('info') + (menu ? '来自你发送的菜单 · 识别价格可修正' : '来自你的常吃食堂统计 · ' + state.catalog.entry_count + ' 项');
  $('minutes-label').textContent = menu ? '可用时间 / 分钟' : '可用时间参考 / 分钟';
  $('needs-hint').textContent = menu ? '预算、时间、忌口按菜单核验；时间含你设置的等待和用餐估计。' : '价格按区间核验预算；五项指标以文字描述，未提供具体耗时与配料。';
  try { if (menu) localStorage.setItem('eat-imported-menu',menu.id); else localStorage.removeItem('eat-imported-menu'); } catch (_) {}
}
function openMenuEntry() {
  if (state.busy) return;
  $('menu-entry').hidden = false; $('menu-edit-form').hidden = true;
  $('menu-dialog-title').textContent = '发菜单，帮你搭一餐';
  $('saved-menus').innerHTML = state.catalog.venues.filter((v) => v.imported).map((v) => '<button type="button" data-use-menu="' + esc(v.id) + '">' + esc(v.name) + ' · ' + v.dishes.length + ' 道菜</button>').join('');
  if (!$('menu-dialog').open) $('menu-dialog').showModal();
}
function renderMenuImages() {
  $('menu-images').innerHTML = state.menuImages.map((item,index) => '<div class="menu-image"><img src="' + item.url + '" alt="菜单图片 ' + (index+1) + '"><span>' + esc(item.name) + '</span><button type="button" data-remove-menu-image="' + index + '" aria-label="移除第' + (index+1) + '张菜单">×</button></div>').join('');
}
async function attachMenuImages(files) {
  if (state.busy) return;
  $('menu-import-error').textContent = '';
  try {
    if (files.length + state.menuImages.length > 3) throw new Error('一次最多发送 3 张菜单图片。');
    for (const file of files) {
      if (!['image/jpeg','image/png','image/webp'].includes(file.type) || file.size > 12*1024*1024) throw new Error('请选择不超过 12 MB 的 JPG、PNG 或 WebP 图片。');
      const url = await new Promise((resolve,reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error('图片读取失败，请重新选择。'));
        reader.onload = () => {
          const img = new Image();
          img.onerror = () => reject(new Error('图片无法打开，请换一张清晰的菜单。'));
          img.onload = () => {
            try {
              const scale = Math.min(1,1800/Math.max(img.width,img.height));
              const canvas = document.createElement('canvas');
              canvas.width = Math.max(1,Math.round(img.width*scale)); canvas.height = Math.max(1,Math.round(img.height*scale));
              const context = canvas.getContext('2d'); context.fillStyle = '#fff'; context.fillRect(0,0,canvas.width,canvas.height); context.drawImage(img,0,0,canvas.width,canvas.height);
              const resized = canvas.toDataURL('image/jpeg',.88);
              if (resized.length > 2600000) throw new Error('图片内容过大，请裁剪为菜单区域后重试。');
              resolve(resized);
            } catch (err) { reject(err); }
          };
          img.src = reader.result;
        };
        reader.readAsDataURL(file);
      });
      state.menuImages.push({url:url,name:file.name});
      renderMenuImages();
    }
  } catch (err) { $('menu-import-error').textContent = err.message; }
  finally { $('menu-files').value = ''; }
}
function menuReply(response) {
  const menu = response.menu;
  const plans = response.recommendations.plans;
  const p = plans.find((plan) => plan.id === response.recommendations.primary_id) || plans[0];
  let text = '从「' + menu.name + '」整理了 ' + menu.dishes.length + ' 道菜。';
  if (p) text += '\n\n可以搭配：' + p.items.map((item) => item.name + ' ×' + item.quantity).join('、') + '。\n总计 ¥' + money(p.total) + '，人均 ¥' + money(p.per_person) + '。预计 ' + p.minutes + ' 分钟，按你设置的等待和用餐时间估计。\n\n右侧保留其他可选组合，也可以继续告诉我想怎么调整。';
  else text += '\n\n目前还没有满足条件的组合：' + response.recommendations.conflicts.join('、') + '。可以在识别菜单中补充价格、核实配料，或调整本次需求。';
  if (response.recognition_notice) text += '\n\n' + response.recognition_notice;
  appendMessage('assistant',text,{mode:'chat',model:response.recognition_model,menuId:menu.id,plan:p,needs:response.needs});
}
function applyMenuResponse(response) {
  const old = importedMenu();
  if (!old || old.id !== response.menu.id) { state.activeMeal = null; persistMeal(); }
  state.catalog.venues = response.venues;
  renderVenueOptions(response.menu.id);
  applyChatRecommendations(response);
}
async function importMenu(event) {
  event.preventDefault();
  if (state.busy) return;
  const text = $('menu-text').value.trim();
  if (!text && !state.menuImages.length) { $('menu-import-error').textContent = '先选择菜单图片或粘贴菜单文字。'; return; }
  const data = {text:text,images:state.menuImages.map((item) => item.url),requirements:$('menu-requirements').value.trim(),needs:readNeeds(),model:state.model,vision_model:state.visionModel,wait:Number($('menu-wait').value),dine:Number($('menu-dine').value)};
  $('menu-import-error').textContent = ''; setBusy(true); state.pending = true;
  $('menu-import-submit').textContent = '正在识别并搭配…';
  appendMessage('user',(state.menuImages.length ? '发送了 ' + state.menuImages.length + ' 张菜单图片。' : '发送了一份文字菜单。') + (data.requirements ? '\n' + data.requirements : ''),{mode:'chat'});
  try {
    const response = await api('/api/menus/import',data);
    state.pending = false;
    applyMenuResponse(response); menuReply(response);
    $('menu-dialog').close(); state.menuImages = []; renderMenuImages(); $('menu-text').value = ''; $('menu-requirements').value = '';
  } catch (err) { $('menu-import-error').textContent = err.message + ' 可以重试，或将菜单文字粘贴后再发送。'; }
  finally { state.pending = false; setBusy(false); renderMessages(); renderPlans(); $('menu-import-submit').textContent = '识别并选菜'; }
}
function openMenuEditor(mid) {
  if (state.busy) return;
  const menu = state.catalog.venues.find((v) => v.id === mid && v.imported);
  if (!menu) { toast('这份菜单已删除。'); return; }
  if ($('detail-dialog').open) $('detail-dialog').close();
  state.editingMenu = menu;
  $('menu-entry').hidden = true; $('menu-edit-form').hidden = false;
  $('menu-dialog-title').textContent = '核对这份菜单';
  $('menu-edit-name').value = menu.name; $('menu-edit-wait').value = menu.wait; $('menu-edit-dine').value = menu.dine;
  $('menu-edit-error').textContent = '';
  $('menu-edit-rows').innerHTML = menu.dishes.map((d,index) => '<div class="menu-edit-row" data-menu-row="' + index + '"><label>菜名<input class="edit-dish-name" value="' + esc(d.name) + '" maxlength="60" required></label><label>单价 / 元<input class="edit-dish-price" type="number" min="0" max="5000" step="0.01" value="' + (d.price === null ? '' : d.price/100) + '" placeholder="未标价"></label><label>类型<select class="edit-dish-kind">' + [['dish','共享菜'],['meal','单人套餐'],['staple','主食'],['drink','饮品']].map(([value,label]) => '<option value="' + value + '"' + (d.kind === value ? ' selected':'') + '>' + label + '</option>').join('') + '</select></label><label class="menu-ingredients">配料<input class="edit-dish-ingredients" value="' + esc(d.ingredients.join('、')) + '" placeholder="请向店家核实，用顿号分隔"></label><label class="menu-known"><input class="edit-dish-known" type="checkbox"' + (d.ingredients_known ? ' checked':'') + '>我已核实这道菜的完整配料</label></div>').join('');
  if (!$('menu-dialog').open) $('menu-dialog').showModal();
}
async function saveMenuEdits(event) {
  event.preventDefault(); if (state.busy || !state.editingMenu) return;
  const menu = Object.assign({},state.editingMenu,{name:$('menu-edit-name').value.trim(),wait:Number($('menu-edit-wait').value),dine:Number($('menu-edit-dine').value)});
  menu.dishes = Array.from($('menu-edit-rows').querySelectorAll('[data-menu-row]')).map((row,index) => Object.assign({},menu.dishes[index],{
    name:row.querySelector('.edit-dish-name').value.trim(),price:row.querySelector('.edit-dish-price').value === '' ? null:Number(row.querySelector('.edit-dish-price').value),kind:row.querySelector('.edit-dish-kind').value,
    ingredients:row.querySelector('.edit-dish-ingredients').value.split(/[,，、；;\n]+/).map((value) => value.trim()).filter(Boolean),ingredients_known:row.querySelector('.edit-dish-known').checked}));
  setBusy(true); $('menu-edit-error').textContent = '';
  try {
    const response = await api('/api/menus/update',{menu_id:menu.id,menu:menu,needs:readNeeds()});
    applyMenuResponse(response); $('menu-dialog').close(); menuReply(response);
  } catch (err) { $('menu-edit-error').textContent = err.message; }
  finally { setBusy(false); renderPlans(); }
}
function exitMenu(announce) {
  if (state.busy) return;
  state.scene = 'canteen'; $('venue-select').value = state.catalog.venues[0].id;
  state.activeMeal = null; persistMeal(); syncControls();
  state.recommendationMealId = null; state.primaryId = null;
  refreshSidebar();
  if (announce) toast('已退出这份菜单，继续按食堂目录推荐。');
}

$('upload-menu').addEventListener('click',openMenuEntry);
$('current-menu').addEventListener('click',() => { const menu = importedMenu(); if (menu) openMenuEditor(menu.id); });
$('exit-menu').addEventListener('click',() => exitMenu(true));
$('menu-replace').addEventListener('click',openMenuEntry);
$('menu-files').addEventListener('change',(event) => attachMenuImages(Array.from(event.target.files)));
$('menu-import-form').addEventListener('submit',importMenu);
$('menu-edit-form').addEventListener('submit',saveMenuEdits);
['chat-input','menu-dialog'].forEach((id) => $(id).addEventListener('paste',(event) => {
  const files = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith('image/'));
  if (files.length && !state.busy) { event.preventDefault(); openMenuEntry(); attachMenuImages(files); }
}));
$('menu-dropzone').addEventListener('dragover',(event) => { event.preventDefault(); $('menu-dropzone').classList.add('drag-over'); });
$('menu-dropzone').addEventListener('dragleave',() => $('menu-dropzone').classList.remove('drag-over'));
$('menu-dropzone').addEventListener('drop',(event) => { event.preventDefault(); $('menu-dropzone').classList.remove('drag-over'); attachMenuImages(Array.from(event.dataTransfer.files)); });
$('menu-delete').addEventListener('click',async () => {
  if (state.busy || !state.editingMenu) return;
  const mid = state.editingMenu.id;
  try {
    const result = await api('/api/menus/delete',{menu_id:mid});
    const active = importedMenu(); state.catalog.venues = result.venues; renderVenueOptions();
    if (active && active.id === mid) exitMenu(false);
    $('menu-dialog').close(); toast('菜单已删除，已有用餐记录仍保留。');
  } catch (err) { $('menu-edit-error').textContent = err.message; }
});
$('chat-form').addEventListener('submit',(event) => { event.preventDefault(); sendMessage(); });
$('chat-input').addEventListener('keydown',(event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendMessage(); }
});
$('chat-mode').addEventListener('change',() => { syncMode(); clearError(); $('chat-input').focus(); });
$('model-button').addEventListener('click',() => openModels(false));
$('refresh-models').addEventListener('click',() => openModels(true));
$('option-search').addEventListener('input',renderPlans);
$('model-search').addEventListener('input',renderModels);
$('edit-conditions').addEventListener('click',editConditions);
$('shuffle-button').addEventListener('click',shuffle);
$('retry-button').addEventListener('click',() => { if (state.retry) state.retry(); });
$('fallback-button').addEventListener('click',() => { if (state.failedRequest) execute(state.failedRequest,'basic'); });
$('feedback-form').addEventListener('submit',saveFeedback);
$('new-chat').addEventListener('click',startNewChat);
$('needs-form').addEventListener('submit',(event) => {
  event.preventDefault();
  if (!checkNeeds()) return;
  changed();
  $('needs-settings').open = false;
  closePanels();
  $('chat-mode').value = 'meal'; syncMode();
  $('chat-input').focus(); toast('条件已更新，发送消息即可重新推荐。');
});
['budget','minutes','people','avoid','foods','avoid-foods','purpose','venue-select','portion-goal','pace-goal'].forEach((id) => $(id).addEventListener('input',changed));
document.querySelectorAll('[data-condition]').forEach((slider) => slider.addEventListener('input',() => {
  $(slider.dataset.condition).value = Number(slider.value) === 0 ? '':slider.value;
  changed(slider.dataset.condition);
}));
$('side-backdrop').addEventListener('click',closePanels);
$('reset-form').addEventListener('submit',async (event) => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    updateData(await api('/api/reset',{confirm:$('reset-confirm').value}));
    const hadMenu = importedMenu();
    state.catalog = await api('/api/catalog'); renderVenueOptions();
    if (hadMenu) exitMenu(false);
    $('reset-dialog').close(); toast('本地记录与偏好已清空。');
  } catch (err) { $('reset-error').textContent = err.message; }
  finally { button.disabled = false; }
});
document.addEventListener('submit',(event) => {
  if (event.target.id === 'preference-form') {
    event.preventDefault();
    preference($('preference-tag').value,Number($('preference-direction').value),false);
  }
});
document.addEventListener('click',async (event) => {
  const b = event.target.closest('button');
  if (!b || b.disabled) return;
  if (b.dataset.close) $(b.dataset.close).close();
  else if (b.dataset.model && !state.busy) {
    state.model = b.dataset.model;
    try { localStorage.setItem('eat-llm-model',state.model); } catch (_) {}
    syncModel(); clearError();
    $('model-dialog').close();
    $('chat-input').focus();
    toast('已切换为 ' + state.model + '，下一次回复开始使用。');
  }
  else if (b.hasAttribute('data-panel-close')) closePanels();
  else if (b.dataset.panel) openPanel(b.dataset.panel);
  else if (b.hasAttribute('data-edit-conditions')) editConditions();
  else if (b.dataset.open) { closePanels(); openDrawer(b.dataset.open); }
  else if (b.dataset.drawerTab) { state.drawerTab = b.dataset.drawerTab; renderDrawer(); }
  else if (b.dataset.editImportedMenu) openMenuEditor(b.dataset.editImportedMenu);
  else if (b.dataset.removeMenuImage !== undefined && !state.busy) {
    state.menuImages.splice(Number(b.dataset.removeMenuImage),1); renderMenuImages();
  }
  else if (b.dataset.useMenu && !state.busy) {
    const menu = state.catalog.venues.find((v) => v.id === b.dataset.useMenu && v.imported);
    if (menu) {
      state.activeMeal = null; persistMeal();
      applyNeeds(Object.assign({},readNeeds(),{scene:'menu',venue_id:menu.id}));
      refreshSidebar(); $('menu-dialog').close();
    }
  }
  else if (b.dataset.scene && !state.busy) { state.scene = b.dataset.scene; changed(); }
  else if (b.dataset.taste && !state.busy) {
    const t = b.dataset.taste;
    state.tastes = state.tastes.includes(t) ? state.tastes.filter((x) => x !== t):state.tastes.concat(t);
    changed();
  } else if (b.dataset.suggestion && !state.busy) {
    $('chat-mode').value = b.dataset.mode; syncMode(); clearError();
    $('chat-input').value = b.dataset.suggestion; $('chat-input').focus();
  } else if (b.dataset.detail !== undefined) openDetail(Number(b.dataset.detail));
  else if (b.dataset.choose !== undefined) chooseOption(Number(b.dataset.choose));
  else if (b.dataset.chatPlan) {
    const m = state.messages.find((item) => item.id === b.dataset.chatPlan);
    if (m && m.plan) openDetail(0,m.plan,m.needs,m.mealId);
  } else if (b.dataset.quantity) {
    const id = b.dataset.dish;
    state.detail.items[id] = Math.max(0,Math.min(24,(state.detail.items[id] || 0) + Number(b.dataset.quantity)));
    $('quantity-' + id).textContent = state.detail.items[id]; quote();
  } else if (b.id === 'confirm-meal') confirmMeal();
  else if (b.dataset.feedback) openFeedback(b.dataset.feedback);
  else if (b.dataset.cancelMeal || b.dataset.restoreMeal) {
    const id = b.dataset.cancelMeal || b.dataset.restoreMeal;
    b.disabled = true;
    try {
      updateData(await api('/api/meals/cancel',{meal_id:id,cancelled:Boolean(b.dataset.cancelMeal)}));
      if (b.dataset.cancelMeal) {
        finishMeal(id);
        if (state.recommendationMealId === id) { state.recommendationMealId = null; state.primaryId = null; renderPlans(); }
        appendMessage('assistant','已撤销这餐，不再计入用餐间隔和偏好学习。',{mode:'meal',restoreMealId:id});
      } else {
        appendMessage('assistant','已恢复这条用餐记录。',{mode:'meal'});
      }
    } catch (err) { toast(err.message); b.disabled = false; }
  }
  else if (b.dataset.learning) {
    b.disabled = true;
    try { updateData(await api('/api/learning',{meal_id:b.dataset.learning,enabled:b.dataset.enabled === 'true'})); toast('学习状态已更新，偏好已重新计算。'); }
    catch (err) { toast(err.message); b.disabled = false; }
  } else if (b.dataset.memoryAction) {
    const m = state.data.memories.find((item) => item.tag === b.dataset.tag);
    if (m) preference(m.tag,b.dataset.memoryAction === 'delete' ? 0:-m.direction,b.dataset.memoryAction === 'reset');
  } else if (b.id === 'restore-auto') preference($('preference-tag').value,0,true);
  else if (b.id === 'clear-data') { $('reset-confirm').value = ''; $('reset-error').textContent = ''; $('reset-dialog').showModal(); }
});
document.addEventListener('keydown',(event) => {
  const panel = document.querySelector('.panel-open');
  if (!panel || document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape') { event.preventDefault(); closePanels(); }
  if (event.key === 'Tab') {
    const items = Array.from(panel.querySelectorAll('a,button:not(:disabled),input:not(:disabled),select:not(:disabled),summary')).filter((el) => el.getClientRects().length > 0);
    const first = items[0], last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }
});
window.addEventListener('resize',() => {
  if ((innerWidth > 999 && $('left-sidebar').classList.contains('panel-open')) || (innerWidth > 700 && $('right-sidebar').classList.contains('panel-open'))) closePanels();
});
document.querySelectorAll('dialog').forEach((dialog) => dialog.addEventListener('click',(event) => {
  if (event.target === dialog) {
    const r = dialog.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();
  }
}));
boot();
