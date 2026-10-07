'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const icon = name => `<i data-lucide="${name}"></i>`;
  const icons = () => window.lucide?.createIcons();
  const stageName = {early:'前期', mid:'中期', late:'后期'};
  const intentNames = {Attack:'攻击', Buff:'强化', Debuff:'减益', DebuffStrong:'强力减益', StatusCard:'状态牌', CardDebuff:'牌组干扰', Defend:'格挡', Sleep:'休眠', Summon:'召唤', Stun:'眩晕', Unknown:'未知'};
  let catalog = {scenarios:[], cards:[], whitelist:[], encounters:[]};
  let state = {active:false, revision:0, busy:false};
  let selectedCard = null;
  let selectedPotion = null;
  let sourceFilter = 'all';
  let pickedHand = [];
  let selectedIndices = new Set();
  let lastSelection = '';
  let lastJob = '';
  let renderedScene = '';
  let requestPending = false;
  let annotationMode = 'fast';
  function batchOptions() {
    return {source:$('batch-source').value,stage:$('batch-stage').value,position:$('batch-position').value};
  }
  function mode() {
    $('quick-annotation').hidden = annotationMode !== 'fast';
    $('comparison-panel').hidden = annotationMode !== 'compare';
    $('save-plan').hidden = annotationMode !== 'compare';
    $('plan-name').hidden = annotationMode !== 'compare';
    $('feature-list').parentElement.hidden = annotationMode !== 'compare' || !state.plans?.some(plan=>plan.source==='human');
    $('annotation-mode').querySelectorAll('button').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.mode===annotationMode)));
  }
  const cardMap = () => new Map(catalog.cards.map(card => [card.id, card]));
  const name = id => {const card = catalog.cards.find(c => c.id === id); return card?.name_zh || card?.name_en || id;};
  const art = id => `/api/card-art?id=${encodeURIComponent(id)}`;
  function error(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
  async function json(url, options) {const response = await fetch(url, options); const body = await response.json(); if (!response.ok) throw new Error(body.error || response.statusText); return body;}
  async function loadCatalog() {
    catalog = await json('/api/sandbox/catalog');
    $('encounter-options').innerHTML = catalog.encounters.map(row => `<option value="${esc(row.id)}">${esc(row.name)}</option>`).join('');
    $('resume-draft').hidden = !catalog.draft_available || state.active;
    renderScenes(); renderLibrary();
  }
  function renderScenes() {
    const query = $('scene-filter').value.toLowerCase();
    $('scene-list').innerHTML = catalog.scenarios.filter(row => (sourceFilter === 'all' || row.source === sourceFilter) && row.title.toLowerCase().includes(query)).map(row =>
      `<button class="scene-item ${state.scene?.id === row.id ? 'active' : ''}" data-scene="${esc(row.id)}"><strong>${esc(row.title)}</strong><small>${row.source === 'recorded' ? '实战快照' : '人工构造'} · ${stageName[row.stage] || row.stage}</small></button>`).join('');
    $('scene-list').querySelectorAll('[data-scene]').forEach(button => {button.disabled = state.busy; button.onclick = () => send('load', {id:button.dataset.scene});});
  }
  async function send(command, options = {}) {
    if (requestPending || state.busy) return;
    requestPending = true; controls(); error('');
    try {
      state = await json('/api/sandbox/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command, options, revision:state.revision, request_id:crypto.randomUUID()})});
      selectedCard = null;
      selectedPotion = null;
      render();
    } catch (exc) {error(exc.message);}
    finally {requestPending = false; controls();}
  }
  function controls() {
    const busy = state.busy || requestPending;
    const active = state.active;
    $('new-scene').disabled = busy;
    $('generate').disabled = busy;
    $('resume-draft').disabled = busy;
    $('undo').disabled = busy || !state.can_undo;
    $('redo').disabled = busy || !state.can_redo;
    $('reset').disabled = busy || !active;
    $('save-plan').disabled = busy || !state.finished || state.blocked;
    $('save-demo').disabled = busy || !state.can_demonstrate || state.blocked;
    $('save-next').disabled = $('save-demo').disabled;
    $('next-scene').disabled = busy;
    $('skip-scene').disabled = busy;
    $('checkpoint').disabled = busy || !state.can_checkpoint || state.blocked;
    $('agent-plan').disabled = busy || !active || state.blocked;
    $('agent-hint').disabled = busy || !active || state.finished || state.blocked || !!state.selection;
    $('end-turn').disabled = busy || state.blocked || !state.actions?.some(row => row.action_type === 'end_turn');
    $('save-label').disabled = busy || !$('plan-a').value || !$('plan-b').value || $('plan-a').value === $('plan-b').value || !document.querySelector('input[name=preference]:checked');
    document.querySelectorAll('.card-play').forEach(button => {button.disabled = busy || state.blocked || button.dataset.playable !== 'true';});
    document.querySelectorAll('.scene-item').forEach(button => {button.disabled = busy;});
    document.querySelectorAll('.enemy').forEach(button => {button.disabled = busy || button.dataset.targetable !== 'true';});
    document.querySelectorAll('[data-potion]').forEach(button=>{button.disabled=busy||state.blocked;});
    const commands = {load:'加载场景',generate:'生成场景',action:'结算动作',undo:'重放前缀',redo:'重放前缀',reset:'恢复快照',save_plan:'验证方案',agent:'搜索对照',label:'保存标注',resume:'恢复草稿',next:'准备下一题',demonstrate:'保存示范 / 准备下一题',skip:'跳过 / 准备下一题',checkpoint:'保存决策点'};
    $('job-status').textContent = busy ? `${commands[state.job?.command] || '处理中'}…` : state.job?.status === 'failed' ? '操作失败' : active ? '就绪' : '未加载场景';
  }
  function render() {
    controls();
    const progress=state.collection||{};
    $('collection-count').textContent=`已标 ${progress.scenes||0} 题 · ${progress.decisions||0} 步`;
    const receipt=state.last_submission;
    $('submission-receipt').hidden=!receipt;
    $('submission-receipt').textContent=receipt ? `${receipt.saved ? `已保存 ${receipt.steps} 步示范。` : ''}${receipt.queue_exhausted ? '当前筛选下没有待标题。' : ''}` : '';
    mode();
    if (!state.active) {icons(); return;}
    if (renderedScene !== state.scene.id) {
      renderedScene = state.scene.id;
      $('plan-name').value = ''; $('annotation-note').value = ''; $('demo-note').value='';
      document.querySelectorAll('input[name=preference]').forEach(input=>{input.checked=false;});
    }
    $('scene-title').textContent = state.scene.title;
    $('stage-badge').textContent = stageName[state.scene.stage];
    $('mode-badge').textContent = state.hint_seen ? '已看提示' : state.explored ? '探索' : '首次作答';
    $('turn-label').textContent = `回合 ${state.turn ?? '—'}`;
    const context=state.turn_context||{};
    $('position-badge').textContent=context.position==='mid' ? `起点已行动 · ${context.cards_played_before_root ?? '?'} 张牌` : context.position==='start' ? '回合起点' : '位置未标定';
    $('prelude-details').hidden=!(state.root_turn_history||[]).length;
    $('prelude-summary').textContent=`起点前行动 · ${(state.root_turn_history||[]).length} 步`;
    $('prelude-actions').innerHTML=(state.root_turn_history||[]).map(row=>`<li>${esc(actionText(row))}</li>`).join('');
    $('demo-step-count').textContent=`${(state.history||[]).filter(row=>row.action_type!=='select_cards').length} 步`;
    $('player-hp').textContent = `${state.player?.hp ?? '—'} / ${state.player?.max_hp ?? '—'}`;
    $('player-energy').textContent = state.player?.energy ?? '—';
    $('player-block').textContent = state.player?.block ?? 0;
    $('player-powers').innerHTML = (state.player?.powers || []).map(p => `<span class="power">${esc(p.name || p.id)} ${esc(p.amount)}</span>`).join('');
    $('player-potions').innerHTML = (state.potions || []).map(p=>`<button data-potion="${p.index}" title="使用${esc(p.name)}">${icon('flask-conical')}${esc(p.name)}</button>`).join('');
    $('player-potions').querySelectorAll('button').forEach(button=>{button.onclick=()=>{const index=Number(button.dataset.potion);const actions=state.actions.filter(a=>a.action_type==='use_potion'&&a.metadata.potion_index===index);if(actions.some(a=>a.target_index!=null&&a.metadata.target_type==='AnyEnemy')){selectedCard=null;selectedPotion=index;render();}else if(actions.length)send('action',{action_id:actions[0].id});};});
    $('terminal').textContent = state.terminal ? ({game_over:'战败',defeat:'战败'}[state.terminal] || '战斗结束') : state.finished ? '回合已结算' : '';
    renderEnemies(); renderHand(); renderPlans(); renderSelection(); renderScenes();
    $('action-list').innerHTML = (state.history || []).map(row => `<li>${esc(actionText(row))}</li>`).join('');
    const reveal = state.plans?.some(plan => plan.source === 'human');
    $('feature-list').parentElement.hidden = !reveal || annotationMode !== 'compare';
    $('feature-list').innerHTML = (state.score?.contributions || []).map(row => `<div class="feature-row"><span>${esc(row.label)}</span><span class="${row.score >= 0 ? 'positive' : 'negative'}">${row.score.toFixed(2)}</span></div>`).join('');
    $('label-count').textContent = `${state.labels?.length || 0} 条标注`;
    renderHint();
    $('resume-draft').hidden = true;
    controls(); icons();
  }
  function actionText(row) {
    if (row.action_type === 'end_turn') return '结束回合';
    if (row.action_type === 'select_cards') return `选牌 ${row.indices.map(i => i + 1).join('、')}`;
    const label = row.action_type === 'use_potion' ? (row.metadata?.potion_id || '药水') : name(row.metadata?.card_id || '卡牌');
    return label + (row.target_index != null ? ` → 敌人 ${row.target_index + 1}` : '');
  }
  function renderHint() {
    const scorer = state.scorer;
    $('active-scorer').textContent = scorer ? `${scorer.feature_version} · ${scorer.trained ? '已训练权重' : '初始权重'} · ${scorer.weights_sha256.slice(0,8)}` : '—';
    const hint=state.agent_hint;
    $('hint-result').hidden=!hint;
    if(!hint)return;
    const verified=hint.verification==='PASS';
    $('hint-first').textContent=`${state.hint_current ? '建议下一步' : '此前局面建议'}：${hint.line?.length ? actionText(hint.line[0]) : '无可用计划'}`;
    $('hint-verification').textContent=verified ? '重放与评分一致' : '验证未通过';
    $('hint-line').textContent=(hint.line||[]).map(actionText).join(' → ');
    const root=hint.audit?.root?.candidate_coverage_ratio;
    const edge=hint.audit?.turn_space?.candidate_coverage_ratio;
    const pct=value=>typeof value==='number'?`${(value*100).toFixed(0)}%`:'—';
    $('hint-metrics').textContent=`${Number(hint.score).toFixed(2)} 分 · ${(Number(hint.search_ms||0)/1000).toFixed(2)} s · 根覆盖 ${pct(root)} · 已发现边覆盖 ${pct(edge)} · 完整快照搜索${state.hint_current ? '' : ' · 局面已改变，可重新查询'}`;
    $('hint-candidates').innerHTML=(hint.candidates||[]).map(row=>`<div class="hint-candidate"><span>${esc(actionText(row.action))}</span><b>${row.score==null?'无效':Number(row.score).toFixed(2)}</b></div>`).join('');
    $('hint-scores').innerHTML=(hint.explanation?.contributions||[]).map(row=>`<div class="feature-row"><span>${esc(row.label)}</span><span class="${row.score>=0?'positive':'negative'}">${Number(row.score).toFixed(2)}</span></div>`).join('');
  }
  function renderEnemies() {
    const selected = row => selectedCard !== null ? row.card_index === selectedCard && row.action_type === 'play_card' : selectedPotion !== null && row.action_type === 'use_potion' && row.metadata?.potion_index === selectedPotion;
    const targets = new Set((state.actions || []).filter(selected).map(row => row.target_index));
    $('enemy-list').innerHTML = (state.enemies || []).map((enemy, position) => {
      const targetable = (selectedCard !== null || selectedPotion !== null) && targets.has(enemy.index ?? position);
      const intent = enemy.intent || {};
      const types = (intent.intent_types || []).map(value => intentNames[value] || value).join(' / ');
      const attack = (intent.intent_types || []).includes('Attack');
      return `<button class="enemy ${targetable ? 'targetable' : ''}" data-enemy="${enemy.index ?? position}" data-targetable="${targetable}"><div class="enemy-header"><span class="enemy-symbol">${icon('skull')}</span><strong>${position + 1}. ${esc(enemy.name)}</strong></div><div class="intent">${icon(attack ? 'swords' : 'circle-dot')}<span>${esc(types)} ${attack ? `${intent.display_damage ?? '?'} × ${intent.hits || 1}` : ''}</span></div><div class="hp-line"><span>${enemy.hp} / ${enemy.max_hp}</span><span>格挡 ${enemy.block || 0}</span></div><div class="hp-track"><div class="hp-fill" style="width:${Math.max(0,Math.min(100,100*enemy.hp/Math.max(1,enemy.max_hp)))}%"></div></div><div class="powers">${(enemy.powers || []).map(p => `<span class="power">${esc(p.name || p.id)} ${esc(p.amount)}</span>`).join('')}</div></button>`;
    }).join('');
    $('enemy-list').querySelectorAll('[data-enemy]').forEach(button => {button.onclick = () => {const target = Number(button.dataset.enemy);const action = state.actions.find(row => selected(row) && row.target_index === target);if (action) send('action', {action_id:action.id});};});
  }
  function renderHand() {
    $('cancel-target').hidden = selectedCard === null && selectedPotion === null;
    $('hand').innerHTML = (state.hand || []).map(card => {
      const playable = state.actions?.some(row => row.action_type === 'play_card' && row.card_index === card.index);
      return `<article class="hand-card ${selectedCard === card.index ? 'selected' : ''} ${playable ? '' : 'unplayable'}"><button class="card-play" data-card="${card.index}" data-playable="${!!playable}" aria-label="打出${esc(card.name)}" title="${playable ? '选择出牌' : '当前不可使用'}"><img class="card-art" src="${art(card.id)}" alt="${esc(card.name)}" onerror="this.style.visibility='hidden'"><span class="card-cost">${esc(card.cost)}</span><span class="card-content"><strong class="card-name">${esc(card.name)}${card.upgraded ? ' +' : ''}</strong><span class="card-description">${esc(card.description)}</span></span></button><div class="card-footer"><span>${esc(card.type)}</span><button class="card-info" data-info="${card.index}" title="卡牌详情" aria-label="${esc(card.name)}详情">${icon('info')}</button></div></article>`;
    }).join('');
    $('hand').querySelectorAll('[data-card]').forEach(button => {button.onclick = () => {selectedPotion=null;const index = Number(button.dataset.card);const actions = state.actions.filter(row => row.action_type === 'play_card' && row.card_index === index);if (!actions.length) return;if (actions.some(row => row.target_index != null)) {selectedCard = selectedCard === index ? null : index;render();} else send('action',{action_id:actions[0].id});};});
    $('hand').querySelectorAll('[data-info]').forEach(button => {button.onclick = () => openCard(state.hand.find(card => card.index === Number(button.dataset.info)));});
  }
  function openCard(card) {
    $('detail-art').src = art(card.id); $('detail-art').alt = card.name;
    $('detail-name').textContent = card.name + (card.upgraded ? ' +' : '');
    $('detail-type').textContent = `${card.type || ''} · ${card.cost} 费`;
    $('detail-description').textContent = card.description || '';
    $('card-dialog').showModal();
  }
  function renderPlans() {
    const plans = state.plans || [];
    for (const [id, fallback] of [['plan-a',0],['plan-b',1]]) {
      const old = $(id).value;
      $(id).innerHTML = plans.map(plan => `<option value="${plan.id}">${esc(plan.name)} · ${plan.source === 'human' ? '人工' : 'Agent'}</option>`).join('');
      $(id).value = plans.some(plan => plan.id === old) ? old : plans[fallback]?.id || '';
    }
    renderComparison();
  }
  function renderComparison() {
    $('plan-comparison').innerHTML = ['plan-a','plan-b'].map(id => {
      const plan = state.plans?.find(p => p.id === $(id).value);
      if (!plan) return '<div class="plan-summary"><p>尚无方案</p></div>';
      const values = plan.features.base_values || plan.features.values;
      const hp = values.hp_change * 10;
      return `<div class="plan-summary"><p class="plan-value">生命 ${hp >= 0 ? '+' : ''}${hp.toFixed(0)} · 伤害 ${(values.enemy_hp_removed * 10).toFixed(0)}</p><p>${plan.line.map(action => esc(actionText(action))).join(' → ')}</p><p>${plan.verified ? '重放一致' : '未验证'} · ${plan.explored ? '探索' : '首次作答'}</p><details><summary>初始评分 ${plan.score.score.toFixed(2)}</summary><p>能力预计贡献 ${Number(plan.score.ability_score || 0).toFixed(2)}</p></details></div>`;
    }).join('');
    controls();
  }
  function renderSelection() {
    if (!state.selection) {if ($('selection-dialog').open) $('selection-dialog').close();lastSelection = '';return;}
    const key = JSON.stringify(state.selection);
    if (key !== lastSelection) {selectedIndices = new Set();lastSelection = key;}
    $('selection-prompt').textContent = `选择 ${state.selection.min_select ?? '?'}—${state.selection.max_select ?? '?'} 张牌`;
    $('selection-options').innerHTML = (state.selection.cards || []).map(card => `<button data-selection="${card.index}" aria-pressed="${selectedIndices.has(card.index)}">${esc(name(String(card.id || card.card_id || '').replace('CARD.','')))}</button>`).join('');
    $('selection-options').querySelectorAll('button').forEach(button => {button.onclick = () => {const index = Number(button.dataset.selection);selectedIndices.has(index) ? selectedIndices.delete(index) : selectedIndices.add(index);renderSelection();};});
    $('confirm-selection').disabled = state.busy || !Number.isInteger(state.selection.min_select) || !Number.isInteger(state.selection.max_select) || selectedIndices.size < state.selection.min_select || selectedIndices.size > state.selection.max_select;
    if (!$('selection-dialog').open) $('selection-dialog').showModal();
  }
  function renderLibrary() {
    const allowed = new Set(catalog.whitelist.map(card => card.id));
    const query = $('hand-search').value.toLowerCase();
    $('hand-library').innerHTML = catalog.cards.filter(card => allowed.has(card.id) && `${card.name_zh} ${card.name_en} ${card.id}`.toLowerCase().includes(query)).map(card => `<button type="button" class="library-card" data-add="${card.id}"><img src="${art(card.id)}" alt="" onerror="this.style.visibility='hidden'"><span>${esc(card.name_zh || card.name_en)}</span></button>`).join('');
    $('hand-library').querySelectorAll('[data-add]').forEach(button => {button.onclick = () => {if (pickedHand.length < 10) {pickedHand.push(button.dataset.add);renderPickedHand();}};});
    renderPickedHand();
  }
  function renderPickedHand() {
    $('picked-hand').innerHTML = pickedHand.map((id,index) => `<button type="button" data-remove="${index}">${esc(name(id))}${icon('x')}</button>`).join('');
    $('picked-hand').querySelectorAll('[data-remove]').forEach(button => {button.onclick = () => {pickedHand.splice(Number(button.dataset.remove),1);renderPickedHand();};});
    const caps=Object.fromEntries(catalog.whitelist.map(card=>[card.id,card.max_copies]));
    $('hand-library').querySelectorAll('[data-add]').forEach(button=>{const cap=caps[button.dataset.add]||1;button.disabled=pickedHand.length>=10||pickedHand.filter(id=>id===button.dataset.add).length>=cap;button.title=`上限 ${cap} 张`;});
    icons();
  }
  $('scene-filter').oninput = renderScenes;
  $('source-filter').querySelectorAll('button').forEach(button => {button.onclick = () => {sourceFilter=button.dataset.source;$('source-filter').querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));renderScenes();};});
  $('undo').onclick = () => send('undo'); $('redo').onclick = () => send('redo'); $('reset').onclick = () => send('reset');
  $('annotation-mode').querySelectorAll('button').forEach(button=>{button.onclick=()=>{annotationMode=button.dataset.mode;mode();};});
  for(const id of ['batch-source','batch-stage','batch-position']) $(id).onchange=()=>localStorage.setItem('sandbox-batch',JSON.stringify(batchOptions()));
  try {const saved=JSON.parse(localStorage.getItem('sandbox-batch')||'null');if(saved)for(const key of ['source','stage','position'])if(saved[key])$('batch-'+key).value=saved[key];}catch{}
  $('next-scene').onclick=()=>send('next',{batch:batchOptions()});
  $('skip-scene').onclick=()=>send('skip',{batch:batchOptions(),note:$('demo-note').value});
  const submitDemo=next=>send('demonstrate',{next,batch:batchOptions(),confidence:$('demo-confidence').value,note:$('demo-note').value});
  $('save-demo').onclick=()=>submitDemo(false);
  $('save-next').onclick=()=>submitDemo(true);
  $('checkpoint').onclick=()=>send('checkpoint');
  document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.key==='Enter'&&annotationMode==='fast'&&!$('save-next').disabled){event.preventDefault();submitDemo(true);}});
  $('resume-draft').onclick = () => send('resume');
  $('cancel-target').onclick = () => {selectedCard=null;selectedPotion=null;render();};
  $('end-turn').onclick = () => {const action=state.actions.find(row=>row.action_type==='end_turn');if(action)send('action',{action_id:action.id});};
  $('save-plan').onclick = () => send('save_plan',{name:$('plan-name').value});
  $('agent-plan').onclick = () => send('agent');
  $('agent-hint').onclick = () => send('agent',{origin:'current'});
  $('plan-a').onchange = renderComparison; $('plan-b').onchange = renderComparison;
  document.querySelectorAll('input[name=preference]').forEach(input => {input.onchange=controls;});
  $('save-label').onclick = () => send('label',{a:$('plan-a').value,b:$('plan-b').value,preference:document.querySelector('input[name=preference]:checked')?.value,confidence:$('confidence').value,note:$('annotation-note').value});
  $('new-scene').onclick = () => $('scene-dialog').showModal();
  $('close-scene').onclick = () => $('scene-dialog').close();
  $('close-card').onclick = () => $('card-dialog').close();
  $('selection-dialog').addEventListener('cancel',event=>event.preventDefault());
  $('confirm-selection').onclick = () => send('action',{indices:[...selectedIndices]});
  $('reset-selection').onclick = () => {$('selection-dialog').close();send('reset');};
  $('hand-search').oninput = renderLibrary;
  $('scene-form').onsubmit = event => {event.preventDefault();const data=Object.fromEntries(new FormData(event.target));for(const key of ['hp','energy','enemy_hp','warmup_steps'])data[key]=Number(data[key]);data.upgrade_hand=!!data.upgrade_hand;data.hand=[...pickedHand];$('scene-dialog').close();send('generate',data);};
  $('export').onclick = async () => {try {const response=await fetch('/api/sandbox/export');if(!response.ok)throw new Error('导出失败');const url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='sandbox-dataset.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(exc){error(exc.message);}};
  async function poll() {
    try {
      const next = await json('/api/sandbox/state');
      const changed = next.revision !== state.revision || next.busy !== state.busy || next.job?.id !== state.job?.id;
      state = next;
      if(state.job?.status==='failed')error(state.job.error);
      if(state.job?.status==='completed' && state.job.id !== lastJob) {lastJob=state.job.id;error('');await loadCatalog();}
      if(changed)render();else controls();
    }catch(exc){error(exc.message);}
    setTimeout(poll,600);
  }
  (async()=>{try{await loadCatalog();state=await json('/api/sandbox/state');render();if(!state.active&&!catalog.draft_available){if(catalog.scenarios.length)await send('load',{id:catalog.scenarios[0].id});else await send('next',{batch:batchOptions()});}}catch(exc){error(exc.message);}poll();})();
})();
