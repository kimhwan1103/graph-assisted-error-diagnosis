/* Fixed evaluator probes. Never runs commands or code from model responses. */
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { pathToFileURL } = require('node:url');

function sha(buffer) { return crypto.createHash('sha256').update(buffer).digest('hex'); }
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  return value;
}
function same(a, b) { return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b)); }
function functionalBody(body, spec) {
  if (!body || body.title !== spec.title || !Array.isArray(body.tasks)) return false;
  const ids = new Set();
  const tasks = [];
  for (const task of body.tasks) {
    if (!task || typeof task.id !== 'string' || !/^[0-9a-f-]{36}$/i.test(task.id) || ids.has(task.id)) return false;
    ids.add(task.id);
    const {id, ...content} = task;
    tasks.push(content);
  }
  const sort = items => items.map(x => JSON.stringify(canonical(x))).sort();
  return same(sort(tasks), sort(spec.expected_tasks));
}
function preserved(root, spec) {
  const detail = {};
  for (const [name, expected] of Object.entries(spec.protected_files)) {
    try { detail[name] = sha(fs.readFileSync(path.join(root, name))) === expected; }
    catch { detail[name] = false; }
  }
  return {pass: Object.keys(detail).length > 0 && Object.values(detail).every(Boolean), checks: detail};
}
async function regression(root) {
  const checks = {};
  try {
    const {loadConfig} = await import(pathToFileURL(path.join(root, 'src/config.js')).href);
    checks.default_config = same(loadConfig({}), {port: 3000, host: '0.0.0.0', title: '작은 완료'});
    checks.custom_config = same(loadConfig({PORT: '3123', HOST: '127.0.0.1', APP_TITLE: ' Test '}), {port: 3123, host: '127.0.0.1', title: 'Test'});
    checks.invalid_port = [0, 65536, 1.5, 'invalid'].every(value => {
      try { loadConfig({PORT: String(value)}); return false; } catch { return true; }
    });
  } catch { checks.config_import = false; }
  return {pass: Object.values(checks).every(Boolean), checks};
}
async function probe(spec, root = '/app', base = 'http://127.0.0.1:3000') {
  const result = {startup_pass: false, health_pass: null, functional_pass: null,
    regression_pass: null, data_preservation_pass: null, target_failure_resolved: null, details: {}};
  let health;
  for (let attempt = 0; attempt < 25; attempt++) {
    try {
      const r = await fetch(base + '/healthz', {signal: AbortSignal.timeout(800)});
      health = {status: r.status, body: await r.json()};
      result.startup_pass = true;
      break;
    } catch (error) {
      result.details.last_health_error = {message:String(error), cause:String(error.cause || '')};
      await new Promise(resolve => setTimeout(resolve, 200));
    }
  }
  if (result.startup_pass) {
    result.health_pass = health.status === 200 && health.body.status === 'ok' && health.body.app === spec.health_app;
    result.details.health = health;
    try {
      const r = await fetch(base + '/api/tasks', {signal: AbortSignal.timeout(2000)});
      const body = await r.json();
      result.functional_pass = r.status === 200 && functionalBody(body, spec);
      result.details.tasks = {status: r.status, body};
      result.target_failure_resolved = result.functional_pass;
    } catch (e) {
      result.functional_pass = false;
      result.target_failure_resolved = false;
      result.details.tasks_error = String(e);
    }
  }
  const regress = await regression(root), preservation = preserved(root, spec);
  result.regression_pass = regress.pass;
  result.data_preservation_pass = preservation.pass;
  result.details.regression = regress;
  result.details.preservation = preservation;
  return result;
}

module.exports = {functionalBody, preserved, regression, probe};
if (require.main === module) {
  const [mode, serialized, root = '/app'] = process.argv.slice(2);
  const spec = JSON.parse(serialized);
  (mode === 'regression' ? regression(root) : probe(spec, root))
    .then(value => console.log(JSON.stringify(value)))
    .catch(error => { console.error(String(error)); process.exitCode = 2; });
}
