// Storage work starts only when the panel is opened. No disk scan on page load.
const storagePanel = document.getElementById('storage-panel');
const storageSummary = document.getElementById('storage-summary');
const storageArchives = document.getElementById('storage-archives');
let storageBusy = false;

const storageSize = bytes => bytes >= 1048576
  ? `${(bytes / 1048576).toFixed(1)} MiB`
  : `${(bytes / 1024).toFixed(1)} KiB`;

async function storagePost(path, payload = {}) {
  const response = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw Error(result.error || '操作失败');
  return result;
}

async function refreshStorage() {
  if (!storagePanel.open) return;
  storageSummary.textContent = '正在统计存储用量…';
  try {
    const response = await fetch('/api/storage');
    const data = await response.json();
    if (!response.ok) throw Error(data.error || '读取存储信息失败');
    storageSummary.textContent = `未归档 ${data.active_sessions} 局（${storageSize(data.active_session_bytes)}） · 已归档 ${data.archives.length} 局（${storageSize(data.archive_bytes)}） · 战斗快照 ${data.snapshots} 份（${storageSize(data.snapshot_bytes)}），其中过期 ${data.stale_snapshots} 份（${storageSize(data.stale_snapshot_bytes)}） · 回放缓存 ${data.replay_cache_files} 份（${storageSize(data.replay_cache_bytes)}）`;
    storageArchives.replaceChildren();
    for (const archive of data.archives) {
      const row = document.createElement('div');
      row.className = 'storage-archive-row';
      const label = document.createElement('span');
      label.textContent = `${archive.id.split('/').at(-1)} · ${storageSize(archive.bytes)}`;
      const view = document.createElement('button');
      view.type = 'button'; view.textContent = '查看';
      view.onclick = () => { const select = document.getElementById('sessions');
        let option = [...select.options].find(item => item.value === archive.id);
        if (!option) { option = new Option(label.textContent, archive.id); select.add(option); }
        select.value = archive.id; loadSession(archive.id); window.scrollTo({top: 0, behavior: 'smooth'}); };
      const remove = document.createElement('button');
      remove.type = 'button'; remove.textContent = '永久删除';
      remove.onclick = async () => {
        if (storageBusy || !confirm(`永久删除归档 ${archive.id.split('/').at(-1)}？完整报告和日志无法恢复；独立快照保留。`)) return;
        storageBusy = true;
        try { await storagePost('/api/storage/delete', {id: archive.id});
          if (viewing === archive.id) { document.getElementById('sessions').value = ''; await loadSession(''); }
          [...document.getElementById('sessions').options].find(item => item.value === archive.id)?.remove();
          await Promise.all([refreshStorage(), loadHistory(true), loadHistorySummary()]);
        } catch (error) { storageSummary.textContent = String(error); }
        finally { storageBusy = false; }
      };
      row.append(label, view, remove); storageArchives.append(row);
    }
    if (!data.archives.length) storageArchives.textContent = '暂无归档';
  } catch (error) { storageSummary.textContent = String(error); }
}

storagePanel.addEventListener('toggle', () => { if (storagePanel.open) refreshStorage(); });
document.getElementById('storage-refresh').addEventListener('click', refreshStorage);
document.getElementById('storage-clear-stale').addEventListener('click', async () => {
  if (storageBusy || !confirm('删除与当前引擎不兼容的过期快照？这些快照无法再用于当前 A/B 回放，删除后不可恢复。')) return;
  storageBusy = true;
  try { const result = await storagePost('/api/storage/stale-snapshots/delete');
    await refreshStorage(); storageSummary.textContent += ` · 本次删除 ${result.removed} 份`; }
  catch (error) { storageSummary.textContent = String(error); }
  finally { storageBusy = false; }
});
document.getElementById('storage-clear-cache').addEventListener('click', async () => {
  if (storageBusy || !confirm('清空 A/B 回放缓存？之后需要重新计算，已保存的对比结果保留。')) return;
  storageBusy = true;
  try { const result = await storagePost('/api/storage/replay-cache/clear');
    await refreshStorage(); storageSummary.textContent += ` · 本次清理 ${result.removed} 份`; }
  catch (error) { storageSummary.textContent = String(error); }
  finally { storageBusy = false; }
});

const renderHistoryWithoutStorage = renderHistory;
renderHistory = function () {
  renderHistoryWithoutStorage();
  const displayed = document.querySelectorAll('#run-history tr');
  historyRows.forEach((row, index) => {
    const cell = displayed[index]?.lastElementChild;
    if (!cell) return;
    const remove = cell.querySelector('.history-delete');
    if (row.archived) {
      remove.title = '永久删除此归档';
      return;
    }
    const archive = document.createElement('button');
    archive.type = 'button'; archive.className = 'history-archive';
    archive.title = '压缩归档并保留完整历史';
    archive.setAttribute('aria-label', `归档 ${new Date(row.created_at * 1000).toLocaleString()} 的对局`);
    archive.innerHTML = '<i data-lucide="archive"></i>';
    archive.disabled = busy || row.id === current.session_id;
    archive.onclick = async event => {
      event.stopPropagation();
      if (busy || !confirm('归档此局？完整历史仍可查看，关联快照的战斗证据会先独立保存。')) return;
      busy = true;
      try { const result = await storagePost('/api/storage/archive', {id: row.id});
        if (viewing === row.id) { const id = `live_dashboard/archive/${row.id.split('/').at(-1)}`;
          const select = document.getElementById('sessions');
          const option = [...select.options].find(item => item.value === row.id);
          if (option) { option.value = id; option.textContent += ' · 已归档'; select.value = id; }
          await loadSession(id); }
        await Promise.all([loadHistory(true), loadHistorySummary(), refreshStorage()]);
        if (result.incomplete_snapshot_evidence) storageSummary.textContent =
          `${result.incomplete_snapshot_evidence} 份旧快照缺少独立证据，已保留压缩归档作为核验来源；请勿删除该归档。`;
      } catch (error) { text('error', String(error)); document.getElementById('error').hidden = false; }
      finally { busy = false; renderStatus(current); }
    };
    cell.prepend(archive);
  });
  lucide.createIcons();
};

deleteHistory = async function (row) {
  if (busy || row.id === current.session_id) return;
  const when = new Date(row.created_at * 1000).toLocaleString();
  const message = row.archived
    ? `永久删除 ${when} 的归档？完整报告与日志无法恢复；独立快照保留。`
    : `永久删除 ${when} 的完整对局记录？关联快照会先封存战斗证据；快照本身保留。`;
  if (!confirm(message)) return;
  busy = true;
  try {
    await storagePost(row.archived ? '/api/storage/delete' : '/api/sessions/delete', {id: row.id});
    if (viewing === row.id) { document.getElementById('sessions').value = ''; await loadSession(''); }
    [...document.getElementById('sessions').options].find(item => item.value === row.id)?.remove();
    await Promise.all([loadHistory(true), loadHistorySummary(), refreshStorage()]);
  } catch (error) { text('error', String(error)); document.getElementById('error').hidden = false; }
  finally { busy = false; renderStatus(current); }
};
