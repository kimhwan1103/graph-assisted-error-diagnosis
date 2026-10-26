import { readFileSync } from 'node:fs';

export function loadTariffs() {
  return JSON.parse(readFileSync('/app/assets/tariffs.json', 'utf8'));
}
