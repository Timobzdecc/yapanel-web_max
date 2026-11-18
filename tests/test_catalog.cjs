const fs = require('fs'), vm = require('vm'), assert = require('assert');
const html = fs.readFileSync(require('path').join(__dirname, '../static/index.html'), 'utf8');
const app = fs.readFileSync(require('path').join(__dirname, '../static/app.js'), 'utf8');
const ui = html + '\n' + app;

const yam2Source = ui.slice(ui.indexOf('function renderYam2()'), ui.indexOf('function y2out('));
const vcmpSource = ui.slice(ui.indexOf('function vcmp('), ui.indexOf('\n// ---------- файлы'));
const elements = {};
const node = id => elements[id] ||= { value: '', checked: true, innerHTML: '', textContent: '' };
const context = {
  $: node,
  $$: () => [],
  esc: x => String(x ?? ''),
  icon: () => '',
  y2: {
    packages: [{ name: 'tangenx/wakeword', version: '1.4.0', target: 'all', services: [] }],
    catalog: {
      configured: true,
      platform: 'yandexstation_2',
      packages: [
        { name: 'tangenx/wakeword', latest: '1.4.0', summary: 'Wake word', fits: true },
        { name: 'sqlite', latest: '3.45.3', summary: 'SQLite', fits: true },
      ],
    },
    status: {},
  },
};
vm.createContext(context);
vm.runInContext(vcmpSource + '\n' + yam2Source + '\nrenderYam2();', context);
const catalog = node('#y2-catalog').innerHTML;
assert.equal((catalog.match(/tangenx\/wakeword/g) || []).length, 1);
assert(catalog.includes('1.4.0'));
assert.equal((catalog.match(/установлено/g) || []).length, 1);
assert(catalog.includes('sqlite'));

context.y2.catalog.packages[0].latest = '1.5.0';
vm.runInContext('renderYam2();', context);
assert(node('#y2-catalog').innerHTML.includes('Обновить с 1.4.0'));

node('#y2-filter').value = 'sqlite';
vm.runInContext('renderYam2();', context);
assert(!node('#y2-catalog').innerHTML.includes('tangenx/wakeword'));
console.log('PASS: yam2 catalog deduplication, installed badge, update prompt and search filter preserved');

(async () => {
  const pkgSource = ui.slice(ui.indexOf('async function yam2Pkg('), ui.indexOf('async function yam2Svc('));
  for (const action of ['install', 'remove']) {
    const calls = [];
    const nav = { hidden: true };
    const ctx = {
      api: async () => ({}), act: async (btn, fn) => fn(), confirm: () => true,
      toast: () => {}, loadYam2Packages: async () => calls.push('packages'),
      window: { wwLoad: async () => { nav.hidden = action !== 'install'; calls.push('wakeword'); } },
    };
    vm.createContext(ctx);
    vm.runInContext(pkgSource, ctx);
    await ctx.yam2Pkg({ textContent: 'Install' }, action, 'tangenx/wakeword@1.2.3');
    assert.deepEqual(calls, ['wakeword', 'packages']);
    assert.equal(nav.hidden, action !== 'install');
    calls.length = 0;
    await ctx.yam2Pkg({ textContent: 'Install' }, action, 'yam/other');
    assert.deepEqual(calls, ['packages']);
    ctx.api = async () => ({ need_yes: true, text: 'confirm' });
    ctx.confirm = () => false;
    ctx.y2out = () => {};
    calls.length = 0;
    await ctx.yam2Pkg({ textContent: 'Install' }, 'install', 'tangenx/wakeword');
    assert.deepEqual(calls, []);
  }
  console.log('PASS: wakeword navigation refreshes immediately after package changes');
})().catch(e => { console.error(e); process.exitCode = 1; });
