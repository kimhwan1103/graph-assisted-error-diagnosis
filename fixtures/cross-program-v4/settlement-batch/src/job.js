import { readFileSync } from 'node:fs';
import { loadPolicy } from '../readers/load-policy.js';
import { loadLabels } from '../readers/load-labels.js';
import { aggregate } from '../pipeline/aggregate.js';

try {
  const policy = loadPolicy();
  let label = 'settlement';
  if (process.env.OPTIONAL_CONFIG === '1') {
    try { label = loadLabels().label; }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      console.error(error.stack);
      console.error('[recovered] optional labels unavailable; using default settlement label');
    }
  }
  let transactions;
  try { transactions = JSON.parse(process.argv[2] ?? readFileSync('/app/fixtures/transactions.json', 'utf8')); }
  catch { throw new TypeError('INVALID_TRANSACTIONS'); }
  console.log(JSON.stringify({ ...aggregate(transactions, policy), label }));
} catch (error) {
  console.error(error.stack);
  process.exitCode = error.message === 'INVALID_TRANSACTIONS' ? 2 : 1;
}
