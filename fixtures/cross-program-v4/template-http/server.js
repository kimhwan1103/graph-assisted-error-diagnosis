import { createServer } from 'node:http';
import { loadTemplate } from './view/load-template.js';
import { loadTheme } from './view/load-theme.js';
import { route } from './routes/page.js';

try {
  const template = loadTemplate();
  let theme = 'plain';
  if (process.env.OPTIONAL_CONFIG === '1') {
    try { theme = loadTheme().theme; }
    catch (error) {
      if (error.code !== 'ENOENT') throw error;
      console.error(error.stack);
      console.error('[recovered] optional theme unavailable; using plain theme');
    }
  }
  const server = createServer((req, res) => route(template, theme, req, res));
  server.listen(8080, '127.0.0.1', () => console.log('[ready] template-http listening on 127.0.0.1:8080'));
  process.on('SIGTERM', () => server.close());
} catch (error) {
  console.error(error.stack);
  process.exitCode = 1;
}
