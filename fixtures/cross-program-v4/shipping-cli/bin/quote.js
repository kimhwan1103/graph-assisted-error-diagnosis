import { loadTariffs } from '../lib/load-tariffs.js';
import { loadLabels } from '../lib/load-labels.js';
import { calculate } from '../pricing/calculate.js';

try {
  const tariffs = loadTariffs();
  let label = 'shipping';
  if (process.env.OPTIONAL_CONFIG === '1') {
    try { label = loadLabels().label; }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      console.error(error.stack);
      console.error('[recovered] optional labels unavailable; using default shipping label');
    }
  }
  let order;
  try { order = JSON.parse(process.argv[2] ?? '{"amount":4000,"weightKg":2}'); }
  catch { throw new TypeError('INVALID_INPUT'); }
  console.log(JSON.stringify({ shipping_fee: calculate(order, tariffs), label }));
} catch (error) {
  console.error(error.stack);
  process.exitCode = error.message === 'INVALID_INPUT' ? 2 : 1;
}
