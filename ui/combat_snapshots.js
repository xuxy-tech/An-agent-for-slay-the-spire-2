(() => {
  'use strict';
  const capture = document.getElementById('snapshot-capture');
  const refresh = document.getElementById('snapshot-refresh');
  const message = document.getElementById('snapshot-message');
  const list = document.getElementById('snapshot-list');
  const coverage = document.getElementById('snapshot-coverage');
  const consistencyStatus = document.getElementById('engine-consistency-status');
  const consistencyMessage = document.getElementById('engine-consistency-message');
  const consistencyRun = document.getElementById('engine-consistency-run');
  const labels = {RESTORE_VERIFIED: '独立恢复通过', RESERVE_VERIFIED: '独立恢复通过（备用）', CAPTURED: '已采集，未验证',
    VALIDATION_FAILED: '恢复验证失败', CAPTURE_FAILED: '采集失败',
    LEGACY_UNVERIFIED: '旧格式，未验证', STALE_REVALIDATION: '版本变化，待重验',
    CAPTURING: '采集中'};
  async function json(url, options) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }
  async function load() {
    const data = await json('/api/combat-snapshots');
    capture.hidden = !!data.history_only;
    if (coverage && data.validation_set) {
      const set = data.validation_set;
      coverage.textContent = `自动验证集：${set.valid} / ${set.target} 份独立恢复通过` +
        (set.acts ? ` · 第一幕 ${set.acts['1'] || 0}/24、第二幕 ${set.acts['2'] || 0}/16、第三幕 ${set.acts['3'] || 0}/8` : '') +
        (set.encounters != null ? ` · ${set.encounters} 种遭遇` : '') +
        (set.stale ? ` · ${set.stale} 份待重验` : '') +
        (set.reserve ? ` · ${set.reserve} 份有效备用样本不占名额` : '') +
        (set.valid >= set.target ? ' · 已停止采集，Agent 继续运行' : ' · 自动补齐中');
    }
    list.replaceChildren();
    for (const row of data.snapshots) {
      const tr = document.createElement('tr');
      const cells = [new Date(row.created_at_utc * 1000).toLocaleString(),
        `${row.floor ?? '—'} / ${row.turn ?? '—'}`, labels[row.status] || row.status,
        `${row.artifact_dir}${row.error ? '\n' + row.error : ''}`];
      for (const value of cells) {
        const td = document.createElement('td');
        td.textContent = value;
        td.style.whiteSpace = 'pre-wrap';
        td.style.overflowWrap = 'anywhere';
        tr.appendChild(td);
      }
      list.appendChild(tr);
    }
    if (!data.snapshots.length) message.textContent = '尚无快照。';
  }
  async function loadConsistency() {
    if (!consistencyStatus) return;
    try {
      const data = await json('/api/engine-consistency');
      const passed = data.status === 'PASS' && data.current_version_allowed;
      consistencyStatus.textContent = passed
        ? `当前版本检验通过：${data.passed?.length || 0} 份快照；仅后端对局可用。`
        : data.status === 'PASS' ? '此前检验通过，但快照与当前引擎版本不匹配；请重采并重新检验。'
        : `检验${data.status === 'NOT_RUN' ? '尚未运行' : '未通过'}；仅后端对局已锁定。`;
      consistencyStatus.className = passed ? 'success' : 'muted';
    } catch (error) { consistencyStatus.textContent = `一致性状态读取失败：${error.message}`; }
  }
  async function runConsistency() {
    if (!consistencyRun) return;
    consistencyRun.disabled = true;
    if (consistencyMessage) consistencyMessage.textContent = '正在重新验证当前快照…';
    try {
      const data = await json('/api/engine-consistency', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      if (consistencyMessage) consistencyMessage.textContent = data.status === 'PASS' ? '检验完成。' : '检验失败，差异已保存。';
      await loadConsistency();
    } catch (error) { if (consistencyMessage) consistencyMessage.textContent = error.message; }
    finally { consistencyRun.disabled = false; }
  }
  refresh.addEventListener('click', () => load().catch(e => { message.textContent = e.message; }));
  consistencyRun?.addEventListener('click', runConsistency);
  loadConsistency();
  document.getElementById('combat-snapshots-panel').addEventListener('toggle', e => {
    if (e.target.open) load().catch(error => { message.textContent = error.message; });
  });
  capture.addEventListener('click', async () => {
    capture.disabled = true;
    capture.dataset.busy = "true";
    message.textContent = '正在采集并验证，请保持游戏停在当前决策点…';
    try {
      const status = await json('/api/status');
      if (status.history_only) throw new Error('历史查看模式不能采集当前战斗。');
      const sessions = document.getElementById('sessions');
      if (sessions && sessions.value) throw new Error('请先返回当前运行；采集对象是当前客户端。');
      const data = await json('/api/command', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({command: 'capture_combat_snapshot', options: {}})});
      await load();
      message.textContent = `${labels[data.snapshot.status] || data.snapshot.status}：${data.snapshot.artifact_dir}`;
    } catch (error) {
      message.textContent = error.message;
    } finally {
      delete capture.dataset.busy;
      capture.disabled = false;
    }
  });
})();
