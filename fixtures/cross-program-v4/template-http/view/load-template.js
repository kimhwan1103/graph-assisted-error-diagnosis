import { readFileSync } from 'node:fs';

export function loadTemplate() {
  return readFileSync('/app/views/index.html', 'utf8');
}
