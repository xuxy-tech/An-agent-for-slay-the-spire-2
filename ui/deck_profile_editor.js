'use strict';

let deckProfileDraft = null;
let deckProfileDirty = false;
let deckProfileLocked = false;
let cardCatalog = [];
let previewCard = null;
let previewUpgraded = false;

function profileNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function catalogCard(cardId) {
  return cardCatalog.find(card => card.id === cardId) || {id: cardId, name_en: cardId, name_zh: '', type: '', rarity: '', cost: '?', description_en: '', description_zh: ''};
}

function cardIcon(type) {
  return ({ATTACK:'swords', SKILL:'shield', POWER:'sparkles', CURSE:'skull'})[String(type || '').toUpperCase()] || 'square';
}

function cardArtUrl(cardId) {
  return `/api/card-art?id=${encodeURIComponent(cardId)}`;
}

function cleanCardDescription(value) {
  let textValue = String(value || '').trim();
  let open = textValue.indexOf('[');
  while (open >= 0) {
    const close = textValue.indexOf(']', open + 1);
    if (close < 0) break;
    textValue = textValue.slice(0, open) + textValue.slice(close + 1);
    open = textValue.indexOf('[', open);
  }
  return textValue || '暂无描述';
}

function createCardIdentity(card) {
  const trigger = document.createElement('button');
  trigger.type = 'button';
  trigger.className = 'card-identity card-preview-trigger';
  trigger.title = '查看卡图与描述';
  const art = document.createElement('span');
  art.className = 'card-art-thumb';
  const image = document.createElement('img');
  image.src = cardArtUrl(card.id);
  image.alt = '';
  image.loading = 'lazy';
  image.addEventListener('load', () => art.classList.add('loaded'));
  image.addEventListener('error', () => image.remove());
  const fallback = document.createElement('i');
  fallback.setAttribute('data-lucide', cardIcon(card.type));
  art.append(image, fallback);
  const names = document.createElement('span');
  const primary = document.createElement('strong');
  primary.textContent = card.name_zh ? `${card.name_zh} · ${card.name_en}` : card.name_en;
  const details = document.createElement('small');
  details.textContent = `${card.id} · ${card.type || 'Unknown'} · ${card.rarity || 'Unknown'}`;
  names.append(primary, details);
  trigger.append(art, names);
  trigger.addEventListener('click', () => openCardPreview(card));
  return trigger;
}

function addCardToProfile(cardId) {
  if (!deckProfileDraft?.ports.length || deckProfileDraft.cards.some(card => card.id === cardId)) return;
  deckProfileDraft.cards.push({id: cardId, max_copies: 1, ports: [deckProfileDraft.ports[0].id]});
  $('profile-card-search').value = '';
  markProfileDirty();
  renderDeckProfileFields();
}

function renderCardPreviewState() {
  const card = previewCard;
  if (!card) return;
  const upgraded = previewUpgraded && card.upgradable;
  text('card-preview-name', card.name_zh || card.name_en || card.id);
  text('card-preview-name-en', card.name_zh ? card.name_en : card.id);
  const cost = $('card-preview-cost');
  const rawCost = upgraded ? card.cost_upgraded : card.cost;
  const costText = Number(rawCost) === -1 ? 'X' : String(rawCost ?? '?');
  cost.hidden = !costText || costText === '?';
  text('card-preview-cost', costText);
  text('card-preview-meta', `${card.id} · ${card.type || 'Unknown'} · ${card.rarity || 'Unknown'} · ${card.pool || 'Unknown'}`);
  text('card-preview-description-zh', cleanCardDescription(upgraded ? card.description_zh_upgraded : card.description_zh));
  text('card-preview-description-en', cleanCardDescription(upgraded ? card.description_en_upgraded : card.description_en));
  const level = $('card-preview-level');
  level.hidden = !card.upgradable;
  level.querySelectorAll('button').forEach(button => {
    const active = (button.dataset.upgraded === 'true') === upgraded;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
}

function openCardPreview(card) {
  previewCard = card;
  previewUpgraded = false;
  renderCardPreviewState();
  const image = $('card-preview-image');
  const fallback = $('card-preview-fallback');
  image.hidden = false;
  fallback.hidden = true;
  image.alt = `${card.name_zh || card.name_en} 卡图`;
  image.onload = () => { image.hidden = false; fallback.hidden = true; };
  image.onerror = () => { image.hidden = true; fallback.hidden = false; lucide.createIcons(); };
  image.src = cardArtUrl(card.id);
  const selected = deckProfileDraft?.cards.some(value => value.id === card.id);
  const add = $('card-preview-add');
  add.disabled = deckProfileLocked || selected || !deckProfileDraft?.ports.length;
  add.lastChild.textContent = selected ? '已在白名单' : '加入白名单';
  $('card-preview').showModal();
  lucide.createIcons();
}

function moveItem(rows, index, direction) {
  const target = index + direction;
  if (target < 0 || target >= rows.length) return;
  [rows[index], rows[target]] = [rows[target], rows[index]];
  markProfileDirty();
  renderDeckProfileFields();
}

async function loadCardCatalog() {
  try {
    const response = await fetch('/api/card-catalog');
    const data = await response.json();
    if (response.ok) cardCatalog = data.cards || [];
  } catch (_) {
    cardCatalog = [];
  }
  renderDeckProfileFields();
}

function renderDeckProfileEditor(data) {
  const incoming = data?.deck_profile;
  if (!incoming || deckProfileDirty) return;
  deckProfileDraft = structuredClone(incoming);
  delete deckProfileDraft._source;
  deckProfileDraft.ports ||= [];
  deckProfileDraft.cards ||= [];
  renderDeckProfileFields();
}

function renderDeckProfileFields() {
  if (!deckProfileDraft) return;
  $('profile-id').value = deckProfileDraft.id || '';
  $('profile-name').value = deckProfileDraft.name || '';
  renderProfilePorts();
  renderProfileCards();
  renderCardLibrary();
  text('profile-summary', `${deckProfileDraft.id || '未命名'} · ${deckProfileDraft.ports.length} 个端口 · ${deckProfileDraft.cards.length} 张白名单牌`);
  lucide.createIcons();
}

function markProfileDirty() {
  deckProfileDirty = true;
  $('profile-save').disabled = deckProfileLocked;
  text('profile-summary', '配置已修改，尚未保存');
}

function iconButton(icon, title, handler, disabled=false) {
  const button = document.createElement('button');
  button.type = 'button';
  button.title = title;
  button.setAttribute('aria-label', title);
  button.innerHTML = `<i data-lucide="${icon}"></i>`;
  button.disabled = deckProfileLocked || disabled;
  button.addEventListener('click', handler);
  return button;
}

function orderControls(rows, index) {
  const controls = document.createElement('div');
  controls.className = 'order-controls';
  controls.append(
    iconButton('chevron-up', '上移', () => moveItem(rows, index, -1), index === 0),
    iconButton('chevron-down', '下移', () => moveItem(rows, index, 1), index === rows.length - 1),
  );
  return controls;
}

function appendCells(row, values) {
  for (const value of values) {
    const cell = document.createElement('td');
    if (typeof value === 'string') cell.textContent = value;
    else cell.append(value);
    row.append(cell);
  }
}

function renderProfilePorts() {
  const body = $('profile-ports');
  body.replaceChildren();
  deckProfileDraft.ports.forEach((port, index) => {
    const row = document.createElement('tr');
    const name = document.createElement('input');
    name.value = port.name || port.id;
    name.disabled = deckProfileLocked;
    const target = document.createElement('input');
    target.type = 'number'; target.min = '0'; target.value = port.target ?? 0; target.disabled = deckProfileLocked;
    const maximum = document.createElement('input');
    maximum.type = 'number'; maximum.min = '0'; maximum.value = port.maximum ?? port.target ?? 0; maximum.disabled = deckProfileLocked;
    name.addEventListener('change', () => { port.name = name.value.trim() || port.id; markProfileDirty(); });
    target.addEventListener('change', () => {
      port.target = Math.max(0, profileNumber(target.value, 0));
      port.maximum = Math.max(port.target, profileNumber(maximum.value, port.target));
      maximum.value = port.maximum;
      markProfileDirty();
    });
    maximum.addEventListener('change', () => {
      port.maximum = Math.max(port.target, profileNumber(maximum.value, port.target));
      maximum.value = port.maximum;
      markProfileDirty();
    });
    const remove = iconButton('trash-2', '删除端口', () => {
      deckProfileDraft.ports.splice(index, 1);
      for (const card of deckProfileDraft.cards) {
        card.ports = (card.ports || []).filter(value => value !== port.id);
        if (!card.ports.length && deckProfileDraft.ports.length) card.ports = [deckProfileDraft.ports[0].id];
      }
      markProfileDirty(); renderDeckProfileFields();
    }, deckProfileDraft.ports.length === 1 && deckProfileDraft.cards.length > 0);
    appendCells(row, [orderControls(deckProfileDraft.ports, index), name, target, maximum, remove]);
    body.append(row);
  });
}

function renderProfileCards() {
  const body = $('profile-cards');
  body.replaceChildren();
  deckProfileDraft.cards.forEach((card, index) => {
    const meta = catalogCard(card.id);
    const row = document.createElement('tr');
    const identity = createCardIdentity(meta);
    const cap = document.createElement('input');
    cap.type = 'number'; cap.min = '1'; cap.value = card.max_copies ?? 1; cap.disabled = deckProfileLocked;
    cap.addEventListener('change', () => { card.max_copies = Math.max(1, profileNumber(cap.value, 1)); markProfileDirty(); });
    const ports = document.createElement('div');
    ports.className = 'port-chips';
    for (const port of deckProfileDraft.ports) {
      const label = document.createElement('label');
      const input = document.createElement('input');
      input.type = 'checkbox'; input.checked = (card.ports || []).includes(port.id); input.disabled = deckProfileLocked;
      input.addEventListener('change', () => {
        const selected = new Set(card.ports || []);
        input.checked ? selected.add(port.id) : selected.delete(port.id);
        if (!selected.size) { input.checked = true; return; }
        card.ports = deckProfileDraft.ports.filter(row => selected.has(row.id)).map(row => row.id);
        markProfileDirty();
      });
      label.append(input, document.createTextNode(port.name || port.id));
      ports.append(label);
    }
    const remove = iconButton('x', '移出白名单', () => {
      deckProfileDraft.cards.splice(index, 1);
      markProfileDirty(); renderDeckProfileFields();
    });
    appendCells(row, [orderControls(deckProfileDraft.cards, index), identity, cap, ports, remove]);
    body.append(row);
  });
}

function renderCardLibrary() {
  const box = $('profile-card-library');
  const query = $('profile-card-search').value.trim().toLowerCase();
  box.replaceChildren();
  if (!query) return;
  const selected = new Set(deckProfileDraft.cards.map(card => card.id));
  const matches = cardCatalog.filter(card => !selected.has(card.id) &&
    [card.id, card.name_en, card.name_zh].some(value => String(value || '').toLowerCase().includes(query))).slice(0, 8);
  for (const card of matches) {
    const row = document.createElement('div');
    row.className = 'library-card';
    const label = createCardIdentity(card);
    const add = iconButton('plus', '加入白名单', () => {
      addCardToProfile(card.id);
    }, !deckProfileDraft.ports.length);
    row.append(label, add);
    box.append(row);
  }
  lucide.createIcons();
}

async function saveProfile(command) {
  if (busy || current.worker_running || current.preparing) return;
  if (command === 'save_deck_profile') {
    deckProfileDraft.id = $('profile-id').value.trim();
    deckProfileDraft.name = $('profile-name').value.trim();
  }
  busy = true;
  try {
    const response = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command, options:{profile:deckProfileDraft}})});
    const data = await response.json();
    if (!response.ok) throw Error(data.error);
    deckProfileDirty = false;
    renderStatus(data);
    renderDeckProfileEditor(data);
  } catch (error) {
    text('error', String(error)); $('error').hidden = false;
  } finally { busy = false; }
}

function renderDeckAudit() {
  const row = rows.find(value => String(value.sequence) === String(selected));
  const telemetry = row?.decision_telemetry || row || {};
  const scores = telemetry.offered_scores || [];
  const box = $('deck-audit');
  box.replaceChildren();
  if (!scores.length) { box.textContent = '本步骤没有抓牌审计'; box.className = 'muted'; return; }
  box.className = 'deck-audit';
  for (const score of scores) {
    const line = element('div', undefined, 'deck-audit-row');
    const status = score.eligible ? `候选 · 优先级 ${(score.priority_rank || []).join('.')}` : `拒绝 · ${score.rejection_reason || score.reason}`;
    line.append(element('strong', score.card_id), element('span', `${score.assigned_port_name || score.assigned_port || '未分配端口'} · ${status}`));
    box.append(line);
  }
}

const originalRenderStatus = renderStatus;
renderStatus = function(data) {
  originalRenderStatus(data);
  deckProfileLocked = !!data.worker_running || !!data.preparing || !!viewing || imported;
  renderDeckProfileEditor(data);
  for (const id of ['profile-id','profile-name','profile-card-search','profile-add-port','profile-save','profile-reset']) $(id).disabled = deckProfileLocked;
};
const originalRenderDetail = renderDetail;
renderDetail = function() { originalRenderDetail(); renderDeckAudit(); };

$('profile-card-search').addEventListener('input', renderCardLibrary);
$('profile-add-port').addEventListener('click', () => {
  const displayName = prompt('端口名称')?.trim();
  if (!displayName) return;
  const base = displayName.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '') || `port_${deckProfileDraft.ports.length + 1}`;
  let id = base;
  let suffix = 2;
  while (deckProfileDraft.ports.some(port => port.id === id)) id = `${base}_${suffix++}`;
  deckProfileDraft.ports.push({id, name: displayName, target: 1, maximum: 2});
  markProfileDirty(); renderDeckProfileFields();
});
$('profile-save').addEventListener('click', () => saveProfile('save_deck_profile'));
$('profile-reset').addEventListener('click', () => saveProfile('reset_deck_profile'));
$('card-preview-close').addEventListener('click', () => $('card-preview').close());
$('card-preview-level').querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
  previewUpgraded = button.dataset.upgraded === 'true';
  renderCardPreviewState();
}));
$('card-preview-add').addEventListener('click', () => {
  if (previewCard) addCardToProfile(previewCard.id);
  $('card-preview').close();
});
$('card-preview').addEventListener('click', event => {
  if (event.target === $('card-preview')) $('card-preview').close();
});
loadCardCatalog();

function setProfileCollapsed(collapsed) {
  $('profile-body').hidden = collapsed;
  $('profile-toggle').setAttribute('aria-expanded', String(!collapsed));
  $('profile-toggle').querySelector('svg, i')?.setAttribute('style', collapsed ? 'transform:rotate(-90deg)' : '');
  try { localStorage.setItem('deck-profile-collapsed', String(collapsed)); } catch {}
}
$('profile-toggle').addEventListener('click', () => setProfileCollapsed(!$('profile-body').hidden));
try { setProfileCollapsed(localStorage.getItem('deck-profile-collapsed') === 'true'); } catch {}
