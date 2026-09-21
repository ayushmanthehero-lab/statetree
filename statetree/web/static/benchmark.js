'use strict';
const byId = (id) => document.getElementById(id);
const button = byId('run-button');
const statusLine = byId('status');
let running = false;
const text = (id, value) => { byId(id).textContent = value; };
const format = (number) => Number.isFinite(number) ? number.toLocaleString() : 'Unavailable';
function status(message, error = false) {
  statusLine.textContent = message;
  statusLine.classList.toggle('error', error);
}
async function request(url, options) {
  const response = await fetch(url, {...options, cache: 'no-store'});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'The request did not complete.');
  return value;
}
function render(value) {
  const report = value.report;
  if (!report) throw new Error(value.error || 'This run has no completed report.');
  for (const name of ['baseline', 'statetree']) {
    const result = report[name];
    text(name + '-input', format(result.usage.input_tokens));
    text(name + '-output-tokens', format(result.usage.output_tokens));
    text(name + '-cache', format(result.usage.cache_read_input_tokens));
    text(name + '-answer', result.output || result.error || 'No answer returned.');
    const badge = byId(name + '-check');
    badge.textContent = result.passed ? 'Answer passed' : result.status === 'error' ? 'Inference failed' : 'Answer failed';
    badge.className = 'badge ' + (result.passed ? 'pass' : 'fail');
  }
  const comparison = report.comparison;
  const percent = comparison.input_reduction_percent;
  text('saving', percent === null ? 'Input reduction is unavailable.' :
    percent < 0 ? Math.abs(percent) + '% more input tokens in this example.' :
    percent + '% fewer input tokens in this example.');
  text('comparison-note', (comparison.both_tasks_passed ? 'Both answers passed the fixed fact check. ' :
    'At least one answer or inference failed. This does not establish a successful reduction. ') + comparison.note);
  text('raw-report', JSON.stringify(value, null, 2));
  byId('report-details').hidden = false;
  status(value.persistence_note || 'Comparison finished. Inspect the counts and answer checks below.',
    !comparison.both_tasks_passed || value.persistence === 'failed');
}
async function poll(id) {
  const started = Date.now();
  while (Date.now() - started < 240000) {
    const value = await request('/api/runs/' + id);
    if (value.status === 'completed') { render(value); return; }
    if (value.status === 'error') throw new Error(value.error || 'The demo did not finish.');
    await new Promise(resolve => setTimeout(resolve, 2000));
  }
  throw new Error('This run is taking longer than expected. Reload this page to check its report; do not start a duplicate run.');
}
button.addEventListener('click', async () => {
  if (running) return;
  const code = byId('access-code').value;
  if (!code) { status('Enter the demo access code supplied by the operator.', true); byId('access-code').focus(); return; }
  running = true;
  button.disabled = true;
  status('Running the fixed baseline and commit-context calls. Keep this page open.');
  try {
    const value = await request('/api/runs', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Demo-Key': code}, body: '{}'});
    byId('access-code').value = '';
    history.replaceState(null, '', '#run=' + value.id);
    await poll(value.id);
  } catch (error) { status(error.message || 'The demo is unavailable.', true); }
  finally { running = false; button.disabled = false; }
});
const savedRun = /^#run=([0-9a-f]{32})$/.exec(location.hash);
if (savedRun) {
  running = true;
  button.disabled = true;
  status('Loading this run’s report…');
  poll(savedRun[1]).catch(error => status(error.message, true)).finally(() => { running = false; button.disabled = false; });
}
