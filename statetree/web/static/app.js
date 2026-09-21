'use strict';

(() => {
  const savedRun = /^#run=([0-9a-f]{32})$/.exec(location.hash);
  if (savedRun) { location.replace('/benchmark#run=' + savedRun[1]); return; }

  const $ = (id) => document.getElementById(id);
  const put = (id, value) => { $(id).textContent = value; };
  const activeStates = new Set(['queued', 'running', 'cancel_requested', 'applying']);
  const resumableStates = new Set(['interrupted', 'failed', 'cancelled', 'waiting_for_input']);
  const knownStatuses = new Set([...activeStates, ...resumableStates, 'completed', 'applied', 'conflict']);
  const identifier = (value) => typeof value === 'string' && /^[0-9a-f]{32}$/.test(value);
  const label = (value) => String(value || 'unknown').replaceAll('_', ' ');
  const pretty = (value) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
  const firstLine = (value, max = 105) => String(value || '').replace(/\s+/g, ' ').slice(0, max);
  const element = (tag, className, value) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = value;
    return node;
  };
  let ownerKey = '';
  let session = 0;
  let projects = [];
  let tasks = [];
  let selectedProject = '';
  let selectedTask = '';
  let task = null;
  let busy = false;
  let timer = null;
  let polling = false;
  let stateConnected = false;
  let sidebarSignature = '';
  let taskSignature = '';
  let inspectorSignature = '';
  let refreshFailure = false;
  let mutationVersion = 0;
  const pending = new Set();
  const initialTask = /^#task=([0-9a-f]{32})$/.exec(location.hash);
  let requestedTask = initialTask ? initialTask[1] : '';

  function notice(message = '', error = false) {
    put('notice', message);
    $('notice').hidden = !message;
    $('notice').classList.toggle('error', error);
  }

  function date(value, timeOnly = false) {
    if (!value) return '';
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) return '';
    return timeOnly ? parsed.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}) :
      parsed.toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'});
  }

  async function request(path, options = {}) {
    const requestSession = session;
    const controller = new AbortController();
    pending.add(controller);
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch(path, {
        method: options.method || 'GET',
        headers: {'X-Owner-Key': ownerKey, ...(options.body ? {'Content-Type': 'application/json'} : {})},
        body: options.body ? JSON.stringify(options.body) : undefined,
        cache: 'no-store', credentials: 'same-origin', redirect: 'error', signal: controller.signal,
      });
      if (requestSession !== session) throw new Error('Session changed.');
      let value;
      try { value = await response.json(); }
      catch (_) { throw new Error('The server returned an unreadable response.'); }
      if (!response.ok) {
        const error = new Error(typeof value.error === 'string' ? value.error : 'The request did not complete.');
        error.status = response.status;
        throw error;
      }
      return value;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Its outcome has not been confirmed.');
      throw error;
    } finally {
      clearTimeout(timeout);
      pending.delete(controller);
    }
  }

  function showLogin(message = '') {
    put('login-error', message);
    $('login-error').hidden = !message;
    if (!$('login-dialog').open) $('login-dialog').showModal();
    $('owner-key').focus();
  }

  function disconnect(message = '') {
    session += 1;
    ownerKey = '';
    pending.forEach((controller) => controller.abort());
    clearTimeout(timer);
    projects = []; tasks = []; task = null; selectedTask = ''; selectedProject = '';
    busy = false; stateConnected = false; refreshFailure = false;
    sidebarSignature = ''; taskSignature = ''; inspectorSignature = '';
    $('owner-key').value = '';
    $('prompt').value = '';
    put('task-prompt', ''); put('task-result', ''); $('event-list').replaceChildren();
    $('conversation-history')?.replaceChildren();
    put('task-id', ''); put('resume-count', '0');
    render();
    notice(message);
  }

  function setTask(value, updateHash = true) {
    if (!value || !identifier(value.id) || !identifier(value.project_id)) return;
    const changed = selectedTask !== value.id;
    selectedTask = value.id;
    selectedProject = value.project_id;
    task = value;
    const previous = tasks.findIndex((item) => item.id === value.id);
    if (previous < 0) tasks.unshift(value); else tasks[previous] = value;
    if (changed) { taskSignature = ''; inspectorSignature = ''; }
    if (updateHash) history.replaceState(null, '', '#task=' + value.id);
    render();
    if (changed) $('conversation-scroll').scrollTop = 0;
  }

  function ingestState(value) {
    projects = Array.isArray(value.projects) ? value.projects.filter((item) => identifier(item.id)) : [];
    tasks = Array.isArray(value.tasks) ? value.tasks.filter((item) => identifier(item.id) && identifier(item.project_id)) : [];
    stateConnected = true;
    if (!projects.some((project) => project.id === selectedProject)) selectedProject = projects[0]?.id || '';
    if (requestedTask) {
      const saved = tasks.find((item) => item.id === requestedTask);
      if (saved) { setTask(saved, false); requestedTask = ''; }
    }
    const latest = tasks.find((item) => item.id === selectedTask);
    if (latest) task = latest;
    render();
  }

  async function refresh() {
    if (!ownerKey || polling || busy || document.hidden) return;
    polling = true;
    const refreshSession = session;
    const refreshVersion = mutationVersion;
    const refreshTask = selectedTask;
    try {
      const value = await request('/api/chat/state');
      if (refreshSession !== session || refreshVersion !== mutationVersion) return;
      ingestState(value);
      // Polling is read-only. It must never queue or resume a task.
      if (refreshTask && refreshTask === selectedTask) {
        const result = await request('/api/chat/tasks/' + refreshTask);
        if (refreshSession === session && refreshVersion === mutationVersion && selectedTask === refreshTask) setTask(result.task, false);
      }
      if (refreshFailure) notice();
      refreshFailure = false;
    } catch (error) {
      if (refreshSession !== session) return;
      stateConnected = false;
      if (error.status === 401 || error.status === 403) {
        disconnect(); showLogin('Your session could not be authenticated. Enter your owner key again.');
      } else {
        refreshFailure = true;
        notice('Connection interrupted. Saved activity is shown below; checking again shortly. ' + error.message, true);
        render();
      }
    } finally { polling = false; }
  }

  function schedulePoll() {
    clearTimeout(timer);
    if (!ownerKey) return;
    timer = setTimeout(async () => { await refresh(); schedulePoll(); }, 3000);
  }

  function project() { return projects.find((value) => value.id === selectedProject); }

  function renderSidebar() {
    const current = project();
    const visibleTasks = tasks.filter((item) => item.project_id === selectedProject)
      .sort((left, right) => String(right.updated_at || '').localeCompare(String(left.updated_at || '')));
    const signature = JSON.stringify([projects, selectedProject, selectedTask, visibleTasks.map(({id, prompt, status, updated_at}) => [id, prompt, status, updated_at])]);
    if (signature !== sidebarSignature) {
      sidebarSignature = signature;
      const select = $('project-select');
      select.replaceChildren();
      if (!projects.length) {
        const option = element('option', '', ownerKey ? 'No registered projects' : 'No project connected');
        option.value = ''; select.append(option);
      }
      for (const item of projects) {
        const option = element('option', '', item.name || 'Local project');
        option.value = item.id; select.append(option);
      }
      select.value = selectedProject;
      select.disabled = !ownerKey || !projects.length || busy;
      const list = $('task-list');
      list.replaceChildren();
      if (!visibleTasks.length) list.append(element('p', 'empty-list', ownerKey ? 'No tasks yet. Start with a question or a change.' : 'Your tasks will appear here.'));
      for (const item of visibleTasks) {
        const button = element('button', 'task-item' + (item.id === selectedTask ? ' active' : ''));
        button.type = 'button'; button.title = String(item.prompt || 'Untitled task');
        if (item.id === selectedTask) button.setAttribute('aria-current', 'true');
        button.append(element('span', 'task-item-dot ' + (knownStatuses.has(item.status) ? item.status : '')));
        const copy = element('span', 'task-item-copy');
        copy.append(element('span', 'task-item-title', firstLine(item.prompt) || 'Untitled task'));
        copy.append(element('span', 'task-item-meta', label(item.status) + (date(item.updated_at) ? ' · ' + date(item.updated_at) : '')));
        button.append(copy);
        button.addEventListener('click', () => { setTask(item); setSidebar(false); refresh(); });
        list.append(button);
      }
      put('task-count', visibleTasks.length);
    }
    $('project-select').disabled = !ownerKey || !projects.length || busy;
    const online = Boolean(current?.online && stateConnected);
    $('worker-dot').classList.toggle('online', online);
    put('worker-label', !ownerKey ? 'Worker not connected' : !stateConnected ? 'Connection unknown' : online ? 'Local worker online' : 'Local worker offline');
    put('worker-detail', current ? online ? 'Ready for project tasks' : current.last_seen ? 'Last seen ' + date(current.last_seen) : 'Waiting for a heartbeat' : 'Connect your local workspace');
    put('project-hint', !ownerKey ? 'Sign in to see your projects.' : !current ? 'Start your worker to register a project.' : online ? 'Connected from your PC' : 'Tasks will wait for the worker.');
    put('header-project', current?.name || 'Workspace');
    put('header-task', task ? firstLine(task.prompt, 70) : 'New task');
    put('composer-project', current?.name || 'No project selected');
    put('auth-button', ownerKey ? 'Sign out' : 'Sign in');
  }

  function renderEvents(events) {
    const openIds = new Set([...$('event-list').querySelectorAll('details[open]')].map((node) => node.dataset.eventId));
    const list = $('event-list');
    list.replaceChildren();
    const seen = new Set();
    for (const [index, event] of (Array.isArray(events) ? events : []).entries()) {
      if (!event || typeof event !== 'object') continue;
      const id = String(event.id || 'event-' + index);
      if (seen.has(id)) continue;
      seen.add(id);
      const type = typeof event.type === 'string' ? event.type : 'activity';
      const row = element('div', 'event-row' + (type === 'error' ? ' error' : ''));
      if (type === 'message' && !event.details) {
        row.append(element('div', 'event-message', String(event.text || '')));
      } else {
        const details = element('details'); details.dataset.eventId = id;
        details.open = openIds.has(id);
        const summary = element('summary', 'event-summary');
        summary.append(element('span', 'event-symbol', type === 'error' ? '!' : type === 'checkpoint' ? '◇' : '›'));
        summary.append(element('span', 'event-label', firstLine(event.text, 150) || label(type)));
        summary.append(element('span', 'event-type', ['tool_start', 'tool_result', 'checkpoint', 'usage', 'agent', 'status', 'error'].includes(type) ? label(type) : 'activity'));
        summary.append(element('time', 'event-time', date(event.created_at, true)));
        details.append(summary);
        const parts = [];
        if (event.text) parts.push(String(event.text));
        if (event.details !== undefined && event.details !== null) parts.push(pretty(event.details));
        if (!parts.length) parts.push('No additional details were reported.');
        details.append(element('pre', 'event-detail', parts.join('\n\n')));
        row.append(details);
      }
      list.append(row);
    }
  }

  function renderTask() {
    $('welcome').hidden = Boolean(task);
    $('conversation').hidden = !task;
    if (!task) return;
    const parents = [];
    const visited = new Set([task.id]);
    let parentId = task.parent_task_id;
    while (parentId && !visited.has(parentId)) {
      visited.add(parentId);
      const parent = tasks.find((item) => item.id === parentId);
      if (!parent) break;
      parents.unshift(parent);
      parentId = parent.parent_task_id;
    }
    const signature = JSON.stringify([task, parents.map((item) => [item.id, item.prompt, item.result, item.updated_at])]);
    if (signature === taskSignature) return;
    const scroller = $('conversation-scroll');
    const nearBottom = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 100;
    taskSignature = signature;
    let historyRoot = $('conversation-history');
    if (!historyRoot) {
      historyRoot = element('div', 'conversation-history');
      historyRoot.id = 'conversation-history';
      $('conversation').prepend(historyRoot);
    }
    historyRoot.replaceChildren();
    for (const parent of parents) {
      const previous = element('details', 'previous-turn');
      previous.append(element('summary', '', 'Earlier task · ' + firstLine(parent.prompt, 90)));
      previous.append(element('p', 'previous-prompt', String(parent.prompt || '')));
      if (parent.result) previous.append(element('div', 'previous-result', pretty(parent.result)));
      const open = element('button', 'text-button', 'Inspect this task'); open.type = 'button';
      open.addEventListener('click', () => setTask(parent)); previous.append(open);
      historyRoot.append(previous);
    }
    put('task-prompt', String(task.prompt || ''));
    put('task-status', label(task.status));
    $('task-status').className = 'status-pill ' + (knownStatuses.has(task.status) ? task.status : '');
    put('task-updated', date(task.updated_at) ? 'Updated ' + date(task.updated_at) : '');
    renderEvents(task.events);
    put('task-result', task.result ? pretty(task.result) : '');
    $('result-message').hidden = !task.result;
    const notes = {
      queued: project()?.online ? 'Queued for the local worker.' : 'Queued. The task will start when your local worker connects.',
      running: 'The local worker is running this task. Activity appears as it is reported.',
      waiting_for_input: 'The agent is waiting for input. Review the activity before resuming.',
      cancel_requested: 'Stop requested. Waiting for the worker to confirm it has stopped.',
      cancelled: 'The task was stopped. Resume continues from the last confirmed checkpoint.',
      interrupted: 'The task was interrupted. Review its last activity, then resume from the saved checkpoint.',
      failed: 'The task failed. Review the error and tool output before resuming.',
      completed: 'Task complete. Review the result and any changes before applying them.',
      applying: 'The worker is checking the original files and applying the changes.',
      applied: 'The worker reports that the changes were applied to your project.',
      conflict: 'The changes could not be applied because the project changed. Review the reported conflict; the task workspace is retained.',
    };
    put('task-state-note', notes[task.status] || 'Latest task status: ' + label(task.status));
    $('task-state-note').classList.toggle('warning', ['failed', 'interrupted', 'conflict'].includes(task.status));
    if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  }

  function renderDiff(diff) {
    const files = [];
    let file = null;
    const lines = diff.split('\n');
    for (const [index, line] of lines.entries()) {
      if (line.startsWith('+++ ') && lines[index - 1]?.startsWith('--- ')) {
        const name = line.slice(4) === '/dev/null' ? lines[index - 1].slice(4).replace(/^a\//, '') : line.slice(4).replace(/^b\//, '');
        file = {name, added: 0, removed: 0}; files.push(file);
      } else if (line.startsWith('Binary file changed: ')) {
        file = null; files.push({name: line.slice(21), added: 0, removed: 0, binary: true});
      } else if (file && line.startsWith('+') && !line.startsWith('+++')) file.added += 1;
      else if (file && line.startsWith('-') && !line.startsWith('---')) file.removed += 1;
    }
    put('file-count', files.length || (diff ? '—' : 0));
    const list = $('changed-files'); list.replaceChildren();
    if (!files.length) list.append(element('p', 'detail-empty', diff ? 'A diff was reported. Open it below to inspect the changes.' : task ? 'No file changes have been reported.' : 'Changes will appear as your agent works.'));
    for (const entry of files) {
      const row = element('div', 'changed-file');
      row.append(element('span', 'file-icon', '▤'), element('span', 'file-path', entry.name), element('span', 'file-delta', entry.binary ? 'binary' : '+' + entry.added + ' −' + entry.removed));
      list.append(row);
    }
    const pre = $('task-diff'); pre.replaceChildren();
    for (const line of diff.split('\n')) {
      pre.append(element('span', 'diff-line' + (line.startsWith('+') ? ' add' : line.startsWith('-') ? ' remove' : line.startsWith('@@') ? ' hunk' : ''), line));
    }
    $('diff-details').hidden = !diff;
  }

  function usageNumber(usage, names) {
    for (const source of [usage?.totals, usage?.total, usage]) {
      if (!source || typeof source !== 'object') continue;
      for (const name of names) {
        if (typeof source[name] === 'number' && Number.isFinite(source[name]) && source[name] >= 0) return source[name];
      }
    }
    return null;
  }

  function renderInspector() {
    const signature = JSON.stringify([task?.id, task?.diff, task?.checkpoint, task?.usage, task?.status, task?.resume_count, task?.apply_requested]);
    if (signature === inspectorSignature) return;
    inspectorSignature = signature;
    renderDiff(typeof task?.diff === 'string' ? task.diff : '');
    const checkpoint = task?.checkpoint;
    const checkpointRoot = $('checkpoint-content');
    const checkpointOpen = Boolean(checkpointRoot.querySelector('details[open]'));
    checkpointRoot.replaceChildren();
    if (checkpoint && typeof checkpoint === 'object' && Object.keys(checkpoint).length) {
      const note = checkpoint.note || checkpoint.summary || checkpoint.notes || checkpoint.message;
      checkpointRoot.append(element('span', 'checkpoint-badge', 'Checkpoint recorded'));
      if (note) checkpointRoot.append(element('p', 'checkpoint-note', pretty(note)));
      if (Number.isInteger(checkpoint.confirmed_steps)) checkpointRoot.append(element('p', 'detail-caption', checkpoint.confirmed_steps + ' confirmed steps' + (date(checkpoint.created_at) ? ' · ' + date(checkpoint.created_at) : '')));
      const details = element('details', 'raw-details'); details.open = checkpointOpen;
      details.append(element('summary', '', 'Inspect saved checkpoint'), element('pre', 'checkpoint-raw', pretty(checkpoint)));
      checkpointRoot.append(details);
    } else checkpointRoot.append(element('p', 'detail-empty', task ? 'No checkpoint has been reported yet.' : 'Completed steps are saved here so a task can continue later.'));
    const usage = task?.usage;
    const counts = {
      'usage-input': usageNumber(usage, ['input_tokens', 'prompt_tokens', 'inputTokens']),
      'usage-output': usageNumber(usage, ['output_tokens', 'completion_tokens', 'outputTokens']),
      'usage-cache': usageNumber(usage, ['cache_read_input_tokens', 'cached_tokens', 'cacheReadInputTokens']),
      'usage-requests': usageNumber(usage, ['requests', 'request_count', 'model_requests', 'total_requests']),
    };
    const incomplete = usage?.status === 'unknown' || usage?.unknown_usage_requests > 0 || usage?.ambiguous_usage_requests > 0;
    if (incomplete) for (const key of ['usage-input', 'usage-output', 'usage-cache']) counts[key] = null;
    for (const [id, value] of Object.entries(counts)) {
      put(id, value === null ? 'Unknown' : value.toLocaleString());
      $(id).classList.toggle('unknown', value === null);
    }
    put('usage-note', incomplete ? 'Total token usage is unknown. The record retains known subtotals and requests with missing or ambiguous usage.' : usage ? 'Reported usage only. Missing counts stay unknown; inspect the record for retries and delegated calls.' : 'Usage appears when reported by the worker.');
    $('usage-details').hidden = !usage;
    put('usage-raw', usage ? pretty(usage) : '');
    $('task-meta').hidden = !task;
    put('task-id', task?.id || ''); put('resume-count', task?.resume_count ?? 0);
    const applied = task?.status === 'applied';
    const applying = task?.status === 'applying' || task?.apply_requested;
    $('apply-note').hidden = !task?.diff;
    put('apply-note', applied ? 'Changes applied by the local worker.' : task?.status === 'conflict' ? 'A conflict was reported. Review the task result before resolving it locally.' : applying ? 'Apply requested. Waiting for confirmation from the worker.' : 'Applies to the original project after the worker checks for conflicts.');
  }

  function renderControls() {
    const available = Boolean(ownerKey && project());
    const active = task && activeStates.has(task.status);
    const promptBytes = new TextEncoder().encode($('prompt').value.trim()).length;
    const oversized = promptBytes > 2000;
    const canCompose = !task || ['completed', 'applied'].includes(task.status);
    $('prompt').disabled = !available || busy || !canCompose;
    $('prompt').placeholder = task && canCompose ? 'Ask a follow-up or describe the next change…' : 'Describe a task for your project…';
    $('send-task').disabled = !available || busy || !canCompose || oversized || !$('prompt').value.trim();
    $('prompt').setAttribute('aria-invalid', String(oversized));
    put('prompt-budget', promptBytes ? promptBytes.toLocaleString() + ' / 2,000 bytes' : 'Enter to send');
    $('new-task').disabled = busy;
    $('stop-task').hidden = !task || !['queued', 'running', 'waiting_for_input', 'cancel_requested'].includes(task.status);
    $('stop-task').disabled = busy || task?.status === 'cancel_requested' || Boolean(task?.cancel_requested);
    $('resume-task').hidden = !task || !resumableStates.has(task.status);
    $('resume-task').disabled = busy;
    $('fresh-task').hidden = !task || active;
    $('fresh-task').disabled = busy;
    $('apply-task').hidden = !task || !task.diff || task.status !== 'completed';
    $('apply-task').disabled = busy || Boolean(task?.apply_requested);
    put('composer-note', oversized ? 'Keep the task within 2,000 UTF-8 bytes. Shorten it or split it into follow-ups.' : !ownerKey ? 'Sign in to connect to your local project.' : !project() ? 'Start the local worker to register a project.' : busy ? 'Waiting for the server to confirm your request…' : task ? canCompose ? 'Follow-ups continue with this task’s files and saved context.' : active ? 'You can stop this task, or open a new task while it runs.' : 'Resume this task to continue, or choose New task.' : !project().online ? 'Your worker is offline. New tasks will wait in the queue.' : 'Changes are kept in a separate task workspace until you apply them.');
  }

  function render() { renderSidebar(); renderTask(); renderInspector(); renderControls(); }

  function newTask() {
    if (busy) return;
    task = null; selectedTask = ''; taskSignature = ''; inspectorSignature = ''; requestedTask = '';
    history.replaceState(null, '', location.pathname + location.search);
    $('prompt').value = ''; $('prompt').style.height = '';
    notice(); render(); setSidebar(false);
    if (ownerKey) $('prompt').focus(); else showLogin();
  }

  function setSidebar(open) {
    $('app-shell').classList.toggle('sidebar-open', open);
    $('sidebar-toggle').setAttribute('aria-expanded', String(open));
  }

  function setInspector(open) {
    $('app-shell').classList.toggle('inspector-hidden', !open);
    $('inspector-toggle').setAttribute('aria-expanded', String(open));
  }

  async function mutate(path, body, afterSuccess) {
    if (busy || !ownerKey) return;
    const actionSession = session;
    mutationVersion += 1;
    busy = true; notice(); renderControls();
    try {
      const value = await request(path, {method: 'POST', body});
      if (actionSession !== session) return;
      if (!value.task || !identifier(value.task.id)) throw new Error('The server did not return a valid task record.');
      setTask(value.task);
      if (afterSuccess) afterSuccess(value);
    } catch (error) {
      if (actionSession !== session) return;
      if (error.status === 401 || error.status === 403) {
        disconnect(); showLogin('Your owner key was not accepted.');
      } else notice(error.message + (error.status ? '' : ' Check the saved task list before retrying; the request has not been repeated.'), true);
    } finally {
      if (actionSession === session) { busy = false; render(); }
    }
  }

  $('login-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('login-submit').disabled) return;
    const candidate = $('owner-key').value.trim();
    if (!candidate) return;
    session += 1;
    const loginSession = session;
    ownerKey = candidate;
    $('owner-key').value = '';
    $('login-submit').disabled = true; $('login-error').hidden = true;
    try {
      const value = await request('/api/chat/state');
      if (loginSession !== session) return;
      ingestState(value);
      $('login-dialog').close(); notice(); schedulePoll();
      if (requestedTask) {
        const result = await request('/api/chat/tasks/' + requestedTask);
        if (loginSession === session) { setTask(result.task); requestedTask = ''; }
      }
    } catch (error) {
      if (loginSession !== session) return;
      disconnect(); showLogin(error.message || 'Could not connect to the workspace.');
    } finally { $('login-submit').disabled = false; }
  });

  $('login-dismiss').addEventListener('click', () => { $('owner-key').value = ''; $('login-dialog').close(); });
  $('login-dialog').addEventListener('cancel', () => { $('owner-key').value = ''; });
  $('auth-button').addEventListener('click', () => { if (ownerKey) disconnect('Signed out. Private task data has been cleared from this tab.'); else showLogin(); });
  $('new-task').addEventListener('click', newTask);
  $('project-select').addEventListener('change', () => { selectedProject = $('project-select').value; newTask(); });
  $('sidebar-toggle').addEventListener('click', () => setSidebar(!$('app-shell').classList.contains('sidebar-open')));
  $('inspector-toggle').addEventListener('click', () => setInspector($('app-shell').classList.contains('inspector-hidden')));
  $('inspector-close').addEventListener('click', () => setInspector(false));
  $('main-content').addEventListener('click', (event) => { if (!event.target.closest('#sidebar-toggle')) setSidebar(false); });
  $('prompt').addEventListener('input', () => { $('prompt').style.height = 'auto'; $('prompt').style.height = Math.min($('prompt').scrollHeight, 180) + 'px'; renderControls(); });
  $('prompt').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
  document.querySelectorAll('[data-prompt]').forEach((button) => button.addEventListener('click', () => {
    if (!ownerKey) { showLogin(); return; }
    $('prompt').value = button.dataset.prompt; $('prompt').dispatchEvent(new Event('input')); $('prompt').focus();
  }));
  $('composer').addEventListener('submit', (event) => {
    event.preventDefault();
    const prompt = $('prompt').value.trim();
    if (new TextEncoder().encode(prompt).length > 2000) { notice('Task prompts are limited to 2,000 UTF-8 bytes. Keep this request focused and use follow-ups for additional work.', true); return; }
    if (!prompt || !identifier(selectedProject) || busy || !ownerKey || (task && !['completed', 'applied'].includes(task.status))) return;
    const payload = {project_id: selectedProject, prompt};
    const normalized = (value) => String(value).trim().replace(/\s+/g, ' ');
    if (task) {
      if (normalized(prompt) !== normalized(task.prompt)) payload.parent_task_id = task.id;
      else payload.task_id = task.id;
    }
    mutate('/api/chat/tasks', payload, (value) => {
      $('prompt').value = ''; $('prompt').style.height = '';
      if (value.resumed) notice('Attached to the saved task and its existing checkpoint.');
    });
  });
  $('resume-task').addEventListener('click', () => {
    if (task && resumableStates.has(task.status)) mutate('/api/chat/tasks', {project_id: task.project_id, prompt: task.prompt, task_id: task.id}, () => notice('Resume requested. The worker will continue from its saved task state.'));
  });
  $('fresh-task').addEventListener('click', () => {
    if (task && !activeStates.has(task.status)) mutate('/api/chat/tasks', {project_id: task.project_id, prompt: task.prompt, fresh: true});
  });
  $('stop-task').addEventListener('click', () => { if (task) mutate('/api/chat/tasks/' + task.id + '/cancel', {}); });
  $('apply-task').addEventListener('click', () => { if (task?.status === 'completed' && task.diff && !task.apply_requested) mutate('/api/chat/tasks/' + task.id + '/apply', {}); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && ownerKey) refresh(); });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { setSidebar(false); if (matchMedia('(max-width:1180px)').matches) setInspector(false); }
    if (event.key.toLowerCase() === 'n' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.closest('input,textarea,select,button,dialog')) newTask();
  });
  window.addEventListener('hashchange', () => {
    const match = /^#task=([0-9a-f]{32})$/.exec(location.hash);
    if (!match) return;
    const found = tasks.find((item) => item.id === match[1]);
    if (found) { setTask(found, false); refresh(); } else requestedTask = match[1];
  });
  window.addEventListener('pagehide', () => disconnect());
  window.addEventListener('pageshow', (event) => { if (event.persisted) showLogin(); });
  setInspector(!matchMedia('(max-width:1180px)').matches);
  render();
  showLogin();
})();
