import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../static/renderer-profile.js', import.meta.url), 'utf8');
const { MODEL_NAMES, rendererProfile } = await import(
  'data:text/javascript;base64,' + Buffer.from(source).toString('base64')
);

assert.deepEqual(MODEL_NAMES, { max: 'Станция Макс' });

const original = { id: 'max', name: 'Станция Макс', clock: true, boot: true };
const cases = [
  [{ hardware_model: 'yandex,yandexstation_2\0amlogic', hostname: 'renamed' }, 'max'],
  [{ hardware_model: 'Yandex Station Max', hostname: 'renamed' }, 'max'],
  [{ hardware_model: 'yandex,station-max', hostname: 'renamed' }, 'max'],
  [{ hostname: 'Yandex-Station-Max-1234' }, 'max'],
  [{ hardware_model: 'unknown', hostname: 'renamed' }, 'max'],
  [{}, 'max'],
];

for (const [identity, expected] of cases) {
  const result = rendererProfile(original, identity);
  assert.equal(result.id, expected, JSON.stringify(identity));
  assert.equal(result.name, 'Станция Макс');
  assert.equal(result.clock, true);
  assert.equal(result.boot, true);
}

assert.equal(rendererProfile({ id: 'max', name: 'Max' }, { hostname: 'renamed' }).name, 'Станция Макс');
assert.deepEqual(original, { id: 'max', name: 'Станция Макс', clock: true, boot: true });
console.log(`${cases.length + 1} Station Max renderer identity cases passed; hardware profile unchanged`);
