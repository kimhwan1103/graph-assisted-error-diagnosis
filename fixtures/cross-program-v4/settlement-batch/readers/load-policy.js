import { readFileSync } from 'node:fs';

export function loadPolicy() {
  return JSON.parse(readFileSync('/app/config/settlement-rules.json', 'utf8'));
}
