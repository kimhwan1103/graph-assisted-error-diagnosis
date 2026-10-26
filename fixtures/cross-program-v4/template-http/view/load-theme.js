import { readFileSync } from 'node:fs';

export function loadTheme() {
  return JSON.parse(readFileSync('/app/config/local-theme.json', 'utf8'));
}
