'use strict';
// Uses workbench's authenticated API helper. No token or transcript is stored in browser storage.
let selectedAgentTask = null;
let agentEventCursor = 0;
let agentPolling = false;
let agentVisibleEvents = 0;
let agentTaskStatus = null;
let agentHeaderCheckpoint = null;

function resetAgentView(id) {
  selectedAgentTask = id;
  agentEventCursor = 0;
  agentHeaderCheckpoint = null;
  agentTaskStatus = null;
  agentVisibleEvents = 0;
  $('agent-events').replaceChildren();
  $('agent-note').textContent = '';
  $('agent-final').textContent = '';
  $('agent-final').hidden = true;
  $('agent-error').textContent = '';
  $('agent-operation').value = ''; $('agent-resolution').value = ''; $('agent-confirm').checked = false;
  $('agent-review').hidden = true;
}
function renderAgentTask(task) {
  agentTaskStatus = task.status;
  $('agent-title').textContent = task.prompt.slice(0, 140);
  $('agent-status').textContent = task.status;
  $('agent-stats').textContent = `${task.steps} saved tool receipts · ${task.model_calls} model request preparations · checkpoint ${short(task.checkpoint_id)} · native execution ${task.allow_execution ? 'approved' : 'not approved'}`;
  $('agent-note').textContent = pretty(task.note);
  $('agent-error').textContent = task.error || '';
  $('agent-final').hidden = !task.final;
  $('agent-final').textContent = task.final || '';
  $('agent-resume').disabled = task.active || task.status === 'completed';
  $('agent-pause').disabled = !task.active || task.pause_requested;
  $('agent-review').hidden = task.status !== 'needs_review';
}
function addAgentEvent(event) {
  const card = node('article', null, 'checkpoint');
  const value = event.value || {};
  let label = event.kind.replaceAll('_', ' ');
  if (value.tool) label += ` · ${value.tool}`;
  if (event.kind === 'checkpoint') label += ` · ${short(value.id)}`;
  card.append(node('b', label), node('div', new Date(event.created_at).toLocaleTimeString(), 'fine'));
  let text = '';
  if (value.arguments) text = pretty(value.arguments);
  else if (value.receipt) text = pretty(value.receipt);
  else if (event.kind === 'checkpoint') text = `Saved automatically. Operation: ${value.operation_id}\nOutcome: ${value.status}`;
  else if (value.answer) text = value.answer;
  else if (value.message || value.error) text = value.message || value.error;
  else if (value.usage) text = `Provider-reported usage: ${pretty(value.usage)}`;
  if (text) card.append(node('pre', text.length > 3500 ? `${text.slice(0,3500)}\n[Display clipped; full receipt remains on disk.]` : text));
  $('agent-events').append(card);
  if (++agentVisibleEvents > 200) $('agent-events').firstElementChild?.remove();
}
async function loadAgentTasks(selectFirst = false) {
  if (!token || $('app').hidden) return;
  const list = await api('/api/tasks');
  $('agent-project').textContent = `Working project: ${list.repo}`;
  $('agent-tasks').replaceChildren();
  for (const task of list.items) {
    const item = button(`${task.status} · ${task.prompt.slice(0, 85)}`, async () => {
      resetAgentView(task.id);
      await pollSelectedAgent();
    });
    $('agent-tasks').append(item);
  }
  if (!list.items.length) $('agent-tasks').append(node('p', 'No agent tasks yet.', 'fine'));
  if (selectFirst && !selectedAgentTask && list.items.length) resetAgentView(list.items[0].id);
}
async function pollSelectedAgent() {
  if (!selectedAgentTask || !token) return;
  const id = selectedAgentTask;
  const value = await api(`/api/task?id=${encodeURIComponent(id)}&after=${agentEventCursor}`);
  if (selectedAgentTask !== id) return;
  const previousStatus = agentTaskStatus;
  renderAgentTask(value.task);
  value.events.forEach(addAgentEvent);
  agentEventCursor = value.next_event;
  if (value.task.checkpoint_id && agentHeaderCheckpoint !== value.task.checkpoint_id) {
    const status = await api('/api/status');
    if (selectedAgentTask !== id) return;
    renderStatus(status);
    agentHeaderCheckpoint = value.task.checkpoint_id;
  }
  if (previousStatus !== value.task.status) await loadAgentTasks();
  if (value.task.status === 'needs_review' && value.unresolved.length && !$('agent-operation').value) {
    $('agent-operation').value = value.unresolved[0].id;
  }
}
$('agent-refresh').addEventListener('click', () => run(async () => {
  await loadAgentTasks(true); await pollSelectedAgent();
}));
document.querySelector('[data-tab="agent"]').addEventListener('click', async () => {
  try { await loadAgentTasks(true); await pollSelectedAgent(); }
  catch (error) { notice(error.message, true); }
});
form('agent-form', async () => {
  const task = await api('/api/tasks', {prompt: $('agent-prompt').value,
      new_task: $('agent-new').checked, allow_execution: $('agent-execute').checked});
  resetAgentView(task.id); renderAgentTask(task);
  await loadAgentTasks(); await pollSelectedAgent();
  notice('Task saved. Checkpoints are automatic. A browser refresh will not discard this task.');
});
$('agent-pause').addEventListener('click', async () => {
  if (!selectedAgentTask) return;
  try {
    await api('/api/task/pause', {task_id:selectedAgentTask});
    notice('Pause requested. Any in-flight model request may finish; its next tool will not start.');
    await pollSelectedAgent();
  } catch (error) { notice(error.message,true); }
});
$('agent-resume').addEventListener('click', () => run(async () => {
  if (!selectedAgentTask) return;
  await api('/api/task/resume',{task_id:selectedAgentTask});
  await pollSelectedAgent();
  notice('Resuming from saved receipts and compact checkpoint context.');
}));
form('agent-review', async () => {
  await api('/api/task/resolve',{task_id:selectedAgentTask,
    operation_id:$('agent-operation').value.trim(),note:$('agent-resolution').value,confirm:$('agent-confirm').checked});
  $('agent-operation').value = ''; $('agent-resolution').value = ''; $('agent-confirm').checked = false;
  await pollSelectedAgent();
  notice('Your observed outcome is recorded. Press Resume saved task to continue.');
});
$('lock').addEventListener('click', () => {
  resetAgentView(null);
  $('agent-tasks').replaceChildren(); $('agent-project').textContent='';
  $('agent-title').textContent='Select a task'; $('agent-status').textContent='Locked'; $('agent-stats').textContent='';
  $('agent-prompt').value=''; $('agent-operation').value=''; $('agent-resolution').value='';
  $('agent-review').hidden=true; $('agent-confirm').checked=false; $('agent-execute').checked=false;
});
setInterval(async () => {
  if (agentPolling || !token || $('app').hidden || $('tab-agent').hidden) return;
  agentPolling = true;
  try {
    if (!selectedAgentTask) await loadAgentTasks(true);
    if (selectedAgentTask) await pollSelectedAgent();
  } catch (error) {
    // A temporary browser/server disconnection does not discard the selected task.
    if (token) $('agent-error').textContent = `Connection interrupted. Saved task: ${selectedAgentTask || 'none'}. Reconnect or restart the server, then resume.`;
  } finally { agentPolling = false; }
}, 1500);
