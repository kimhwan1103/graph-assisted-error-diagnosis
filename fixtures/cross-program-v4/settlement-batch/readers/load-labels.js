import { readFileSync } from 'node:fs';

export function loadLabels() {
  return JSON.parse(readFileSync('/app/config/local-labels.json', 'utf8'));
}
