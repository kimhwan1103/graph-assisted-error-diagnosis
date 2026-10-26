const fs = require('node:fs');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const spec = JSON.parse(fs.readFileSync('/__evaluation/spec.json', 'utf8'));
const equal = (a, b) => require('node:util').isDeepStrictEqual(a, b);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
function hashes() {
  return Object.fromEntries(Object.keys(spec.protected_files).map(path => {
    try { return [path, crypto.createHash('sha256').update(fs.readFileSync('/app/' + path)).digest('hex')]; }
    catch { return [path, null]; }
  }));
}
async function main() {
  const before = hashes();
  let functional = false, regression = false, execution = false, health = null;
  const details = [];
  if (spec.lifecycle !== 'http') {
    for (const test of spec.tests) {
      const r = spawnSync(process.execPath, ['/app/' + spec.entry, ...test.args], { cwd:'/app', env:process.env, encoding:'utf8', timeout:5000 });
      let value = null;
      try { value = JSON.parse(r.stdout.trim()); } catch {}
      const pass = r.status === test.code && (test.expected ? equal(value, test.expected) : r.stderr.includes(test.error));
      details.push({ pass, code:r.status, stdout:r.stdout, stderr:r.stderr, error:r.error?.code ?? null });
    }
    execution = details[0].code === 0;
    functional = details[0].pass;
    regression = details.slice(1).every(x => x.pass);
  } else {
    const child = spawn(process.execPath, ['/app/' + spec.entry], { cwd:'/app', env:process.env, stdio:['ignore','pipe','pipe'] });
    let stdout = '', stderr = '';
    child.stdout.on('data', chunk => stdout += chunk);
    child.stderr.on('data', chunk => stderr += chunk);
    child.on('error', error => stderr += String(error));
    try {
      let page = null;
      for (let i = 0; i < 40; i++) {
        try { page = await fetch('http://127.0.0.1:8080/', {signal:AbortSignal.timeout(250)}); break; }
        catch { if (child.exitCode !== null) break; await sleep(50); }
      }
      if (page) {
        const body = await page.text();
        functional = page.status === 200 && body === spec.html && page.headers.get('x-theme') === 'plain';
        const h = await fetch('http://127.0.0.1:8080/healthz', {signal:AbortSignal.timeout(1000)});
        health = h.status === 200 && equal(await h.json(), spec.health);
        const missing = await fetch('http://127.0.0.1:8080/not-present', {signal:AbortSignal.timeout(1000)});
        regression = health && missing.status === 404 && equal(await missing.json(), {error:'not_found'});
        await sleep(100);
        execution = child.exitCode === null;
        details.push({body, status:page.status});
      } else { health = false; }
    } catch (error) { details.push({request_error:String(error)}); health = false; }
    finally {
      child.kill('SIGTERM');
      await Promise.race([new Promise(resolve => child.once('exit', resolve)), sleep(1000)]);
      if (child.exitCode === null) child.kill('SIGKILL');
      details.push({stdout, stderr, exit:child.exitCode});
    }
  }
  const after = hashes();
  return { execution_pass:execution, functional_pass:functional, regression_pass:regression,
    health_pass:health, target_failure_resolved:execution && functional,
    data_preservation_pass:equal(before, spec.protected_files) && equal(after, spec.protected_files),
    before_hashes:before, after_hashes:after, details };
}
main().then(value => console.log(JSON.stringify(value))).catch(error => { console.error(error.stack); process.exitCode=70; });
