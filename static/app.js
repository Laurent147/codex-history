'use strict';
const $ = id => document.getElementById(id);
const filters = ['q', 'directory', 'status', 'date_field', 'after', 'before', 'fulltext', 'subagents'];
let sessions = [], selected = new Set(), csrf = '', controller, generation = 0;
let sortKey = 'updated_at', sortDirection = -1, deleting = false, loading = false, pendingIds = [];
const date = value => value ? new Date(value * 1000).toLocaleString([], {year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'}) : '—';
const title = row => row.title || row.first_user_message || 'Untitled conversation';
const tokens = value => value == null ? 'Unavailable' : value.toLocaleString();
const compact = value => new Intl.NumberFormat(undefined, {notation:'compact',maximumFractionDigits:1}).format(value);
const money = value => value == null ? 'Unavailable' : new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:3}).format(value);
function notice(text, error=false) { $('notice').textContent = text; $('notice').hidden = !text; $('notice').classList.toggle('error', error); }
async function api(path, options) {
  const response = await fetch(path, options), data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function selectionUI() {
  $('selected').textContent = selected.size ? `${selected.size} selected` : '';
  $('delete').textContent = selected.size ? `Delete selected (${selected.size})` : 'Delete selected';
  $('delete').disabled = !selected.size || deleting || loading;
  $('clear-selection').disabled = !selected.size || deleting || loading;
  $('select-all').disabled = !sessions.length || deleting || loading;
  $('master').checked = sessions.length > 0 && selected.size === sessions.length;
  $('master').indeterminate = selected.size > 0 && selected.size < sessions.length;
  $('master').disabled = deleting || loading || !sessions.length;
}
function render() {
  sessions.sort((a,b) => {
    if(a[sortKey] == null || b[sortKey] == null) return a[sortKey] == null ? (b[sortKey] == null ? 0 : 1) : -1;
    return (typeof a[sortKey] === 'number' ? a[sortKey] - b[sortKey] : String(a[sortKey] || '').localeCompare(String(b[sortKey] || ''))) * sortDirection;
  });
  $('rows').replaceChildren();
  for (const row of sessions) {
    const tr = $('row-template').content.firstElementChild.cloneNode(true);
    tr.classList.toggle('chosen', selected.has(row.id));
    const checkbox = tr.querySelector('.row-select');
    checkbox.checked = selected.has(row.id); checkbox.disabled = deleting || loading;
    checkbox.setAttribute('aria-label', `Select ${title(row)}`);
    checkbox.onchange = () => { checkbox.checked ? selected.add(row.id) : selected.delete(row.id); tr.classList.toggle('chosen', checkbox.checked); selectionUI(); };
    tr.querySelector('.open-title').textContent = title(row);
    tr.querySelector('.first-message').textContent = row.first_user_message || '';
    tr.querySelector('.session-id').textContent = row.id;
    tr.querySelector('.badge').hidden = !row.archived;
    tr.querySelector('.cwd').textContent = row.cwd || '—';
    tr.querySelector('.branch').textContent = [row.git_branch, row.model, row.agent_role].filter(Boolean).join(' · ');
    tr.querySelector('.updated').textContent = date(row.updated_at);
    tr.querySelector('.created').textContent = date(row.created_at);
    const usage = row.usage;
    tr.querySelector('.token-total').textContent = tokens(row.total_tokens);
    tr.querySelector('.token-breakdown').textContent = usage.input_tokens == null ? 'Breakdown unavailable' : `${compact(usage.input_tokens)} in · ${compact(usage.output_tokens)} out`;
    tr.querySelector('.token-total').title = usage.input_tokens == null ? usage.note : `Input: ${tokens(usage.input_tokens)}\nCached input (included): ${tokens(usage.cached_input_tokens)}\nOutput: ${tokens(usage.output_tokens)}\nReasoning (included in output): ${tokens(usage.reasoning_output_tokens)}\nSource: ${usage.source}\nSidebar database total: ${tokens(usage.database_total_tokens)}\n${usage.note}`;
    tr.querySelector('.cost').textContent = row.cost_usd == null ? 'Unavailable' : `≈ ${money(row.cost_usd)}`;
    tr.querySelector('.cost-note').textContent = row.cost_usd == null ? usage.note : (usage.long_context_requests ? `${usage.long_context_requests} long-context requests` : usage.base_rates_only ? 'USD · base rates only' : 'USD · standard rates');
    tr.querySelector('.cost').title = [usage.source, ...usage.models.map(m => `${m.model}: ${money(m.cost_usd)}`), usage.note].filter(Boolean).join('\n');
    tr.querySelector('.open-title').onclick = tr.querySelector('.view').onclick = () => preview(row);
    tr.querySelector('.resume').onclick = async () => {
      try { await navigator.clipboard.writeText(`codex resume ${row.id}`); notice('Resume command copied. Paste it in your terminal.'); }
      catch { notice(`Copy this command: codex resume ${row.id}`); }
    };
    tr.querySelector('.delete-one').disabled = deleting || loading;
    tr.querySelector('.delete-one').onclick = () => confirmDelete([row.id]);
    $('rows').append(tr);
  }
  $('empty').hidden = sessions.length > 0;
  selectionUI();
}
async function load() {
  if (deleting) return;
  controller?.abort(); controller = new AbortController(); const current = ++generation;
  selected.clear(); loading = true; render(); $('count').textContent = 'Loading…';
  $('total-tokens').textContent = $('total-cost').textContent = '…';
  $('token-coverage').textContent = $('cost-coverage').textContent = 'Loading usage…';
  const params = new URLSearchParams();
  for (const id of filters) { const el = $(id); params.set(id, el.type === 'checkbox' ? (el.checked ? '1' : '0') : el.value); }
  // Date-only values are interpreted on the server, which runs on this same machine.
  try {
    const data = await api(`/api/sessions?${params}`, {signal:controller.signal});
    if (current !== generation) return;
    sessions = data.sessions;
    const directory = $('directory').value;
    $('directory').replaceChildren(new Option('All directories',''), ...data.directories.map(d => new Option(d,d)));
    $('directory').value = directory;
    $('count').textContent = `${sessions.length} matching / ${data.total} total`;
    $('home').textContent = data.home;
    const summary = data.summary;
    $('total-tokens').textContent = summary.sessions && !summary.tokens_available ? 'Unavailable' : tokens(summary.total_tokens);
    $('token-coverage').textContent = summary.tokens_available === summary.sessions ? `${summary.sessions} matching conversations` : `${summary.tokens_available} of ${summary.sessions} chats have recorded usage`;
    $('total-cost').textContent = summary.sessions && !summary.cost_available ? 'Unavailable' : `≈ ${money(summary.cost_usd)}`;
    const complete = summary.cost_available === summary.sessions;
    $('cost-label').textContent = complete ? 'Hypothetical API cost · USD' : 'Known cost subtotal · USD · incomplete';
    const unpriced = summary.total_tokens ? Math.round(100 * (summary.total_tokens - summary.priced_tokens) / summary.total_tokens) : null;
    $('cost-coverage').textContent = complete ? `${summary.sessions} of ${summary.sessions} chats priced · not your subscription bill` : `${summary.cost_available} of ${summary.sessions} chats priced${unpriced == null ? '' : ` · ${unpriced}% of recorded tokens unpriced`}`;
    $('pricing-date').textContent = data.pricing.checked_at;
    notice(data.warnings.join('\n'));
  } catch(error) {
    if (error.name === 'AbortError' || current !== generation) return;
    sessions = []; $('count').textContent = 'Could not load history'; notice(error.message, true);
    $('total-tokens').textContent = $('total-cost').textContent = '—';
    $('token-coverage').textContent = $('cost-coverage').textContent = 'Usage could not be loaded';
  } finally { if (current === generation) { loading = false; render(); } }
}
let timer;
for (const id of filters) $(id).addEventListener(id === 'q' ? 'input' : 'change', () => {
  clearTimeout(timer);
  // Immediately discard selection; an old response must not re-enable deletion during debounce.
  controller?.abort(); generation++; selected.clear(); loading = true; render();
  timer = setTimeout(load, id === 'q' ? 250 : 0);
});
$('refresh').onclick = () => { clearTimeout(timer); load(); };
$('reset').onclick = () => { for (const id of filters) { const el=$(id); if(el.type==='checkbox') el.checked=false; else el.value = id==='status' ? 'active' : id==='date_field' ? 'updated' : ''; } clearTimeout(timer); load(); };
$('select-all').onclick = () => { selected = new Set(sessions.map(r => r.id)); render(); };
$('clear-selection').onclick = () => { selected.clear(); render(); };
$('master').onchange = () => { selected = $('master').checked ? new Set(sessions.map(r=>r.id)) : new Set(); render(); };
for (const button of document.querySelectorAll('.sort')) button.onclick = () => {
  sortDirection = sortKey === button.dataset.sort ? -sortDirection : 1; sortKey = button.dataset.sort;
  for(const b of document.querySelectorAll('.sort')) b.textContent = ({title:'Conversation',cwd:'Directory',updated_at:'Updated',created_at:'Created',total_tokens:'Tokens',cost_usd:'Est. API cost'})[b.dataset.sort] + (b.dataset.sort===sortKey ? (sortDirection===1?' ↑':' ↓'):'');
  render();
};
let previewGeneration = 0;
async function preview(row) {
  const current = ++previewGeneration;
  $('preview-title').textContent = title(row); $('preview-id').textContent = row.id;
  $('messages').textContent = 'Loading conversation…'; $('preview').showModal();
  try {
    const data = await api(`/api/session/${row.id}`);
    if(current !== previewGeneration) return;
    $('messages').replaceChildren();
    const query = $('q').value.trim();
    for (const m of data.messages) {
      const article = document.createElement('article'), heading = document.createElement('h3'), body=document.createElement('pre');
      article.className = m.role; heading.textContent = m.role === 'user' ? 'You' : 'Codex';
      // Plain text rendering keeps transcripts inert, including HTML and tool content.
      if(query) {
        const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const expression = new RegExp(escaped, 'gi'); let start=0;
        for(const match of m.text.matchAll(expression)) { body.append(document.createTextNode(m.text.slice(start,match.index))); const mark=document.createElement('mark'); mark.textContent=match[0]; body.append(mark); start=match.index+match[0].length; }
        body.append(document.createTextNode(m.text.slice(start)));
      } else body.textContent = m.text;
      article.append(heading,body); $('messages').append(article);
    }
    if(!data.messages.length) $('messages').textContent = 'No readable conversation messages found. This session may contain only tool events or its rollout may be unavailable.';
    if(data.truncated) { const p=document.createElement('p'); p.textContent='Preview limited to the first 1,000 messages or 2 MB. Full conversation search still scans all messages.'; $('messages').append(p); }
  } catch(error) { if(current === previewGeneration) $('messages').textContent = error.message; }
}
$('close-preview').onclick = () => { previewGeneration++; $('preview').close(); };
function confirmDelete(ids) {
  if(deleting || loading) return;
  if(ids.length > 500) { notice('Select up to 500 conversations at a time.',true); return; }
  pendingIds = [...ids];
  $('confirm-title').textContent = `Delete ${ids.length} conversation${ids.length===1?'':'s'}?`;
  $('delete-list').replaceChildren();
  for (const row of sessions.filter(r=>ids.includes(r.id))) { const li=document.createElement('li'); li.textContent=`${title(row)} · ${row.cwd}`; $('delete-list').append(li); }
  $('delete-summary').textContent = 'Only the conversations listed above will be deleted. This cannot be undone.';
  $('confirmation').showModal();
}
$('delete').onclick = () => confirmDelete([...selected]);
$('cancel-delete').onclick = () => $('confirmation').close();
$('confirm-delete').onclick = async () => {
  const ids = [...pendingIds]; $('confirmation').close(); deleting = true; document.body.classList.add('busy'); render();
  notice(`Deleting ${ids.length} conversation${ids.length===1?'':'s'}… Keep this tab open.`);
  let summary='', failed=false;
  try {
    csrf ||= (await api('/api/token')).token;
    const data = await api('/api/delete', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify({ids})});
    const errors=data.results.filter(r=>!r.ok), count=data.results.length-errors.length;
    summary=`Deleted ${count} conversation${count===1?'':'s'}.`;
    if(errors.length) { failed=true; summary+=`\n${errors.length} failed:\n`+errors.map(r=>`${r.id}: ${r.error}`).join('\n'); }
  } catch(error) { failed=true; summary=`${error.message}\nRefresh to verify which conversations remain before retrying.`; }
  finally { deleting=false; document.body.classList.remove('busy'); await load(); notice(summary,failed); }
};
window.addEventListener('beforeunload', event => { if(deleting) { event.preventDefault(); event.returnValue=''; } });
load();
