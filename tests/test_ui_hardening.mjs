import assert from 'node:assert/strict';
import vm from 'node:vm';
import { readFile } from 'node:fs/promises';

const app = await readFile(new URL('../static/app.js', import.meta.url), 'utf8');
const html = await readFile(new URL('../static/index.html', import.meta.url), 'utf8');

// The mobile bottom bar has at least five visible items; a fixed four-column
// grid wrapped the last one under the viewport.
const mbarRule = html.match(/\.mbar\s*\{([^}]*)\}/)?.[1] || '';
assert(!mbarRule.includes('repeat(4,'), 'mobile bar must not force four columns');
assert(/\.mbar a, \.mbar button\s*\{[^}]*flex:\s*1 1 0/.test(html), 'mobile bar items must share width');
const mbarNav = html.match(/<nav class="mbar"[^>]*>([\s\S]*?)<\/nav>/)?.[1] || '';
for (const tab of ['overview', 'player', 'yt', 'boot', 'party']) {
  assert(mbarNav.includes(`data-tab="${tab}"`), `mobile bar must include ${tab}`);
}
assert(mbarNav.includes('id="mbar-more"'), 'mobile bar must include #mbar-more');
for (const removed of ['id="tab-alarm"', 'id="tab-scenes"', 'id="tab-led"', 'id="tab-radio"', 'id="yam-legacy"', 'id="bt-out-row"', 'id="alarm-bar"']) {
  assert(!html.includes(removed), `removed non-Max UI element still present: ${removed}`);
}

// Every id (and every tab page) must be unique: a duplicated <main> makes the
// browser show two copies of the same page side by side.
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
const duplicateIds = ids.filter((id, index) => ids.indexOf(id) !== index);
assert.deepEqual(duplicateIds, [], `duplicate ids in index.html: ${duplicateIds.join(', ')}`);
const pageIds = [...html.matchAll(/<main[^>]*\bid="(tab-[^"]+)"/g)].map(match => match[1]);
assert.equal(new Set(pageIds).size, pageIds.length, 'each tab page must appear once');

// ---------- 1. Единый visibility-aware планировщик ----------
assert(app.includes('const Scheduler = (() => {'), 'Scheduler must exist');
assert(app.includes("document.addEventListener('visibilitychange'"), 'Scheduler must listen to visibilitychange');
assert(app.includes('document.hidden'), 'Scheduler must check document.hidden');
assert(app.includes('const every = (ms, fn) => Scheduler.add(ms, fn)'), 'every() wrapper must exist');
for (const legacy of [
  'setInterval(updateSub', 'setInterval(loadDispState', 'setInterval(loadMusic',
  'setInterval(refreshStatus', 'setInterval(loadSparks', 'setInterval(loadBt',
  'setInterval(loadParty', 'setInterval(loadAssistant', 'setInterval(loadCharts',
  'setInterval(loadScenes', 'setInterval(loadAlarms', 'setInterval(connCheck',
]) assert(!app.includes(legacy), `legacy timer still present: ${legacy}`);
assert(!app.includes('setTimeout(pollLeds'), 'LED fallback must not reschedule itself');
assert(!app.includes('setTimeout(liveLeds'), 'LED live poll must not reschedule itself');
assert(app.includes('if (!online || document.hidden || !$(\'#tab-party\')') ||
       app.includes("if (!online || document.hidden || !$('#tab-party')"), 'party poll must be visibility-aware');
assert(app.includes("if (!online || document.hidden || $('#tab-assistant').hidden) return;"), 'assistant poll must be visibility-aware');
assert(app.includes("if (!online || document.hidden || shownTab !== 'charts') return;"), 'charts poll must be visibility-aware');

// Исполняем сам планировщик с поддельными часами: скрытая вкладка не будит задачи,
// а при возврате задача выполняется сразу.
const start = app.indexOf('const Scheduler = (() => {');
const blockEnd = '\n})();';
const end = app.indexOf(blockEnd, start);
assert(start >= 0 && end > start, 'scheduler block must be extractable');
const schedulerSource = app.slice(start, end + blockEnd.length) + '\n;globalThis.__Scheduler = Scheduler;';

let now = 0, timers = [], timerSeq = 0;
const fakeSetTimeout = (fn, ms) => {
  const timer = { id: ++timerSeq, at: now + Math.max(0, ms), fn, canceled: false };
  timers.push(timer);
  return timer;
};
const fakeClearTimeout = timer => { if (timer) timer.canceled = true; };
const listeners = {};
const fakeDocument = {
  hidden: false,
  addEventListener(name, fn) { (listeners[name] ||= []).push(fn); },
};
const schedulerContext = {
  document: fakeDocument,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  Date: { now: () => now },
};
vm.createContext(schedulerContext);
vm.runInContext(schedulerSource, schedulerContext);
const Scheduler = schedulerContext.__Scheduler;
let calls = 0;
Scheduler.add(1000, () => { calls++; });
const advance = ms => {
  const target = now + ms;
  for (let guard = 0; guard < 10000; guard++) {
    timers = timers.filter(timer => !timer.canceled);
    const due = timers.filter(timer => timer.at <= target).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = due.at;
    timers = timers.filter(timer => timer !== due);
    due.fn();
  }
  now = target;
};
advance(2500);
assert.equal(calls, 2, 'visible scheduler runs periodic tasks');
fakeDocument.hidden = true;
for (const fn of listeners.visibilitychange || []) fn();
const beforeHidden = calls;
advance(5000);
assert.equal(calls, beforeHidden, 'hidden scheduler must not run tasks');
fakeDocument.hidden = false;
for (const fn of listeners.visibilitychange || []) fn();
advance(0);
assert.equal(calls, beforeHidden + 1, 'visible scheduler catches up immediately');

// ---------- 2. Экранирование пользовательских данных ----------
assert(app.includes('<span>${esc(b.name)}</span>'), 'lamp name must be escaped');
assert(app.includes('data-key="${esc(key)}" data-v="${esc(v)}" data-dots="${esc(dots)}"'), 'secret() attributes must be escaped');
assert(app.includes('aria-label="Редактировать лампу ${esc(b.name)}"'), 'lamp edit button must name the lamp');
assert(app.includes('aria-label="Удалить лампу ${esc(b.name)}"'), 'lamp delete button must name the lamp');
assert(app.includes('esc(e.name)'), 'file name must be escaped');
assert(app.includes('esc(h.question)'), 'assistant history question must be escaped');
assert(app.includes('out.textContent'), 'logs must be rendered as text, not HTML');
assert(app.includes("${tag ? `<span class=\"badge${cur ? ' ok' : ''}\">${esc(tag)}</span>`"), 'track tag must be escaped');
assert(app.includes('cssUrl(url)'), 'cover URL must go through cssUrl()');
for (const raw of [
  '${b.name}</span>', '${x.name}</b>', '${a.label}</b>', '${s.name}</b>',
  '${e.name}</span>', '${h.question}</b>', '${a.query}</div>', '${last.name}</b>',
]) assert(!app.includes(raw), `raw non-escaped template found: ${raw}`);

// ---------- 3. Производительность: списки не перерисовываются зря ----------
assert(app.includes('function setListHtml(el, html)'), 'setListHtml helper must exist');
for (const marker of [
  "setListHtml($('#bt-list')",
  'setListHtml(listEl,', 'setListHtml(body,', "setListHtml($('#sounds')", "setListHtml($('#as-hist')",
]) assert(app.includes(marker), `large list must use setListHtml: ${marker}`);
assert(app.includes('function renderMusicProgress()'), 'progress-only renderer must exist');
assert(app.includes('every(250, () => muState()?.playing && !mu.seeking && renderMusicProgress())'),
  '250 ms tick must update only progress');

// ---------- 4. Доступность и подтверждения ----------
assert(html.includes(':focus-visible {'), 'global focus-visible style must exist');
assert(html.includes('id="toast" role="status" aria-live="polite"'), 'toast must be announced');
assert(html.includes('id="dsp-eq-dialog"') && html.includes('id="dsp-open-eq"'), 'full frequency-response dialog must exist');
assert(app.includes("action: 'set_eq'"), 'EQ dialog must save bands through /api/dsp');
assert(html.includes('id="party-bulb-cancel-x" title="Закрыть" aria-label="Закрыть редактор лампы"'), 'bulb editor close needs aria-label');
assert(html.includes('id="party-scan-close-btn" title="Закрыть" aria-label="Закрыть поиск ламп"'), 'scan close needs aria-label');
assert(html.includes('id="party-import-close-btn" title="Закрыть" aria-label="Закрыть импорт ламп"'), 'import close needs aria-label');
assert(app.includes('function fsAction(a, e)') && /Удалить \$\{e\.is_dir/.test(app), 'fs delete must ask for confirmation');
assert(app.includes('confirm(\'Удалить с колонки всё, что поставила панель?'), 'footprint reset must ask for confirmation');
assert(app.includes('confirm(\'Удалить эту лампу из светомузыки?\')'), 'lamp delete must ask for confirmation');

// ---------- 5. Прокси обложек YouTube / Google CDN (включая gstatic.com) ----------
const thumbStart = app.indexOf('const YT_THUMB_HOST_RE =');
const thumbEnd = app.indexOf('window.proxyThumbUrl = proxyThumbUrl;', thumbStart);
assert(thumbStart >= 0 && thumbEnd > thumbStart, 'proxyThumbUrl block must exist');
const thumbCtx = { URL, encodeURIComponent, window: {} };
vm.createContext(thumbCtx);
vm.runInContext(
  app.slice(thumbStart, thumbEnd + 'window.proxyThumbUrl = proxyThumbUrl;'.length),
  thumbCtx,
);
const likedCover = 'https://www.gstatic.com/youtube/media/ytm/images/pbg/liked-songs-delhi-576.png';
assert.equal(
  thumbCtx.window.proxyThumbUrl(likedCover),
  '/api/extmusic/thumb?url=' + encodeURIComponent(likedCover),
  'gstatic.com Liked Songs cover must be proxied through /api/extmusic/thumb',
);
assert.equal(
  thumbCtx.window.proxyThumbUrl('https://evil-gstatic.com/cover.png'),
  'https://evil-gstatic.com/cover.png',
  'non-allowlisted host must not be proxied',
);

// ---------- 6. Маскировка логов (maskLog) и сбор JS-ошибок ----------
const maskStart = app.indexOf('function maskLog(t)');
const maskEnd = app.indexOf('\nfunction renderLog()', maskStart);
assert(maskStart >= 0 && maskEnd > maskStart, 'maskLog function must exist');
const errStart = app.indexOf('const jsErrors = [];');
const errEnd = app.indexOf("\nwindow.addEventListener('error'", errStart);
assert(errStart >= 0 && errEnd > errStart, 'recordJsError block must exist');
const logCtx = {
  status: { device_id: 'dev1234', ssid: 'MyHomeWifi', address: '192.168.1.50', hostname: 'yandexstation-ABC99' },
  ymLogin: 'my_login_user',
  location: { hash: '#logs' },
  window: {},
};
vm.createContext(logCtx);
vm.runInContext(
  app.slice(errStart, errEnd) + '\n' + app.slice(maskStart, maskEnd) + '\n;globalThis.__maskLog = maskLog; globalThis.__recordJsError = recordJsError; globalThis.__formatJsErrors = formatJsErrors;',
  logCtx,
);
const rawLog = 'sk-proj-secret123456 y0_AgAAAATOKEN123456 192.168.1.99 127.0.0.1:8765 0.369.1.50.479465 me@example.com /home/alex/a puid=987654321 dev1234';
const masked = logCtx.__maskLog(rawLog);
for (const secret of ['secret123456', 'TOKEN123456', '192.168.1.99', 'me@example.com', 'alex', '987654321', 'dev1234']) {
  assert(!masked.includes(secret), `maskLog leaked ${secret}: ${masked}`);
}
assert(masked.includes('127.0.0.1:8765'), 'maskLog must preserve localhost');
assert(masked.includes('0.369.1.50.479465'), 'maskLog must preserve firmware versions');
logCtx.__recordJsError('error', 'boom');
logCtx.__recordJsError('error', 'boom');
assert(logCtx.__formatJsErrors().includes('(×2)'), 'recordJsError must collapse consecutive duplicates');
assert(html.includes('id="log-copy"') && html.includes('data-report="log-report"'), 'Logs tab must expose Copy log and Issue report buttons');
assert(app.includes("const qs = new URLSearchParams({ source: 'yt', track_id: id })"), 'Player artist card must fall back to source=yt/track_id/artist resolution when artistList[0].id is empty');
assert(app.includes('class="btn sm yt-nav" href="${esc(artistHref)}"'), 'YouTube Music artist button must carry .yt-nav class');
assert(app.includes('href="#yt?album=${encodeURIComponent(albBid)}"'), 'Player album badge must link to #yt?album= when album id exists');
assert(html.includes('id="pl-q-save-yt"') && html.includes('id="yt-playlist-new"'), 'UI must expose playlist creation and save-queue buttons');

console.log('Scheduler visibility, escaping, list rendering, ARIA and confirmation cases passed');
