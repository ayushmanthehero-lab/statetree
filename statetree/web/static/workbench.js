'use strict';
// No HTML injection, third-party scripts, token storage or provider-side calls
// from this browser. The access token lives only in this page's memory.
let token = '';
let project = null;
let historyCursor = null;
let branchState = {items: [], canonical: null};
let archivePage = null;
let busy = false;
const $ = id => document.getElementById(id);
const short = value => value ? value.slice(0, 10) : 'Not promoted';
const pretty = value => JSON.stringify(value, null, 2);
const node = (tag, text, className) => {
  const el = document.createElement(tag);
  if (text !== undefined && text !== null) el.textContent = text;
  if (className) el.className = className;
  return el;
};
const button = (label, action, className = '') => {
  const el = node('button', label, className);
  el.type = 'button';
  el.addEventListener('click', () => run(action));
  return el;
};
function notice(message, error = false) {
  $('notice').hidden = !message;
  $('notice').className = error ? 'notice error' : 'notice';
  $('notice').textContent = message;
}
async function api(path, body) {
  const key = token;
  if (!key) throw new Error('The workbench is locked.');
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: {'Authorization': `Bearer ${key}`, 'Content-Type': 'application/json'},
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'omit', cache: 'no-store', redirect: 'error'
  });
  const value = await response.json();
  if (key !== token) throw new Error('The tab was locked while the operation was running.');
  if (!response.ok) throw new Error(value.error || `Request failed (${response.status}).`);
  return value;
}
async function mutate(path, body = {}) {
  if (!project) throw new Error('Refresh the project before changing state.');
  const result = await api(path, {...body, expected_head: project.head});
  await refresh();
  return result;
}
async function run(action) {
  if (busy) return;
  busy = true;
  document.querySelectorAll('button').forEach(el => { if (el.id !== 'lock') el.disabled = true; });
  try {
    notice('');
    await action();
  } catch (error) {
    notice(error.message, true);
    if (token) {
      try { await refresh(); } catch (_) { /* The original error stays visible. */ }
    }
  } finally {
    busy = false;
    document.querySelectorAll('button').forEach(el => { el.disabled = false; });
  }
}
function form(id, action) {
  $(id).addEventListener('submit', event => {event.preventDefault(); run(action);});
}
function showTab(name) {
  document.querySelectorAll('[data-tab]').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(el => {el.hidden = el.id !== `tab-${name}`;});
}
document.querySelectorAll('[data-tab]').forEach(el => el.addEventListener('click', () => showTab(el.dataset.tab)));
function renderStatus(value) {
  project = value;
  $('goal').textContent = value.goal;
  $('repo-name').textContent = value.repo.split(/[\\/]/).filter(Boolean).pop();
  $('model').textContent = value.model.model_id;
  $('head-label').textContent = `Head ${short(value.head)}`;
  $('metric-facts').textContent = value.active_facts.toLocaleString();
  $('metric-requests').textContent = value.usage.requests.toLocaleString();
  $('metric-tokens').textContent = value.usage.total_tokens.toLocaleString();
  $('metric-canonical').textContent = short(value.canonical_head);
  const incomplete = value.usage.unknown_usage_requests + value.usage.ambiguous_usage_requests;
  $('usage-caveat').textContent = incomplete ? `${incomplete} requests have incomplete/ambiguous usage` :
    (value.usage.requests ? 'Observed usage; persists across restores' : 'No model requests recorded');
  $('config-output').textContent = pretty(value.config);
  $('connection-status').textContent = 'Connected · local';
}
function renderHistory(value, append = false) {
  const container = $('history-list');
  if (!append) container.replaceChildren();
  value.items.forEach(item => {
    const card = node('article', null, 'checkpoint');
    const heading = node('div', null, 'row spread');
    heading.append(node('span', short(item.id), 'hash'), node('span', item.verification_status, 'badge'));
    card.append(heading, node('h3', item.note?.summary || item.current_subgoal || 'Project checkpoint'));
    const date = new Date(item.created_at);
    card.append(node('p', Number.isNaN(date.getTime()) ? item.created_at : date.toLocaleString(), 'meta'));
    const actions = node('div', null, 'actions');
    actions.append(button('Restore checkpoint', async () => {
      if (!window.confirm('Restore captured files and public state to this checkpoint? Unrelated untracked files are preserved. Recorded usage is never rolled back.')) return;
      await mutate('/api/restore', {checkpoint_id: item.id, confirm: true});
      notice(`Restored checkpoint ${short(item.id)}. Recorded usage is unchanged.`);
    }, 'danger'));
    (item.note?.evidence_ids || []).slice(0, 2).forEach((id, index) => actions.append(button(`Evidence ${index + 1}`, async () => {
      $('archive-id').value = id;
      await readArchive(id, 0);
    })));
    card.append(actions);
    container.append(card);
  });
  historyCursor = value.next_cursor;
  $('more-history').hidden = !historyCursor;
}
function renderFacts(value) {
  const container = $('facts-list');
  container.replaceChildren();
  const entries = Object.entries(value.active);
  if (!entries.length) container.append(node('div', 'No active facts. Record an observation with its evidence reference.', 'empty'));
  entries.forEach(([key, record]) => {
    const card = node('article', null, 'fact-card');
    card.append(node('h3', key), node('pre', pretty(record.value), 'output'));
    card.append(node('p', `Version ${record.version ?? '—'} · Evidence: ${(record.evidence || []).join(', ')}`, 'fine'));
    const actions = node('div', null, 'actions');
    ['archive', 'invalidate'].forEach(action => actions.append(button(action[0].toUpperCase() + action.slice(1), async () => {
      await mutate('/api/forget', {key, action});
      notice(`${key}: ${action} recorded in a new checkpoint. Historical evidence is retained.`);
    })));
    card.append(actions);
    container.append(card);
  });
}
function renderBranches(value) {
  branchState = value;
  const canonical = $('canonical-branch');
  canonical.replaceChildren(node('h3', 'Branch canonical revision'));
  if (value.canonical) {
    canonical.append(node('p', value.canonical.id, 'hash'));
    if (value.canonical.source_candidate) canonical.append(button('Adopt verified integration into main', async () => {
      if (!window.confirm('Adopt the current verified integration into main? This changes captured files. Divergent main state or files will be rejected.')) return;
      await mutate('/api/adopt', {revision_id: value.canonical.id, confirm: true});
      notice('Verified integration adopted into a new main checkpoint.');
    }));
    else canonical.append(node('p', 'Baseline only; no candidate has been integrated yet.', 'fine'));
  } else canonical.append(node('p', 'Create a branch to establish a baseline.', 'muted'));
  const container = $('branches-list');
  container.replaceChildren();
  if (!value.items.length) container.append(node('div', 'No branches yet. Create an isolated worktree to explore a change.', 'empty'));
  value.items.forEach(item => {
    const card = node('article', null, 'branch-card');
    card.append(node('h3', item.name || item.key), node('p', item.path, 'hash'));
    card.append(node('p', `Candidate: ${item.candidate_id ? short(item.candidate_id) : 'Not completed'}`, 'fine'));
    const actions = node('div', null, 'actions');
    // Namespaced speculative branches are retained evidence; the coordinator,
    // not this named-branch form, owns their lifecycle.
    if (!item.key.includes(':')) actions.append(button('Complete current worktree', async () => {
      const candidate = await mutate('/api/complete', {name: item.name, expected_candidate: item.candidate_id});
      notice(`Candidate ${short(candidate.id)} captured. It is not canonical until independently verified.`);
    }));
    if (item.candidate_id) actions.append(button('Verify & integrate candidate', async () => {
      const result = await mutate('/api/merge', {candidate_id: item.candidate_id, expected_parent: branchState.canonical.id});
      notice(`Integrated revision ${short(result.id)}. Main files have not been changed; adoption is a separate action.`);
    }));
    card.append(actions);
    container.append(card);
  });
}
async function refresh() {
  const [status, history, facts, branches] = await Promise.all([
    api('/api/status'), api('/api/history'), api('/api/facts'), api('/api/branches')
  ]);
  renderStatus(status); renderHistory(history); renderFacts(facts); renderBranches(branches);
}
async function readArchive(id, offset) {
  archivePage = await api(`/api/archive?id=${encodeURIComponent(id)}&offset=${offset}&limit=3000`);
  $('archive-output').hidden = false;
  $('archive-output').textContent = archivePage.content;
  $('archive-next').hidden = archivePage.next_offset === null;
  showTab('history');
}
form('auth-form', async () => {
  token = $('token').value.trim();
  $('auth-error').textContent = '';
  try {
    await refresh();
    $('token').value = '';
    $('auth-screen').hidden = true;
    $('app').hidden = false;
  } catch (error) {
    token = ''; $('auth-error').textContent = error.message;
  }
});
$('lock').addEventListener('click', () => {
  token = ''; project = null; $('app').hidden = true; $('auth-screen').hidden = false;
  $('connection-status').textContent = 'Locked'; $('token').value = '';
  // Do not keep evidence/chat visible in the DOM after locking.
  ['history-list', 'facts-list', 'branches-list', 'recall-results', 'archive-output', 'context-output', 'chat-log', 'config-output'].forEach(id => $(id).replaceChildren());
  notice('');
});
$('refresh').addEventListener('click', () => run(async () => {await refresh(); notice('Project reloaded from durable local storage.');}));
$('export').addEventListener('click', () => run(async () => {
  const bundle = await api('/api/export');
  const url = URL.createObjectURL(new Blob([pretty(bundle)], {type:'application/json'}));
  const link = node('a'); link.href = url; link.download = 'statetree-state.json';
  document.body.append(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  notice('Public state bundle exported. Protect the evidence it contains.');
}));
$('more-history').addEventListener('click', () => run(async () => {
  renderHistory(await api(`/api/history?cursor=${encodeURIComponent(historyCursor)}`), true);
}));
form('checkpoint-form', async () => {
  const result = await mutate('/api/checkpoint', {message: $('checkpoint-note').value, verify: $('checkpoint-verify').checked});
  $('checkpoint-note').value = ''; notice(`Checkpoint ${short(result.id)} saved (${result.verification_status}).`);
});
form('archive-form', () => readArchive($('archive-id').value.trim(), 0));
$('archive-next').addEventListener('click', () => run(() => readArchive(archivePage.archive_id, archivePage.next_offset)));
form('fact-form', async () => {
  await mutate('/api/remember', {key:$('fact-key').value.trim(), value:JSON.parse($('fact-value').value),
    evidence:$('fact-evidence').value.split('\n').map(s => s.trim()).filter(Boolean),
    dependencies:JSON.parse($('fact-dependencies').value || '{}')});
  notice('Fact recorded; stale dependent facts are excluded from active memory.');
});
form('recall-form', async () => {
  const result = await api(`/api/recall?query=${encodeURIComponent($('recall-query').value)}`);
  const container = $('recall-results'); container.replaceChildren();
  container.append(node('p', `${result.notes.length} relevant notes · ${result.estimated_count} estimated bytes, not provider usage`, 'fine'));
  result.notes.forEach(note => {const card=node('article',null,'checkpoint'); card.append(node('span', short(note.commit_id),'hash'),node('p', note.summary)); container.append(card);});
});
form('fork-form', async () => {
  const result = await mutate('/api/fork', {name: $('branch-name').value.trim()});
  $('branch-name').value = ''; notice(`Isolated worktree created at ${result.path}. Edit files there, not in main.`);
});
form('context-form', async () => {
  const result = await api('/api/context', {query:$('context-query').value, new_task:$('context-new').checked});
  $('context-summary').hidden = false;
  $('context-summary').textContent = `${result.estimated_input_tokens.toLocaleString()} estimated UTF-8 bytes · ${result.selected_commit_ids.length} recalled commits · ${result.dropped_messages} messages dropped · ${result.masked_observations} old observations archived. No model call.`;
  $('context-output').hidden = false; $('context-output').textContent = pretty(result);
});
$('compact').addEventListener('click', () => run(async () => {
  const result = await mutate('/api/compact'); notice(`Conversation compacted. Original evidence: ${result.archive_id}`);
}));
form('chat-form', async () => {
  const prompt = $('chat-prompt').value;
  const user = node('div', null, 'chat-message user'); user.append(node('span', 'YOU', 'eyebrow'), node('div', prompt));
  $('chat-log').append(user);
  const result = await mutate('/api/ask', {prompt, new_task: $('chat-new').checked});
  const answer = node('div',null,'chat-message assistant');
  answer.append(node('span','LOCAL MODEL · UNVERIFIED REPLY','eyebrow'),node('div',result.answer));
  $('chat-log').append(answer); $('chat-log').scrollTop = $('chat-log').scrollHeight;
  $('chat-prompt').value = '';
  notice(`${result.provider_requests_this_turn} provider request(s) observed this turn. Usage is retained even if you restore an earlier checkpoint.`);
});
form('import-form', async () => {
  const file = $('bundle-file').files[0];
  if (!file || file.size > 15 * 1024 * 1024) throw new Error('Select a JSON bundle smaller than 15 MiB for browser import. Larger supported files can use the CLI.');
  if (!window.confirm('Import this trusted public-state bundle into a new checkpoint? Workspace files will not be transplanted.')) return;
  await mutate('/api/import', {bundle:JSON.parse(await file.text())}); notice('Validated public state imported into a new checkpoint.');
});
