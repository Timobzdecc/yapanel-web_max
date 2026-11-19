// YouTube Music hub for YapAnel (Yandex Station Max).
// Playback lives in unified #minibar and #tab-player; #tab-yt provides catalog,
// navigation, read-only collections, search filters, likes, queue actions, and auth.
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
  const fmt = ms => {
    const total = Math.max(0, Math.round((Number(ms) || 0) / 1000));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
  };

  const SECTIONS = ['home', 'explore', 'search', 'library', 'account'];
  const DEFAULT_YT_ERR = 'Не удалось выполнить запрос к YouTube Music';

  let status = null;
  let activeSection = 'home';
  let navStack = [];
  let currentDetail = null;
  let detailSeq = 0;

  let homeLoaded = false;
  let exploreLoaded = false;
  let chartsCountry = 'RU';
  let chartsSeq = 0;
  let exploreSeq = 0;
  let searchKind = 'songs';
  let searchSeq = 0;

  let libraryData = null;
  let libraryLikedTracks = null;
  let libraryFilter = 'all';
  let libraryLoaded = false;
  let libraryAccount = '';

  const likedSet = new Set();
  let likedLoaded = false;
  let likedLoading = false;

  let ytOauthTimer = null;
  let ytOauthCountdownTimer = null;
  let ytOauthExpiresAt = 0;
  let pollTimer = null;

  let lastRouteKey = '';
  let lastRouteAt = 0;

  const listRegistry = new Map();
  const cardRegistry = new Map();
  let moodRegistry = [];

  function stripExtYt(value) {
    const s = String(value || '').trim();
    return s.startsWith('ext:yt:') ? s.slice(7).trim() : s;
  }

  function trackIdOf(track) {
    if (!track || typeof track !== 'object') return '';
    return stripExtYt(track.id || track.video_id || '');
  }

  function isPlayableTrack(item) {
    if (!item || typeof item !== 'object') return false;
    const kind = String(item.kind || '').trim().toLowerCase();
    if (kind === 'album' || kind === 'artist' || kind === 'playlist' || kind === 'liked') return false;
    if (item.browse_id && !item.video_id && String(item.id || '') === String(item.browse_id)) return false;
    return Boolean(trackIdOf(item));
  }

  function safeImgUrl(url) {
    const s = String(url || '').trim();
    if (!s) return '';
    if (s.startsWith('/api/')) {
      return typeof PanelContext !== 'undefined' && PanelContext?.url ? PanelContext.url(s) : s;
    }
    try {
      const parsed = new URL(s);
      return parsed.protocol === 'https:' ? parsed.href : '';
    } catch {
      return '';
    }
  }

  function safeHttpsUrl(url) {
    const s = String(url || '').trim();
    if (!s) return '';
    try {
      const parsed = new URL(s);
      return parsed.protocol === 'https:' ? parsed.href : '';
    } catch {
      return '';
    }
  }

  function safeHexColor(color) {
    const s = String(color || '').trim();
    return /^#[0-9a-f]{6}$/i.test(s) ? s : '';
  }

  async function call(path, body) {
    if (typeof api === 'function') return body === undefined ? api(path) : api(path, body);
    const init = body === undefined ? {} : {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    };
    if (typeof PanelContext === 'undefined' || typeof PanelContext.fetch !== 'function') {
      throw new Error('PanelContext недоступен');
    }
    const r = await PanelContext.fetch(path, init);
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || r.statusText || ('HTTP ' + r.status));
    return data;
  }

  function notify(text, isErr = false) {
    if (typeof toast === 'function' && text) toast(text, !!isErr);
  }

  function getPanelMu() {
    try {
      if (typeof mu !== 'undefined' && mu) return mu;
    } catch (_) {}
    return typeof window !== 'undefined' ? window.mu || null : null;
  }

  function refreshPanelPlayer() {
    const panelMu = getPanelMu();
    if (panelMu?.info && status && status.current) {
      panelMu.info.extmusic = status;
    }
    if (typeof window !== 'undefined' && typeof window.loadMusic === 'function') {
      try { window.loadMusic(); } catch (_) {}
    }
  }

  function showError(text) {
    const el = $('yt-error');
    if (!el) return;
    el.textContent = text || '';
    el.hidden = !text;
  }

  function setSection(sec, { resetDetail = true, load = true, resetRoute = true } = {}) {
    const next = SECTIONS.includes(sec) ? sec : 'home';
    activeSection = next;
    if (resetDetail) {
      detailSeq++;
      if (resetRoute) lastRouteKey = '';
      navStack = [];
      currentDetail = null;
      const dv = $('yt-detail-view');
      if (dv) dv.hidden = true;
    }

    document.querySelectorAll('#yt-tabs [data-yt-tab]').forEach(btn => {
      const on = btn.dataset.ytTab === next;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });

    const showingDetail = Boolean(currentDetail);
    document.querySelectorAll('#tab-yt [data-yt-sec]').forEach(el => {
      el.hidden = showingDetail || el.dataset.ytSec !== next;
    });

    if (load && !showingDetail) {
      if (next === 'home' && !homeLoaded) loadHome();
      else if (next === 'explore' && !exploreLoaded) loadExplore();
      else if (next === 'library' && !libraryLoaded) loadLibrary();
      else if (next === 'account') { loadStatus(); loadHwStatus(); loadDlnaStatus(); loadMultiroomStatus(); }
    }
  }

  function syncActiveTrackHighlight() {
    const curId = status?.current?.source === 'yt' ? trackIdOf(status.current) : '';
    document.querySelectorAll('#tab-yt .trow[data-yt-vid]').forEach(row => {
      row.classList.toggle('cur', Boolean(curId && row.dataset.ytVid === curId));
    });
  }

  function syncLikeButtons(vid, isLiked) {
    if (!vid) return;
    document.querySelectorAll('#tab-yt .cbtn[data-yt-like]').forEach(btn => {
      if (btn.dataset.ytLike !== vid) return;
      btn.classList.toggle('liked', isLiked);
      btn.setAttribute('aria-pressed', isLiked ? 'true' : 'false');
      const titleText = btn.dataset.ytTitle || '';
      btn.setAttribute('aria-label', (isLiked ? 'Убрать лайк' : 'Лайк') + (titleText ? ': ' + titleText : ''));
    });
  }

  function syncAllLikeButtons() {
    document.querySelectorAll('#tab-yt .cbtn[data-yt-like]').forEach(btn => {
      const vid = btn.dataset.ytLike || '';
      const isLiked = likedSet.has(vid);
      btn.classList.toggle('liked', isLiked);
      btn.setAttribute('aria-pressed', isLiked ? 'true' : 'false');
    });
  }

  function syncLikedFromPanel() {
    const p = getPanelMu()?.info?.glagol?.state?.playerState;
    if (p && typeof p.id === 'string' && p.id.startsWith('ext:yt:') && typeof p.liked === 'boolean') {
      const vid = stripExtYt(p.id);
      if (vid) {
        if (p.liked) likedSet.add(vid);
        else likedSet.delete(vid);
        syncLikeButtons(vid, p.liked);
      }
    }
  }

  function updateAccountHeaderAndDiag() {
    const yt = status?.sources?.yt || {};
    const auth = !!yt.authenticated;
    const authType = yt.auth_type || (yt.oauth ? 'oauth' : yt.cookies ? 'cookies' : '');
    const rawName = yt.account && yt.account !== 'YouTube Music' ? yt.account : '';
    const acctLabel = auth ? (rawName || 'Google-аккаунт') : 'Гость';
    libraryAccount = rawName || libraryAccount;

    const sub = $('yt-acct-sub');
    if (sub) {
      const pipe = yt.gst ? 'GStreamer DSP' : yt.native ? 'Native YT' : 'Каталог';
      sub.textContent = auth
        ? `${acctLabel} (${authType === 'cookies' ? 'cookies.txt' : 'OAuth TV'}) · ${pipe}`
        : `Гость · ${pipe} и публичный каталог`;
    }

    const stopBtn = $('yt-ext-stop');
    if (stopBtn) {
      const cur = status?.current;
      stopBtn.hidden = !cur;
      if (cur) {
        stopBtn.textContent = cur.source === 'vk' ? '■ Стоп VK' : '■ Стоп';
        stopBtn.setAttribute('aria-label', cur.source === 'vk' ? 'Остановить VK Music' : 'Остановить YouTube Music');
      }
    }

    const badge = $('yt-account-badge');
    if (badge) {
      badge.textContent = auth ? (rawName || 'подключён') : 'не подключено';
      badge.className = auth ? 'badge ok' : 'badge';
    }

    const libBadge = $('yt-library-badge');
    if (libBadge) {
      libBadge.textContent = auth ? 'подключён' : 'не подключён';
      libBadge.className = auth ? 'badge ok' : 'badge bad';
    }

    const libAcc = $('yt-library-account');
    if (libAcc) libAcc.textContent = auth ? acctLabel : 'Аккаунт не подключён';

    const newPlBtn = $('yt-playlist-new');
    if (newPlBtn) newPlBtn.hidden = !auth;

    const loginBtn = $('yt-account-login');
    if (loginBtn) loginBtn.hidden = auth;

    const logoutBtn = $('yt-account-logout');
    if (logoutBtn) logoutBtn.hidden = !auth;

    const clearCookiesBtn = $('yt-cookies-clear-btn');
    if (clearCookiesBtn) clearCookiesBtn.hidden = !yt.cookies;

    const diagAcc = $('yt-diag-account');
    if (diagAcc) diagAcc.textContent = auth ? acctLabel : 'Гость (без входа)';

    const diagAuth = $('yt-diag-auth');
    if (diagAuth) {
      diagAuth.textContent = auth
        ? (authType === 'cookies' ? 'cookies.txt (WEB_REMIX)' : 'QR OAuth (TVHTML5)')
        : 'Не подключён (публичный доступ)';
    }

    const diagPipe = $('yt-diag-pipeline');
    if (diagPipe) {
      const parts = [];
      if (yt.gst) parts.push('GStreamer (DSP Станции)');
      if (yt.native) parts.push('Native YT (резерв)');
      diagPipe.textContent = parts.length ? parts.join(' · ') : (yt.configured ? 'yt-dlp поток' : 'Недоступен');
    }

    const diagPlay = $('yt-diag-playback');
    if (diagPlay) {
      const cur = status?.current;
      const p = status?.player || {};
      if (cur) {
        const srcName = cur.source === 'vk' ? 'VK Music' : (cur.source === 'dlna' ? 'DLNA / Поток' : 'YouTube Music');
        const stName = p.playing ? 'играет' : p.state === 'paused' ? 'пауза' : p.state || 'активен';
        const qLen = Array.isArray(status?.queue) ? status.queue.length : 0;
        const extras = [];
        if (qLen > 1) extras.push(`очередь ${qLen}`);
        if (status?.shuffle) extras.push('перемешано');
        if (status?.repeat && status.repeat !== 'none') extras.push(`повтор: ${status.repeat}`);
        if (status?.autoplay_radio) extras.push('авто-радио');
        diagPlay.textContent = `${srcName}: ${cur.title || 'Без названия'} (${stName})`
          + (extras.length ? ` · ${extras.join(', ')}` : '');
      } else {
        diagPlay.textContent = 'Ожидание';
      }
    }
  }

  let hwState = null;

  function renderHwStatus(hw) {
    hwState = hw || null;
    const card = $('yt-hw-card');
    if (!card) return;
    if (!hw || !hw.supported) {
      card.hidden = true;
      return;
    }
    card.hidden = false;
    const r = hw.remote || {};
    const o = hw.osd || {};
    const badge = $('yt-hw-badge');
    if (badge) {
      badge.textContent = r.grabbed ? 'пульт активен' : (r.connected ? 'пульт подключён' : 'Макс');
      badge.className = 'badge ' + (r.grabbed ? 'ok' : (r.connected ? 'ok' : ''));
    }
    const rChk = $('yt-hw-remote');
    if (rChk) rChk.checked = !!r.enabled;
    const dChk = $('yt-hw-dpad-hdmi');
    if (dChk) dChk.checked = !!r.dpad_hdmi;
    const oChk = $('yt-hw-osd');
    if (oChk) oChk.checked = !!o.enabled;

    const rMeta = $('yt-hw-remote-meta');
    if (rMeta) {
      if (!r.supported) {
        rMeta.textContent = 'Локальный ввод недоступен на этом хосте';
      } else if (r.connected) {
        const dev = r.device_name ? `${r.device_name} (${r.device_path || 'evdev'})` : (r.device_path || 'BLE-пульт');
        const stateTxt = !r.enabled
          ? 'выключено'
          : r.grabbed
            ? 'активен для YouTube Music'
            : 'ожидает запуск YouTube Music';
        rMeta.textContent = `${dev} · ${stateTxt}`;
      } else {
        rMeta.textContent = r.enabled ? 'BLE-пульт не найден (ожидание подключения)' : 'Выключено';
      }
    }

    const oMeta = $('yt-hw-osd-meta');
    if (oMeta) {
      if (!o.supported) oMeta.textContent = 'Матрица 25×16 недоступна';
      else if (o.active) oMeta.textContent = 'Идёт показ на экране…';
      else oMeta.textContent = o.enabled ? 'Включено (не мешает таймеру и эквалайзеру)' : 'Выключено';
    }
  }

  async function loadHwStatus() {
    if (!$('yt-hw-card')) return;
    try {
      const hw = await request('/api/extmusic/hw');
      renderHwStatus(hw);
    } catch (_) {
      // Optional Station Max capability
    }
  }

  async function updateHw(patch, msg) {
    try {
      const hw = await request('/api/extmusic/hw', patch);
      renderHwStatus(hw);
      if (msg) notify(msg);
    } catch (err) {
      notify(err.message || 'Не удалось обновить настройки железа', true);
      loadHwStatus();
    }
  }

  function renderDlnaStatus(st) {
    const card = $('yt-dlna-card');
    if (!card || !st) return;
    const badge = $('yt-dlna-badge');
    if (badge) {
      badge.textContent = st.running ? `порт ${st.port}` : (st.enabled ? 'ожидание' : 'выкл');
      badge.className = 'badge ' + (st.running ? 'ok' : '');
    }
    const chk = $('yt-dlna-enabled');
    if (chk) chk.checked = !!st.enabled;
    const meta = $('yt-dlna-meta');
    if (meta) {
      if (st.running && st.device_url) {
        meta.textContent = `${st.friendly_name || 'Яндекс Станция Макс'} · ${st.device_url}`;
      } else {
        meta.textContent = st.enabled ? 'Запускается DLNA MediaRenderer…' : 'DLNA выключен (прямой запуск потоков доступен)';
      }
    }
  }

  async function loadDlnaStatus() {
    if (!$('yt-dlna-card')) return;
    try {
      renderDlnaStatus(await request('/api/extmusic/dlna'));
    } catch (_) {}
  }

  async function updateDlna(patch, msg) {
    try {
      const res = await request('/api/extmusic/dlna', patch);
      renderDlnaStatus(res);
      if (res?.playback) {
        status = res.playback;
        renderStatus();
      }
      if (msg) notify(msg);
    } catch (err) {
      notify(err.message || 'Ошибка DLNA / потока', true);
      loadDlnaStatus();
    }
  }

  function renderMultiroomStatus(mr) {
    const card = $('yt-multiroom-card');
    if (!card || !mr) return;
    const followers = Array.isArray(mr.followers) ? mr.followers : [];
    const activeCount = followers.filter(f => f.enabled).length;
    const badge = $('yt-mr-badge');
    if (badge) {
      badge.textContent = mr.enabled ? `активен (${activeCount})` : (followers.length ? `ведомых: ${followers.length}` : 'Выкл');
      badge.className = 'badge ' + (mr.enabled ? 'ok' : '');
    }
    const chk = $('yt-mr-enabled');
    if (chk) chk.checked = !!mr.enabled;
    const volChk = $('yt-mr-sync-vol');
    if (volChk) volChk.checked = !!mr.sync_volume;

    const listEl = $('yt-mr-list');
    if (listEl) {
      if (!followers.length) {
        const sugs = Array.isArray(mr.suggestions) ? mr.suggestions : [];
        if (sugs.length) {
          listEl.innerHTML = `<div class="meta">Найдено в speakers.json: ${sugs.map(s => `<button type="button" class="btn sm" data-mr-suggest-url="${esc(s.url)}" data-mr-suggest-name="${esc(s.name)}">+ ${esc(s.name)}</button>`).join(' ')}</div>`;
        } else {
          listEl.innerHTML = '<div class="meta">Ведомые колонки пока не добавлены.</div>';
        }
      } else {
        listEl.innerHTML = followers.map(f => {
          const stTxt = f.last_status
            ? (f.last_status === 'ok' ? `OK (${f.latency_ms || 0}мс)` : esc(f.last_status))
            : 'готова';
          return `<div class="row" style="gap:8px;align-items:center;justify-content:space-between;padding:6px 10px;background:var(--field,#151515);border-radius:10px">`
            + `<label class="row" style="gap:8px;align-items:center;cursor:pointer;min-width:0">`
            + `<input type="checkbox" data-mr-toggle="${esc(f.id)}" ${f.enabled ? 'checked' : ''}>`
            + `<span><b>${esc(f.name)}</b> <span class="mono meta">${esc(f.url)}</span>`
            + `${f.offset_ms ? ` <span class="meta">(${f.offset_ms > 0 ? '+' : ''}${f.offset_ms}мс)</span>` : ''} · <span class="meta">${stTxt}</span></span>`
            + `</label>`
            + `<button type="button" class="btn sm danger-soft" data-mr-remove="${esc(f.id)}" aria-label="Удалить колонку">✕</button>`
            + `</div>`;
        }).join('');
      }
    }
  }

  async function loadMultiroomStatus() {
    if (!$('yt-multiroom-card')) return;
    try {
      renderMultiroomStatus(await request('/api/extmusic/multiroom'));
    } catch (_) {}
  }

  async function updateMultiroom(patch, msg) {
    try {
      const res = await request('/api/extmusic/multiroom', patch);
      renderMultiroomStatus(res);
      if (msg) notify(msg);
    } catch (err) {
      notify(err.message || 'Ошибка настройки мультирума', true);
      loadMultiroomStatus();
    }
  }

  function renderStatus() {
    if (!status) return;
    syncLikedFromPanel();
    updateAccountHeaderAndDiag();
    syncActiveTrackHighlight();
    if (status.error) showError(status.error);
    else showError('');

    const skipped = Array.isArray(status.skipped) ? status.skipped : [];
    if (skipped.length) {
      const key = skipped.map(item => `${item.id || ''}:${item.reason || ''}`).join('|');
      if (key && key !== renderStatus.lastSkippedKey) {
        renderStatus.lastSkippedKey = key;
        notify(`Пропущен «${skipped[0].title || 'трек'}»: ${skipped[0].reason || 'недоступен'}`, true);
      }
    } else {
      renderStatus.lastSkippedKey = '';
    }
  }
  renderStatus.lastSkippedKey = '';

  async function refreshLikedIds() {
    if (likedLoading) return;
    likedLoading = true;
    try {
      const data = await call('/api/extmusic/yt/account/liked?count=200');
      const items = Array.isArray(data?.tracks) ? data.tracks : [];
      likedSet.clear();
      for (const tr of items) {
        const vid = trackIdOf(tr);
        if (vid) likedSet.add(vid);
      }
      likedLoaded = true;
      syncAllLikeButtons();
      syncLikedFromPanel();
    } catch (_) {
      // Non-fatal: individual rows still work and surface auth errors when clicked.
    } finally {
      likedLoading = false;
    }
  }

  async function loadStatus() {
    try {
      status = await call('/api/extmusic/status');
      renderStatus();
      const auth = !!status?.sources?.yt?.authenticated;
      if (auth && !likedLoaded) refreshLikedIds();
      if (!auth && likedLoaded) {
        likedSet.clear();
        likedLoaded = false;
        syncAllLikeButtons();
      }
      return status;
    } catch (e) {
      if (e && e.offline) return null;
      showError(e.message || 'YouTube Music недоступен');
      return null;
    }
  }

  function trackListHtml(listKey, items, { showRank = false, emptyText = 'Ничего не найдено' } = {}) {
    const tracks = (Array.isArray(items) ? items : []).filter(isPlayableTrack);
    listRegistry.set(listKey, tracks);
    if (!tracks.length) return `<div class="empty">${esc(emptyText)}</div>`;

    const curId = status?.current?.source === 'yt' ? trackIdOf(status.current) : '';
    return tracks.map((track, idx) => {
      const vid = trackIdOf(track);
      const isCur = Boolean(curId && curId === vid);
      const isLiked = likedSet.has(vid);
      const cover = safeImgUrl(track.thumb || track.cover);
      const title = track.title || 'Без названия';
      const artist = track.artist || 'YouTube Music';
      const artistId = stripExtYt(track.artist_id || '');
      const artistHtml = artistId
        ? `<a class="yt-nav" href="#yt?artist=${encodeURIComponent(artistId)}" data-yt-artist="${esc(artistId)}">${esc(artist)}</a>`
        : esc(artist);
      const albumId = stripExtYt(track.album_id || '');
      const albumHtml = track.album
        ? (albumId
          ? `<a class="yt-nav" href="#yt?album=${encodeURIComponent(albumId)}" data-yt-album="${esc(albumId)}">${esc(track.album)}</a>`
          : esc(track.album))
        : '';
      const subHtml = artistHtml + (albumHtml ? ` · ${albumHtml}` : '');
      const dur = Number(track.duration) > 0 ? fmt(Number(track.duration) * 1000) : '';

      return `<div class="trow trow-with-acts${isCur ? ' cur' : ''}" data-yt-vid="${esc(vid)}">
        <div class="trow-main" role="button" tabindex="0" data-yt-play="${esc(listKey)}" data-yt-idx="${idx}" aria-label="Включить «${esc(title)}»">
          ${showRank ? `<span class="yt-rank">${idx + 1}</span>` : ''}
          ${cover
            ? `<img src="${esc(cover)}" alt="" loading="lazy">`
            : '<span class="ph" aria-hidden="true"><svg width="18" height="18"><use href="#i-music"/></svg></span>'}
          <span class="tt"><b>${esc(title)}</b><span>${subHtml}</span></span>
          ${dur ? `<span class="td">${esc(dur)}</span>` : ''}
        </div>
        <span class="trow-acts">
          <button type="button" class="cbtn sm" data-yt-radio-track="${esc(listKey)}" data-yt-idx="${idx}" title="Радио по треку" aria-label="Радио по треку: ${esc(title)}">📻</button>
          <button type="button" class="cbtn sm" data-yt-pl-add="${esc(vid)}" data-yt-title="${esc(title)}" title="Добавить в плейлист" aria-label="Добавить в плейлист: ${esc(title)}">➕</button>
          <button type="button" class="cbtn sm${isLiked ? ' liked' : ''}" data-yt-like="${esc(vid)}" data-yt-title="${esc(title)}" aria-pressed="${isLiked ? 'true' : 'false'}" title="Нравится" aria-label="${isLiked ? 'Убрать лайк' : 'Лайк'}: ${esc(title)}"><svg width="15" height="15" aria-hidden="true"><use href="#i-like"/></svg></button>
          <button type="button" class="cbtn sm" data-yt-next="${esc(listKey)}" data-yt-idx="${idx}" title="Играть следующим" aria-label="Играть следующим: ${esc(title)}">⏭+</button>
          <button type="button" class="cbtn sm" data-yt-queue="${esc(listKey)}" data-yt-idx="${idx}" title="Добавить в очередь" aria-label="Добавить в очередь: ${esc(title)}">☰+</button>
        </span>
      </div>`;
    }).join('');
  }

  function cardGridHtml(listKey, items, { emptyText = 'Пусто' } = {}) {
    const cards = (Array.isArray(items) ? items : []).filter(c => c && typeof c === 'object' && (c.browse_id || c.kind === 'liked'));
    cardRegistry.set(listKey, cards);
    if (!cards.length) return `<div class="empty">${esc(emptyText)}</div>`;

    return cards.map((card, idx) => {
      const kind = String(card.kind || (String(card.browse_id || '').startsWith('UC') ? 'artist' : 'playlist')).toLowerCase();
      const isArtist = kind === 'artist';
      const cover = safeImgUrl(card.thumb || card.cover);
      const title = card.title || 'Без названия';
      const fallbackSub = isArtist ? 'Исполнитель' : kind === 'album' ? 'Альбом' : kind === 'liked' ? 'Плейлист' : 'Плейлист';
      const sub = card.subtitle || [card.artist, card.year].filter(Boolean).join(' · ') || fallbackSub;

      return `<button type="button" class="yt-tile${isArtist ? ' yt-tile-artist' : ''}" data-yt-card="${esc(listKey)}" data-yt-idx="${idx}">
        <div class="yt-tile-cover">
          ${cover
            ? `<img src="${esc(cover)}" alt="" loading="lazy"${isArtist ? ' class="yt-artist-circle"' : ''}>`
            : `<span class="yt-cover-ph${isArtist ? ' yt-artist-circle' : ''}" aria-hidden="true"><svg width="28" height="28"><use href="#i-music"/></svg></span>`}
        </div>
        <span class="yt-tile-title">${esc(title)}</span>
        <span class="yt-tile-sub">${esc(sub)}</span>
      </button>`;
    }).join('');
  }

  async function playTrack(track, queue = [], btn = null) {
    if (!track) return false;
    if (btn) btn.setAttribute('aria-busy', 'true');
    showError('');
    try {
      const normalizedQueue = (Array.isArray(queue) && queue.length ? queue : [track]).map(t => ({ ...t, source: 'yt' }));
      const payloadTrack = { ...track, source: 'yt' };
      status = await call('/api/extmusic/play', { track: payloadTrack, queue: normalizedQueue });
      renderStatus();
      refreshPanelPlayer();
      notify(`Включаю: ${track.title || 'YouTube Music'}`);
      return true;
    } catch (e) {
      const msg = e?.message || 'Ошибка воспроизведения';
      showError(msg);
      notify(msg, true);
      return false;
    } finally {
      if (btn) btn.removeAttribute('aria-busy');
    }
  }

  async function stopPlayback(btn = null) {
    if (btn) btn.disabled = true;
    showError('');
    try {
      status = await call('/api/extmusic/stop', {});
      renderStatus();
      refreshPanelPlayer();
      notify('Воспроизведение остановлено');
    } catch (e) {
      const msg = e?.message || 'Не удалось остановить';
      showError(msg);
      notify(msg, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function toggleLike(vid) {
    const cleanId = stripExtYt(vid);
    if (!cleanId) return;
    const prev = likedSet.has(cleanId);
    const next = !prev;
    if (next) likedSet.add(cleanId);
    else likedSet.delete(cleanId);
    syncLikeButtons(cleanId, next);
    showError('');

    try {
      const res = await call('/api/extmusic/yt/like', { id: cleanId, like: next });
      const confirmed = typeof res?.liked === 'boolean' ? res.liked : next;
      if (confirmed) likedSet.add(cleanId);
      else likedSet.delete(cleanId);
      syncLikeButtons(cleanId, confirmed);
      if (libraryFilter === 'liked') libraryLoaded = false;
      refreshPanelPlayer();
      notify(confirmed ? '♥ Добавлено в «Мне нравится»' : 'Лайк убран');
    } catch (e) {
      if (prev) likedSet.add(cleanId);
      else likedSet.delete(cleanId);
      syncLikeButtons(cleanId, prev);
      const msg = e?.message || 'Не удалось обновить лайк';
      showError(msg);
      notify(msg, true);
    }
  }

  async function queueAdd(track, mode, btn = null) {
    if (!track) return;
    if (btn) btn.disabled = true;
    showError('');
    try {
      status = await call('/api/extmusic/queue/add', { track: { ...track, source: 'yt' }, mode });
      renderStatus();
      refreshPanelPlayer();
      notify(mode === 'next'
        ? `Сыграет следующим: ${track.title || 'трек'}`
        : `Добавлено в очередь: ${track.title || 'трек'}`);
    } catch (e) {
      const msg = e?.message || 'Не удалось добавить в очередь';
      showError(msg);
      notify(msg, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function playShuffle(tracks, btn = null) {
    const list = (Array.isArray(tracks) ? tracks : []).filter(isPlayableTrack);
    if (!list.length) return;
    const start = list[Math.floor(Math.random() * list.length)];
    if (!(await playTrack(start, list, btn))) return;
    if (!status?.native) {
      try {
        status = await call('/api/extmusic/shuffle', { enable: true });
        renderStatus();
        refreshPanelPlayer();
      } catch (_) {}
    }
  }

  async function playRadio(opts = {}, btn = null) {
    if (btn) btn.disabled = true;
    showError('');
    try {
      status = await call('/api/extmusic/radio', opts || {});
      renderStatus();
      refreshPanelPlayer();
      const title = opts?.track?.title;
      notify(title ? `Радио по треку: ${title}` : 'Радио запущено');
    } catch (e) {
      const msg = e?.message || 'Не удалось запустить радио';
      showError(msg);
      notify(msg, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function createPlaylistPrompt(videoIds = [], defaultTitle = '') {
    const raw = typeof prompt === 'function'
      ? prompt('Название нового плейлиста YouTube Music:', defaultTitle || '')
      : '';
    const title = String(raw || '').trim();
    if (!title) return null;
    showError('');
    try {
      const body = { title };
      if (Array.isArray(videoIds) && videoIds.length) body.video_ids = videoIds;
      const res = await call('/api/extmusic/yt/playlist/create', body);
      libraryLoaded = false;
      if (activeSection === 'library' && !currentDetail) loadLibrary(true);
      notify(`Плейлист «${res?.title || title}» создан`);
      return res;
    } catch (e) {
      const msg = e?.message || 'Не удалось создать плейлист';
      showError(msg);
      notify(msg, true);
      return null;
    }
  }

  async function addTrackToPlaylistPrompt(vid, title = '') {
    const cleanId = stripExtYt(vid);
    if (!cleanId) return;
    if (!status?.sources?.yt?.authenticated) {
      notify('Подключите аккаунт Google или cookies во вкладке «Аккаунт»', true);
      return;
    }
    showError('');
    try {
      if (!libraryData || !Array.isArray(libraryData.playlists)) {
        try {
          libraryData = await call('/api/extmusic/yt/account/library');
          libraryLoaded = true;
        } catch (_) {}
      }
      const playlists = (Array.isArray(libraryData?.playlists) ? libraryData.playlists : [])
        .filter(pl => {
          const bid = String(pl?.playlist_id || pl?.browse_id || '');
          return bid && bid !== 'VLLM' && bid !== 'LM' && !bid.startsWith('MPREb_');
        });
      if (!playlists.length) {
        await createPlaylistPrompt([cleanId], title || 'Мой плейлист');
        return;
      }
      const lines = playlists.slice(0, 15).map((pl, idx) => `${idx + 1}. ${pl.title || 'Без названия'}`);
      lines.push('0. + Создать новый плейлист');
      const hint = `Добавить «${title || 'трек'}» в плейлист:\n${lines.join('\n')}`;
      const choiceRaw = typeof prompt === 'function' ? prompt(hint, '1') : '';
      if (choiceRaw === null || String(choiceRaw).trim() === '') return;
      const choice = Number(String(choiceRaw).trim());
      if (choice === 0) {
        await createPlaylistPrompt([cleanId], title || 'Новый плейлист');
        return;
      }
      const picked = Number.isInteger(choice) && choice >= 1 && choice <= Math.min(playlists.length, 15)
        ? playlists[choice - 1]
        : playlists.find(pl => String(pl.title || '').toLowerCase() === String(choiceRaw).trim().toLowerCase());
      if (!picked) {
        notify('Неверный номер плейлиста', true);
        return;
      }
      const targetId = picked.playlist_id || picked.browse_id;
      await call('/api/extmusic/yt/playlist/add', { playlist_id: targetId, video_ids: [cleanId] });
      notify(`Добавлено в «${picked.title || 'плейлист'}»`);
    } catch (e) {
      const msg = e?.message || 'Не удалось добавить в плейлист';
      showError(msg);
      notify(msg, true);
    }
  }

  if (typeof window !== 'undefined') {
    window.ytInvalidateLibrary = () => { libraryLoaded = false; };
  }

  // ---------- Home ----------
  async function loadHome() {
    const qpBox = $('yt-home-qp');
    if (qpBox && !homeLoaded) qpBox.innerHTML = '<div class="empty">Загрузка подборки…</div>';
    try {
      const data = await call('/api/extmusic/yt/home?limit=30');
      homeLoaded = true;

      const warn = $('yt-home-warn');
      const warnText = $('yt-home-warn-text');
      if (warn && warnText) {
        warn.hidden = !data?.account_error;
        warnText.textContent = data?.account_error || '';
      }

      const recent = Array.isArray(data?.recent) ? data.recent : [];
      const recentWrap = $('yt-home-recent-wrap');
      const recentBox = $('yt-home-recent');
      const recentCount = $('yt-home-recent-count');
      if (recentWrap && recentBox) {
        recentWrap.hidden = !recent.length;
        if (recentCount) recentCount.textContent = recent.length ? `${recent.length} треков` : '';
        if (recent.length) recentBox.innerHTML = trackListHtml('home-recent', recent);
      }

      const qp = Array.isArray(data?.quick_picks) && data.quick_picks.length
        ? data.quick_picks
        : (Array.isArray(data?.tracks) ? data.tracks : []);
      if (qpBox) {
        qpBox.innerHTML = trackListHtml('home-qp', qp, { emptyText: 'Подборка пока пуста' });
      }

      let mixes = Array.isArray(data?.mixes) ? data.mixes : [];
      if (!mixes.length && Array.isArray(data?.shelves)) {
        mixes = data.shelves
          .flatMap(s => Array.isArray(s?.items) ? s.items : [])
          .filter(it => it && (it.kind === 'album' || it.kind === 'playlist' || it.kind === 'artist'));
      }
      const mixesWrap = $('yt-home-mixes-wrap');
      const mixesBox = $('yt-home-mixes');
      const mixesMeta = $('yt-home-mixes-meta');
      if (mixesWrap && mixesBox) {
        mixesWrap.hidden = !mixes.length;
        if (mixesMeta) mixesMeta.textContent = mixes.length ? `${mixes.length}` : '';
        if (mixes.length) mixesBox.innerHTML = cardGridHtml('home-mixes', mixes);
      }

      const feed = Array.isArray(data?.feed) ? data.feed : [];
      const feedWrap = $('yt-home-feed-wrap');
      const feedBox = $('yt-home-feed');
      if (feedWrap && feedBox) {
        feedWrap.hidden = !feed.length;
        if (feed.length) feedBox.innerHTML = trackListHtml('home-feed', feed);
      }
    } catch (e) {
      if (qpBox) qpBox.innerHTML = `<div class="empty">${esc(e?.message || DEFAULT_YT_ERR)}</div>`;
    }
  }

  // ---------- Explore & Charts ----------
  function renderChartsData(charts) {
    const tracksBox = $('yt-charts-tracks');
    const tracks = Array.isArray(charts?.tracks) ? charts.tracks : [];
    if (tracksBox) {
      tracksBox.innerHTML = trackListHtml('explore-charts', tracks, {
        showRank: true,
        emptyText: 'Чарты временно недоступны',
      });
    }

    const artists = Array.isArray(charts?.artists) ? charts.artists : [];
    const artWrap = $('yt-charts-artists-wrap');
    const artBox = $('yt-charts-artists');
    if (artWrap && artBox) {
      artWrap.hidden = !artists.length;
      if (artists.length) artBox.innerHTML = cardGridHtml('explore-artists', artists);
    }

    const videos = Array.isArray(charts?.videos) ? charts.videos : [];
    const vidWrap = $('yt-charts-videos-wrap');
    const vidBox = $('yt-charts-videos');
    if (vidWrap && vidBox) {
      vidWrap.hidden = !videos.length;
      if (videos.length) {
        const videoTracks = videos.filter(isPlayableTrack);
        const videoCards = videos.filter(v => v && v.browse_id && !isPlayableTrack(v));
        if (videoTracks.length) {
          vidBox.className = 'tlist';
          vidBox.innerHTML = trackListHtml('explore-videos', videoTracks);
        } else {
          vidBox.className = 'yt-shelf';
          vidBox.innerHTML = cardGridHtml('explore-video-cards', videoCards);
        }
      }
    }
  }

  function syncCountryButtons() {
    document.querySelectorAll('#yt-charts-countries [data-yt-country]').forEach(btn => {
      const on = btn.dataset.ytCountry === chartsCountry;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  async function loadCharts(country = chartsCountry) {
    chartsCountry = country || 'RU';
    syncCountryButtons();
    const seq = ++chartsSeq;
    const tracksBox = $('yt-charts-tracks');
    if (tracksBox) tracksBox.innerHTML = '<div class="empty">Загрузка чартов…</div>';
    try {
      const data = await call('/api/extmusic/yt/charts?country=' + encodeURIComponent(chartsCountry));
      if (seq !== chartsSeq) return;
      renderChartsData(data || {});
    } catch (e) {
      if (seq !== chartsSeq) return;
      if (tracksBox) tracksBox.innerHTML = `<div class="empty">${esc(e?.message || 'Ошибка загрузки чартов')}</div>`;
    }
  }

  async function loadExplore() {
    syncCountryButtons();
    const cSeq = ++chartsSeq;
    const eSeq = ++exploreSeq;
    const relBox = $('yt-explore-releases');
    const moodsBox = $('yt-explore-moods');
    const chartsBox = $('yt-charts-tracks');
    if (chartsBox && !exploreLoaded) chartsBox.innerHTML = '<div class="empty">Загрузка чартов…</div>';
    if (relBox && !exploreLoaded) relBox.innerHTML = '<div class="empty">Загрузка новинок…</div>';
    if (moodsBox && !exploreLoaded) moodsBox.innerHTML = '<div class="empty">Загрузка…</div>';

    const [chartsRes, exploreRes] = await Promise.allSettled([
      call('/api/extmusic/yt/charts?country=' + encodeURIComponent(chartsCountry)),
      call('/api/extmusic/yt/explore'),
    ]);
    exploreLoaded = true;

    if (cSeq === chartsSeq) {
      if (chartsRes.status === 'fulfilled') {
        renderChartsData(chartsRes.value || {});
      } else if (chartsBox) {
        chartsBox.innerHTML = `<div class="empty">${esc(chartsRes.reason?.message || 'Чарты недоступны')}</div>`;
      }
    }

    if (eSeq !== exploreSeq) return;
    if (exploreRes.status === 'fulfilled') {
      const exp = exploreRes.value || {};
      const releases = Array.isArray(exp.releases) ? exp.releases : [];
      if (relBox) relBox.innerHTML = cardGridHtml('explore-releases', releases, { emptyText: 'Нет новых релизов' });

      const moods = Array.isArray(exp.moods) ? exp.moods : [];
      moodRegistry = moods;
      if (moodsBox) {
        moodsBox.innerHTML = moods.length
          ? moods.map((m, idx) => {
              const color = safeHexColor(m?.color);
              return `<button type="button" class="yt-mood" data-yt-mood="${idx}">
                <span class="yt-mood-dot"${color ? ` data-color="${esc(color)}"` : ''}></span>
                <span>${esc(m?.title || 'Подборка')}</span>
              </button>`;
            }).join('')
          : '<div class="empty">Категории не найдены</div>';
        moodsBox.querySelectorAll('.yt-mood-dot[data-color]').forEach(dot => {
          const c = safeHexColor(dot.dataset.color);
          if (c) dot.style.background = c;
        });
      }
    } else {
      if (relBox) relBox.innerHTML = `<div class="empty">${esc(exploreRes.reason?.message || 'Новые релизы недоступны')}</div>`;
      if (moodsBox) moodsBox.innerHTML = '<div class="empty">Настроения недоступны</div>';
    }
  }

  // ---------- Search ----------
  async function runSearch(event) {
    if (event) event.preventDefault();
    const input = $('yt-q');
    const q = (input?.value || '').trim();
    const box = $('yt-search-results');
    if ($('yt-q-clear')) $('yt-q-clear').hidden = !q;

    if (q.length < 2) {
      searchSeq++;
      if (box) box.innerHTML = '<div class="empty">Введите хотя бы два символа для поиска</div>';
      return;
    }

    const seq = ++searchSeq;
    showError('');
    if (box) {
      box.className = '';
      box.innerHTML = '<div class="empty">Ищу…</div>';
    }

    try {
      const data = await call(
        '/api/extmusic/yt/search?kind=' + encodeURIComponent(searchKind) + '&q=' + encodeURIComponent(q)
      );
      if (seq !== searchSeq) return;
      const items = Array.isArray(data?.items)
        ? data.items
        : (Array.isArray(data?.tracks) ? data.tracks : []);
      if (!box) return;
      if (searchKind === 'albums' || searchKind === 'artists' || searchKind === 'playlists') {
        box.className = 'yt-cards';
        box.innerHTML = cardGridHtml('search-cards', items, { emptyText: 'Ничего не найдено' });
      } else {
        box.className = 'tlist';
        box.innerHTML = trackListHtml('search-tracks', items, { emptyText: 'Ничего не найдено' });
      }
    } catch (e) {
      if (seq !== searchSeq) return;
      const msg = e?.message || 'Ошибка поиска';
      showError(msg);
      if (box) {
        box.className = '';
        box.innerHTML = `<div class="empty">${esc(msg)}</div>`;
      }
    }
  }

  function setSearchKind(kind) {
    searchKind = kind || 'songs';
    document.querySelectorAll('#yt-search-filters [data-yt-kind]').forEach(btn => {
      const on = btn.dataset.ytKind === searchKind;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    const q = ($('yt-q')?.value || '').trim();
    if (q.length >= 2) runSearch();
  }

  // ---------- Read-only Library ----------
  function renderLibrary() {
    const box = $('yt-library-list');
    const stateEl = $('yt-library-state');
    const filters = $('yt-library-filters');
    if (!box) return;

    const auth = !!status?.sources?.yt?.authenticated || !!libraryData?.configured;
    if (filters) filters.hidden = !auth;

    document.querySelectorAll('#yt-library-filters [data-lib-filter]').forEach(btn => {
      const on = btn.dataset.libFilter === libraryFilter;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });

    if (!auth || !libraryData) {
      box.className = '';
      box.innerHTML = '<div class="empty">Подключите аккаунт Google или cookies.txt во вкладке «Аккаунт», чтобы видеть понравившуюся музыку, плейлисты и исполнителей.</div>';
      if (stateEl) stateEl.textContent = '';
      return;
    }

    if (libraryFilter === 'liked') {
      const tracks = Array.isArray(libraryLikedTracks) ? libraryLikedTracks : [];
      box.className = 'col';
      if (!libraryLikedTracks) {
        box.innerHTML = '<div class="empty">Загрузка понравившихся треков…</div>';
        return;
      }
      const head = tracks.length
        ? `<div class="row" style="gap:8px;flex-wrap:wrap">
             <button type="button" class="btn sm primary" id="yt-lib-liked-play">▶ Играть всё</button>
             ${tracks.length > 1 ? '<button type="button" class="btn sm" id="yt-lib-liked-shuffle">🔀 Перемешать</button>' : ''}
             <span class="meta">${tracks.length} треков</span>
           </div>`
        : '';
      box.innerHTML = `${head}<div class="tlist">${trackListHtml('lib-liked', tracks, { emptyText: 'Список понравившихся треков пуст' })}</div>`;
      if (stateEl) stateEl.textContent = '';
      return;
    }

    const playlists = Array.isArray(libraryData.playlists) ? libraryData.playlists : [];
    const artists = Array.isArray(libraryData.artists) ? libraryData.artists : [];
    const likedCard = libraryData.liked
      ? [{ ...libraryData.liked, kind: 'liked', browse_id: libraryData.liked.browse_id || 'VLLM' }]
      : [];

    if (libraryFilter === 'playlists') {
      box.className = 'yt-cards';
      box.innerHTML = cardGridHtml('lib-playlists', [...likedCard, ...playlists], { emptyText: 'Плейлисты не найдены' });
      if (stateEl) stateEl.textContent = `${playlists.length + likedCard.length} элементов`;
      return;
    }

    if (libraryFilter === 'artists') {
      box.className = 'yt-cards';
      box.innerHTML = cardGridHtml('lib-artists', artists, { emptyText: 'Исполнители не найдены' });
      if (stateEl) stateEl.textContent = artists.length ? `${artists.length} исполнителей` : '';
      return;
    }

    // 'all': Liked + Playlists + Artists
    const sections = [];
    const colItems = [...likedCard, ...playlists];
    if (colItems.length) {
      sections.push(`<div class="col" style="gap:10px">
        <h3 class="yt-detail-subhead">Плейлисты и понравившееся</h3>
        <div class="yt-cards">${cardGridHtml('lib-all-pl', colItems)}</div>
      </div>`);
    }
    if (artists.length) {
      sections.push(`<div class="col" style="gap:10px">
        <h3 class="yt-detail-subhead">Исполнители</h3>
        <div class="yt-cards">${cardGridHtml('lib-all-art', artists)}</div>
      </div>`);
    }
    box.className = 'col';
    box.innerHTML = sections.length ? sections.join('') : '<div class="empty">Медиатека пуста</div>';
    if (stateEl) stateEl.textContent = '';
  }

  async function loadLibrary(force = false) {
    const stateEl = $('yt-library-state');
    if (stateEl) stateEl.textContent = 'Загружаю библиотеку…';
    try {
      const acc = await call('/api/extmusic/yt/account');
      if (!acc?.configured) {
        libraryData = null;
        libraryLoaded = true;
        if (stateEl) stateEl.textContent = '';
        renderLibrary();
        return;
      }
      libraryAccount = acc.account || libraryAccount;
      const data = await call('/api/extmusic/yt/account/library');
      libraryData = data;
      libraryAccount = data?.account || libraryAccount;
      libraryLoaded = true;
      if (libraryFilter === 'liked' && (!libraryLikedTracks || force)) {
        await loadLibraryLiked();
      } else {
        if (stateEl) stateEl.textContent = '';
        renderLibrary();
      }
    } catch (e) {
      libraryLoaded = true;
      if (stateEl) stateEl.textContent = e?.message || 'Библиотека недоступна';
      renderLibrary();
    }
  }

  async function loadLibraryLiked() {
    const stateEl = $('yt-library-state');
    if (stateEl) stateEl.textContent = 'Загружаю понравившиеся…';
    renderLibrary();
    try {
      const data = await call('/api/extmusic/yt/account/liked?count=200');
      const tracks = Array.isArray(data?.tracks) ? data.tracks : [];
      libraryLikedTracks = tracks;
      likedSet.clear();
      for (const tr of tracks) {
        const vid = trackIdOf(tr);
        if (vid) likedSet.add(vid);
      }
      likedLoaded = true;
      if (stateEl) stateEl.textContent = '';
      renderLibrary();
    } catch (e) {
      libraryLikedTracks = [];
      if (stateEl) stateEl.textContent = e?.message || 'Не удалось загрузить треки';
      renderLibrary();
    }
  }

  // ---------- Detail View (Album / Playlist / Artist / Mood / Liked) ----------
  function renderDetailView(detail) {
    currentDetail = detail;
    const dv = $('yt-detail-view');
    const heroEl = $('yt-detail-hero');
    const bodyEl = $('yt-detail-body');
    const crumbEl = $('yt-detail-crumb');
    if (!dv || !heroEl || !bodyEl) return;

    document.querySelectorAll('#tab-yt [data-yt-sec]').forEach(el => { el.hidden = true; });
    dv.hidden = false;

    const kind = String(detail.kind || 'playlist').toLowerCase();
    const isArtist = kind === 'artist';
    const kickerMap = {
      artist: 'ИСПОЛНИТЕЛЬ',
      album: 'АЛЬБОМ',
      playlist: 'ПЛЕЙЛИСТ',
      liked: 'МНЕ НРАВИТСЯ',
      mood: 'ПОДБОРКА',
    };
    const kicker = kickerMap[kind] || 'КОЛЛЕКЦИЯ';
    const title = detail.title || 'Без названия';
    const subtitle = detail.subtitle || '';
    const description = detail.description || '';
    const cover = safeImgUrl(detail.thumb || detail.cover);
    const tracks = Array.isArray(detail.tracks) ? detail.tracks : [];
    const albums = Array.isArray(detail.albums) ? detail.albums : [];
    const similar = Array.isArray(detail.similar) ? detail.similar : [];
    const collections = Array.isArray(detail.collections) ? detail.collections : [];
    const canRadio = Boolean(
      (kind !== 'mood' && detail.browse_id && !String(detail.browse_id).startsWith('FEmusic_'))
      || tracks.length
    );

    if (crumbEl) crumbEl.textContent = kicker;

    heroEl.innerHTML = `<div class="yt-hero">
      ${cover
        ? `<img class="yt-hero-cover${isArtist ? ' yt-artist-circle' : ''}" src="${esc(cover)}" alt="">`
        : `<div class="yt-hero-ph${isArtist ? ' yt-artist-circle' : ''}" aria-hidden="true"><svg width="48" height="48"><use href="#i-music"/></svg></div>`}
      <div class="yt-hero-body">
        <div class="pl-kicker">${esc(kicker)}</div>
        <h2 class="yt-hero-title">${esc(title)}</h2>
        ${subtitle ? `<div class="yt-hero-sub">${esc(subtitle)}</div>` : ''}
        ${description ? `<p class="hint">${esc(description)}</p>` : ''}
        <div class="yt-hero-acts">
          ${tracks.length ? '<button type="button" class="btn primary" id="yt-detail-play">▶ Играть всё</button>' : ''}
          ${tracks.length > 1 ? '<button type="button" class="btn" id="yt-detail-shuffle">🔀 Перемешать</button>' : ''}
          ${canRadio ? '<button type="button" class="btn" id="yt-detail-radio">📻 Радио</button>' : ''}
        </div>
      </div>
    </div>`;

    if (detail.loading) {
      bodyEl.innerHTML = '<div class="empty">Загрузка коллекции…</div>';
      return;
    }

    const parts = [];
    if (tracks.length) {
      parts.push(`<div class="col" style="gap:8px">
        <h3 class="yt-detail-subhead">${isArtist ? 'Популярные треки' : `Треки (${tracks.length})`}</h3>
        <div class="tlist">${trackListHtml('detail-tracks', tracks)}</div>
      </div>`);
    }
    if (collections.length) {
      parts.push(`<div class="col" style="gap:8px">
        <h3 class="yt-detail-subhead">Плейлисты и подборки</h3>
        <div class="yt-cards">${cardGridHtml('detail-collections', collections)}</div>
      </div>`);
    }
    if (albums.length) {
      parts.push(`<div class="col" style="gap:8px">
        <h3 class="yt-detail-subhead">Альбомы и синглы</h3>
        <div class="yt-cards">${cardGridHtml('detail-albums', albums)}</div>
      </div>`);
    }
    if (similar.length) {
      parts.push(`<div class="col" style="gap:8px">
        <h3 class="yt-detail-subhead">Похожие исполнители</h3>
        <div class="yt-cards">${cardGridHtml('detail-similar', similar.map(s => ({ ...s, kind: s.kind || 'artist' })))}</div>
      </div>`);
    }
    bodyEl.innerHTML = parts.length ? parts.join('') : '<div class="empty">В этой коллекции нет доступных треков</div>';
  }

  function pushCurrentToStack() {
    if (currentDetail) {
      navStack.push({ type: 'detail', detail: currentDetail });
    } else {
      navStack.push({ type: 'section', section: activeSection });
    }
  }

  async function openLiked(preview = {}, { pushHistory = true } = {}) {
    if (pushHistory) pushCurrentToStack();
    const seq = ++detailSeq;
    showError('');
    renderDetailView({
      kind: 'liked',
      browse_id: 'VLLM',
      title: preview.title || 'Понравившаяся музыка',
      subtitle: preview.subtitle || 'Ваши лайки в YouTube Music',
      thumb: preview.thumb || '',
      tracks: [],
      loading: true,
    });
    try {
      const data = await call('/api/extmusic/yt/account/liked?count=200');
      if (seq !== detailSeq) return;
      const tracks = Array.isArray(data?.tracks) ? data.tracks : [];
      likedSet.clear();
      for (const tr of tracks) {
        const vid = trackIdOf(tr);
        if (vid) likedSet.add(vid);
      }
      likedLoaded = true;
      libraryLikedTracks = tracks;
      renderDetailView({
        kind: 'liked',
        browse_id: 'VLLM',
        title: preview.title || 'Понравившаяся музыка',
        subtitle: `${tracks.length} треков`,
        thumb: preview.thumb || (tracks[0]?.thumb || ''),
        tracks,
        loading: false,
      });
    } catch (e) {
      if (seq !== detailSeq) return;
      const msg = e?.message || 'Не удалось загрузить понравившееся';
      showError(msg);
      renderDetailView({
        kind: 'liked',
        browse_id: 'VLLM',
        title: preview.title || 'Понравившаяся музыка',
        subtitle: msg,
        thumb: preview.thumb || '',
        tracks: [],
        loading: false,
      });
    }
  }

  async function openCollection(opts = {}, { pushHistory = true } = {}) {
    const browseId = stripExtYt(opts.browseId || opts.browse_id || '');
    if (!browseId) return;
    if (browseId === 'VLLM' || browseId === 'FEmusic_liked_videos' || opts.kind === 'liked') {
      return openLiked(opts, { pushHistory });
    }

    if (pushHistory) pushCurrentToStack();
    const seq = ++detailSeq;
    showError('');
    renderDetailView({
      kind: opts.kind || (browseId.startsWith('UC') ? 'artist' : browseId.startsWith('MPREb_') ? 'album' : 'playlist'),
      browse_id: browseId,
      title: opts.title || 'Загрузка…',
      subtitle: opts.subtitle || '',
      thumb: opts.thumb || '',
      tracks: [],
      loading: true,
    });

    try {
      let url = '/api/extmusic/yt/collection?browse_id=' + encodeURIComponent(browseId);
      if (opts.params) url += '&params=' + encodeURIComponent(opts.params);
      const col = await call(url);
      if (seq !== detailSeq) return;
      renderDetailView({
        ...col,
        kind: col.kind || opts.kind || (browseId.startsWith('UC') ? 'artist' : 'playlist'),
        browse_id: col.browse_id || browseId,
        loading: false,
      });
    } catch (e) {
      if (seq !== detailSeq) return;
      const msg = e?.message || 'Не удалось загрузить коллекцию';
      showError(msg);
      renderDetailView({
        kind: opts.kind || 'playlist',
        browse_id: browseId,
        title: opts.title || 'Ошибка загрузки',
        subtitle: msg,
        thumb: opts.thumb || '',
        tracks: [],
        loading: false,
      });
    }
  }

  async function openMood(mood = {}, { pushHistory = true } = {}) {
    if (!mood) return;
    if (pushHistory) pushCurrentToStack();
    const seq = ++detailSeq;
    showError('');
    renderDetailView({
      kind: 'mood',
      browse_id: mood.browse_id || '',
      title: mood.title || 'Подборка',
      subtitle: 'Настроение и жанр',
      tracks: [],
      collections: [],
      loading: true,
    });

    try {
      let res;
      if (!mood.browse_id && !mood.params && !mood.continuation && mood.token) {
        res = await call('/api/extmusic/yt/account/mood', { token: mood.token, count: 100 });
      } else {
        const qs = new URLSearchParams();
        if (mood.browse_id) qs.set('browse_id', mood.browse_id);
        if (mood.params) qs.set('params', mood.params);
        if (mood.continuation) qs.set('continuation', mood.continuation);
        res = await call('/api/extmusic/yt/mood?' + qs.toString());
      }
      if (seq !== detailSeq) return;
      const tracks = Array.isArray(res?.tracks) ? res.tracks : [];
      const collections = Array.isArray(res?.collections) ? res.collections : [];
      renderDetailView({
        kind: 'mood',
        browse_id: mood.browse_id || '',
        title: res?.title || mood.title || 'Подборка',
        subtitle: [tracks.length ? `${tracks.length} треков` : '', collections.length ? `${collections.length} подборок` : '']
          .filter(Boolean).join(' · '),
        thumb: tracks[0]?.thumb || collections[0]?.thumb || '',
        tracks,
        collections,
        loading: false,
      });
    } catch (e) {
      if (seq !== detailSeq) return;
      const msg = e?.message || 'Не удалось загрузить подборку';
      showError(msg);
      renderDetailView({
        kind: 'mood',
        title: mood.title || 'Подборка',
        subtitle: msg,
        tracks: [],
        collections: [],
        loading: false,
      });
    }
  }

  function goBack() {
    detailSeq++;
    lastRouteKey = '';
    const prev = navStack.pop();
    if (prev && prev.type === 'detail' && prev.detail) {
      renderDetailView(prev.detail);
      return;
    }
    const targetSec = prev?.section || activeSection || 'home';
    currentDetail = null;
    if (location.hash.startsWith('#yt?') && typeof history !== 'undefined' && history.replaceState) {
      try { history.replaceState(null, '', '#yt'); } catch (_) {}
    }
    setSection(targetSec, { resetDetail: true, load: true });
  }

  // ---------- Deep-linking (#yt?artist=..., #yt?browse=...) ----------
  function handleRoute(queryStr = '') {
    const clean = String(queryStr || '').replace(/^#?(?:yt|vk)\??/i, '');
    if (!clean) {
      if (!currentDetail) setSection(activeSection, { resetDetail: false, load: true });
      return;
    }
    const now = Date.now();
    if (clean === lastRouteKey && now - lastRouteAt < 500) return;
    lastRouteKey = clean;
    lastRouteAt = now;

    const params = new URLSearchParams(clean);
    const artist = stripExtYt(params.get('artist') || '');
    const browse = stripExtYt(params.get('browse') || params.get('collection') || params.get('album') || params.get('playlist') || '');
    const browseParams = params.get('params') || '';
    const sec = params.get('section') || params.get('tab') || '';
    const q = params.get('q') || '';

    if (artist) {
      openCollection({ browseId: artist, params: browseParams, kind: 'artist' }, { pushHistory: true });
      return;
    }
    if (browse) {
      openCollection({ browseId: browse, params: browseParams }, { pushHistory: true });
      return;
    }
    if (q) {
      const kind = params.get('kind') || 'songs';
      setSection('search', { resetDetail: true, load: false, resetRoute: false });
      const input = $('yt-q');
      if (input) input.value = q;
      setSearchKind(kind);
      if (q.trim().length >= 2) runSearch();
      return;
    }
    if (sec && SECTIONS.includes(sec)) {
      setSection(sec, { resetDetail: true, load: true, resetRoute: false });
    }
  }

  // ---------- OAuth (RFC 8628 Device Flow) & Cookies ----------
  function switchYtAuthTab(tab) {
    const cookiesBtn = $('yt-tab-cookies-btn');
    const oauthBtn = $('yt-tab-oauth-btn');
    const cookiesPanel = $('yt-auth-cookies-panel');
    const oauthPanel = $('yt-auth-oauth-panel');
    if (tab === 'cookies') {
      if (cookiesBtn) { cookiesBtn.className = 'btn primary'; cookiesBtn.setAttribute('aria-pressed', 'true'); }
      if (oauthBtn) { oauthBtn.className = 'btn'; oauthBtn.setAttribute('aria-pressed', 'false'); }
      if (cookiesPanel) cookiesPanel.hidden = false;
      if (oauthPanel) oauthPanel.hidden = true;
    } else {
      if (cookiesBtn) { cookiesBtn.className = 'btn'; cookiesBtn.setAttribute('aria-pressed', 'false'); }
      if (oauthBtn) { oauthBtn.className = 'btn primary'; oauthBtn.setAttribute('aria-pressed', 'true'); }
      if (cookiesPanel) cookiesPanel.hidden = true;
      if (oauthPanel) oauthPanel.hidden = false;
    }
  }

  function renderYtQr(url) {
    const qrBox = $('yt-oauth-qr');
    if (!qrBox || !url) return;
    if (typeof qrcode === 'function') {
      try {
        const q = qrcode(0, 'M');
        q.addData(url);
        q.make();
        qrBox.innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
        return;
      } catch (_) {}
    }
    qrBox.innerHTML = '<div class="empty">QR-код готов к переходу</div>';
  }

  function updateOauthTimerText() {
    const el = $('yt-oauth-timer');
    if (!el) return;
    if (!ytOauthExpiresAt) {
      el.textContent = '';
      return;
    }
    const leftSec = Math.max(0, Math.round((ytOauthExpiresAt - Date.now()) / 1000));
    el.textContent = leftSec ? `Осталось ${fmt(leftSec * 1000)}` : 'Код истёк';
  }

  function stopYtOauthPoll() {
    if (ytOauthTimer) {
      clearInterval(ytOauthTimer);
      ytOauthTimer = null;
    }
    if (ytOauthCountdownTimer) {
      clearInterval(ytOauthCountdownTimer);
      ytOauthCountdownTimer = null;
    }
    ytOauthExpiresAt = 0;
    updateOauthTimerText();
  }

  async function pollYtOauth() {
    try {
      const res = await call('/api/extmusic/yt/login');
      const step = res?.step || 'idle';
      const pollStatus = $('yt-oauth-poll-status');
      if (step === 'code') {
        if (pollStatus) pollStatus.textContent = res.message || 'Ожидание подтверждения на телефоне...';
        const qrUrl = res.qr_url || res.verification_url_complete || (res.verification_url && res.user_code ? `${res.verification_url}?user_code=${res.user_code}` : '');
        const safeQrUrl = safeHttpsUrl(qrUrl);
        if (safeQrUrl) {
          const linkEl = $('yt-oauth-link');
          if (linkEl && !linkEl.getAttribute('href')) linkEl.href = safeQrUrl;
          if ($('yt-oauth-qr') && !$('yt-oauth-qr').querySelector('svg')) renderYtQr(safeQrUrl);
        }
      } else if (step === 'done') {
        stopYtOauthPoll();
        const prompt = $('yt-oauth-prompt');
        if (prompt) prompt.hidden = true;
        $('yt-oauth-start-btn')?.removeAttribute('disabled');
        if ($('yt-oauth-cancel-btn')) $('yt-oauth-cancel-btn').hidden = true;
        if ($('yt-oauth-state')) $('yt-oauth-state').textContent = res.message || 'Вход выполнен!';
        likedLoaded = false;
        libraryLoaded = false;
        homeLoaded = false;
        await loadStatus();
        await loadLibrary(true);
        notify('Google-аккаунт успешно подключён!');
      } else if (step === 'expired' || step === 'error') {
        stopYtOauthPoll();
        $('yt-oauth-start-btn')?.removeAttribute('disabled');
        if ($('yt-oauth-cancel-btn')) $('yt-oauth-cancel-btn').hidden = true;
        if (pollStatus) pollStatus.textContent = res.message || 'Сессия истекла или произошла ошибка';
        if ($('yt-oauth-state')) $('yt-oauth-state').textContent = res.message || '';
      }
    } catch (e) {
      stopYtOauthPoll();
      $('yt-oauth-start-btn')?.removeAttribute('disabled');
      if ($('yt-oauth-cancel-btn')) $('yt-oauth-cancel-btn').hidden = true;
      if ($('yt-oauth-state')) $('yt-oauth-state').textContent = e?.message || 'Ошибка проверки';
    }
  }

  async function startYtOauth() {
    const clientId = ($('yt-oauth-client-id')?.value || '').trim();
    const clientSecret = ($('yt-oauth-client-secret')?.value || '').trim();
    const state = $('yt-oauth-state');
    if (state) state.textContent = 'Запрашиваю QR-код…';
    $('yt-oauth-start-btn')?.setAttribute('disabled', 'true');
    try {
      const res = await call('/api/extmusic/yt/login/start', {
        client_id: clientId,
        client_secret: clientSecret,
      });
      if (res?.user_code) {
        const prompt = $('yt-oauth-prompt');
        if (prompt) prompt.hidden = false;
        if ($('yt-oauth-code')) $('yt-oauth-code').textContent = res.user_code;
        const verUrl = res.verification_url || 'https://www.google.com/device';
        if ($('yt-oauth-url')) $('yt-oauth-url').textContent = verUrl;

        const qrTargetUrl = res.qr_url || res.verification_url_complete || `${verUrl}?user_code=${res.user_code}`;
        const safeHref = safeHttpsUrl(qrTargetUrl);
        const safeFallback = safeHttpsUrl(verUrl);
        const linkEl = $('yt-oauth-link');
        if (linkEl && (safeHref || safeFallback)) linkEl.href = safeHref || safeFallback;

        if (safeHref || safeFallback) renderYtQr(safeHref || safeFallback);
        if ($('yt-oauth-cancel-btn')) $('yt-oauth-cancel-btn').hidden = false;
        if (state) state.textContent = '';

        const expMs = res.expires_at ? Number(res.expires_at) * 1000 : Date.now() + (Number(res.expires_in) || 1800) * 1000;
        const interval = Math.max(3, Number(res.interval) || 5) * 1000;
        stopYtOauthPoll();
        ytOauthExpiresAt = expMs;
        updateOauthTimerText();
        ytOauthCountdownTimer = setInterval(updateOauthTimerText, 1000);
        ytOauthTimer = setInterval(pollYtOauth, interval);
      }
    } catch (e) {
      $('yt-oauth-start-btn')?.removeAttribute('disabled');
      if (state) state.textContent = e?.message || 'Не удалось запросить код';
    }
  }

  async function cancelYtOauth() {
    stopYtOauthPoll();
    const prompt = $('yt-oauth-prompt');
    if (prompt) prompt.hidden = true;
    const qrBox = $('yt-oauth-qr');
    if (qrBox) qrBox.innerHTML = '<div class="empty">QR-код…</div>';
    const linkEl = $('yt-oauth-link');
    if (linkEl) linkEl.removeAttribute('href');
    $('yt-oauth-start-btn')?.removeAttribute('disabled');
    if ($('yt-oauth-cancel-btn')) $('yt-oauth-cancel-btn').hidden = true;
    try { await call('/api/extmusic/yt/login/cancel', {}); } catch (_) {}
    if ($('yt-oauth-state')) $('yt-oauth-state').textContent = 'Вход отменён';
  }

  async function saveCookiesText() {
    const paste = ($('yt-cookies-paste')?.value || '').trim();
    const state = $('yt-cookies-state');
    if (!paste) {
      if (state) state.textContent = 'Вставьте текст cookies или выберите файл';
      return;
    }
    if (state) state.textContent = 'Сохраняю cookies…';
    try {
      await call('/api/extmusic/yt/cookies', { cookies: paste });
      if ($('yt-cookies-paste')) $('yt-cookies-paste').value = '';
      if (state) state.textContent = 'Cookies сохранены!';
      likedLoaded = false;
      libraryLoaded = false;
      homeLoaded = false;
      await loadStatus();
      await loadLibrary(true);
      notify('Cookies YouTube Music сохранены');
    } catch (e) {
      if (state) state.textContent = e?.message || 'Ошибка сохранения cookies';
    }
  }

  async function importCookies(event) {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    const state = $('yt-cookies-state');
    if (state) state.textContent = 'Читаю файл…';
    try {
      const text = await file.text();
      await call('/api/extmusic/yt/cookies', { cookies: text });
      event.target.value = '';
      if (state) state.textContent = 'Cookies импортированы!';
      likedLoaded = false;
      libraryLoaded = false;
      homeLoaded = false;
      await loadStatus();
      await loadLibrary(true);
      notify('Cookies YouTube Music импортированы');
    } catch (e) {
      if (state) state.textContent = e?.message || 'Не удалось импортировать cookies';
    }
  }

  async function clearCookies() {
    if (typeof confirm === 'function' && !confirm('Удалить cookies YouTube Music?')) return;
    const state = $('yt-cookies-state');
    if (state) state.textContent = 'Удаляю…';
    try {
      await call('/api/extmusic/yt/cookies/clear', {});
      likedSet.clear();
      likedLoaded = false;
      libraryLoaded = false;
      homeLoaded = false;
      await loadStatus();
      await loadLibrary(true);
      if (state) state.textContent = 'Cookies удалены';
      notify('Cookies YouTube Music удалены');
    } catch (e) {
      if (state) state.textContent = e?.message || 'Не удалось удалить cookies';
    }
  }

  async function logoutYtAccount() {
    const account = libraryAccount || ($('yt-diag-account')?.textContent || 'Google');
    if (typeof confirm === 'function' && !confirm(`Выйти из аккаунта ${account}? Личная медиатека будет отключена.`)) return;
    try {
      await call('/api/extmusic/yt/account/logout', {});
      likedSet.clear();
      likedLoaded = false;
      libraryData = null;
      libraryLikedTracks = null;
      libraryLoaded = false;
      homeLoaded = false;
      notify('Аккаунт YouTube Music отключён');
      await loadStatus();
      await loadLibrary(true);
      refreshPanelPlayer();
    } catch (e) {
      showError(e?.message || 'Не удалось выйти из аккаунта');
    }
  }

  function scheduleStatusPoll() {
    if (pollTimer) clearTimeout(pollTimer);
    if (document.hidden || $('tab-yt')?.hidden) return;
    pollTimer = setTimeout(async () => {
      if (!document.hidden && !$('tab-yt')?.hidden && status?.current) {
        await loadStatus();
      }
      scheduleStatusPoll();
    }, 3000);
  }

  function installImageFallback() {
    window.addEventListener('error', e => {
      const img = e.target;
      if (!img || img.tagName !== 'IMG') return;
      if (!img.closest('#tab-yt') || img.id === 'yt-screen-img') return;
      e.stopPropagation();

      const isArtist = img.classList.contains('yt-artist-circle');
      const isHero = img.classList.contains('yt-hero-cover');
      const isTile = Boolean(img.closest('.yt-tile-cover'));
      const ph = document.createElement(isHero ? 'div' : 'span');
      ph.className = isHero ? 'yt-hero-ph' : isTile ? 'yt-cover-ph' : 'ph';
      if (isArtist) ph.classList.add('yt-artist-circle');
      ph.setAttribute('aria-hidden', 'true');

      const size = isHero ? '48' : isTile ? '28' : '18';
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('width', size);
      svg.setAttribute('height', size);
      const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
      use.setAttribute('href', '#i-music');
      svg.appendChild(use);
      ph.appendChild(svg);
      img.replaceWith(ph);
    }, true);
  }

  function bindDelegatedEvents() {
    const tabYt = $('tab-yt');
    if (!tabYt) return;

    tabYt.addEventListener('click', e => {
      const artistLink = e.target.closest('[data-yt-artist]');
      if (artistLink) {
        e.preventDefault();
        e.stopPropagation();
        const aid = stripExtYt(artistLink.dataset.ytArtist);
        if (aid) openCollection({ browseId: aid, kind: 'artist', title: artistLink.textContent || '' });
        return;
      }

      const albumLink = e.target.closest('[data-yt-album]');
      if (albumLink) {
        e.preventDefault();
        e.stopPropagation();
        const albId = stripExtYt(albumLink.dataset.ytAlbum);
        if (albId) openCollection({ browseId: albId, kind: 'album', title: albumLink.textContent || '' });
        return;
      }

      const plAddBtn = e.target.closest('[data-yt-pl-add]');
      if (plAddBtn) {
        e.stopPropagation();
        addTrackToPlaylistPrompt(plAddBtn.dataset.ytPlAdd, plAddBtn.dataset.ytTitle || '');
        return;
      }

      const likeBtn = e.target.closest('[data-yt-like]');
      if (likeBtn) {
        e.stopPropagation();
        toggleLike(likeBtn.dataset.ytLike);
        return;
      }

      const radioBtn = e.target.closest('[data-yt-radio-track]');
      if (radioBtn) {
        e.stopPropagation();
        const items = listRegistry.get(radioBtn.dataset.ytRadioTrack) || [];
        const tr = items[Number(radioBtn.dataset.ytIdx)];
        if (tr) playRadio({ track: { ...tr, source: 'yt' } }, radioBtn);
        return;
      }

      const nextBtn = e.target.closest('[data-yt-next]');
      if (nextBtn) {
        e.stopPropagation();
        const items = listRegistry.get(nextBtn.dataset.ytNext) || [];
        const tr = items[Number(nextBtn.dataset.ytIdx)];
        if (tr) queueAdd(tr, 'next', nextBtn);
        return;
      }

      const queueBtn = e.target.closest('[data-yt-queue]');
      if (queueBtn) {
        e.stopPropagation();
        const items = listRegistry.get(queueBtn.dataset.ytQueue) || [];
        const tr = items[Number(queueBtn.dataset.ytIdx)];
        if (tr) queueAdd(tr, 'end', queueBtn);
        return;
      }

      const playEl = e.target.closest('[data-yt-play]');
      if (playEl) {
        const items = listRegistry.get(playEl.dataset.ytPlay) || [];
        const tr = items[Number(playEl.dataset.ytIdx)];
        if (tr) playTrack(tr, items, playEl);
        return;
      }

      const cardEl = e.target.closest('[data-yt-card]');
      if (cardEl) {
        const cards = cardRegistry.get(cardEl.dataset.ytCard) || [];
        const card = cards[Number(cardEl.dataset.ytIdx)];
        if (card) openCollection(card);
        return;
      }

      const moodEl = e.target.closest('[data-yt-mood]');
      if (moodEl) {
        const mood = moodRegistry[Number(moodEl.dataset.ytMood)];
        if (mood) openMood(mood);
        return;
      }

      if (e.target.closest('#yt-detail-play')) {
        if (currentDetail?.tracks?.length) playTrack(currentDetail.tracks[0], currentDetail.tracks, e.target.closest('button'));
        return;
      }
      if (e.target.closest('#yt-detail-shuffle')) {
        if (currentDetail?.tracks?.length) playShuffle(currentDetail.tracks, e.target.closest('button'));
        return;
      }
      if (e.target.closest('#yt-detail-radio')) {
        const btn = e.target.closest('button');
        const bid = currentDetail?.browse_id || '';
        const kind = currentDetail?.kind || '';
        if (bid && kind !== 'mood' && !bid.startsWith('FEmusic_')) {
          playRadio({ browse_id: bid }, btn);
        } else if (currentDetail?.tracks?.[0]) {
          playRadio({ track: { ...currentDetail.tracks[0], source: 'yt' } }, btn);
        }
        return;
      }
      if (e.target.closest('#yt-lib-liked-play')) {
        if (libraryLikedTracks?.length) playTrack(libraryLikedTracks[0], libraryLikedTracks, e.target.closest('button'));
        return;
      }
      if (e.target.closest('#yt-lib-liked-shuffle')) {
        if (libraryLikedTracks?.length) playShuffle(libraryLikedTracks, e.target.closest('button'));
      }
    });

    tabYt.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const playEl = e.target.closest('.trow-main[data-yt-play]');
      if (!playEl || e.target.closest('a, button')) return;
      e.preventDefault();
      const items = listRegistry.get(playEl.dataset.ytPlay) || [];
      const tr = items[Number(playEl.dataset.ytIdx)];
      if (tr) playTrack(tr, items, playEl);
    });
  }

  function init() {
    if (!$('tab-yt')) return;

    installImageFallback();
    bindDelegatedEvents();

    // Section switcher (.seg)
    $('yt-tabs')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-yt-tab]');
      if (!btn) return;
      if (location.hash.startsWith('#yt?') && typeof history !== 'undefined' && history.replaceState) {
        try { history.replaceState(null, '', '#yt'); } catch (_) {}
      }
      lastRouteKey = '';
      setSection(btn.dataset.ytTab, { resetDetail: true, load: true });
    });

    // Header stop & detail back
    $('yt-ext-stop')?.addEventListener('click', e => stopPlayback(e.currentTarget));
    $('yt-detail-back')?.addEventListener('click', goBack);

    // Home actions
    $('yt-mixes-refresh')?.addEventListener('click', () => { homeLoaded = false; loadHome(); });
    $('yt-home-refresh')?.addEventListener('click', () => { homeLoaded = false; loadHome(); });
    $('yt-home-warn-account')?.addEventListener('click', () => setSection('account'));

    // Explore actions
    $('yt-charts-countries')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-yt-country]');
      if (btn) loadCharts(btn.dataset.ytCountry);
    });
    $('yt-explore-refresh')?.addEventListener('click', () => { exploreLoaded = false; loadExplore(); });

    // Search form & filters
    $('yt-search-form')?.addEventListener('submit', runSearch);
    $('yt-q')?.addEventListener('input', () => {
      if ($('yt-q-clear')) $('yt-q-clear').hidden = !$('yt-q')?.value;
    });
    $('yt-q-clear')?.addEventListener('click', () => {
      searchSeq++;
      const input = $('yt-q');
      if (input) {
        input.value = '';
        input.focus();
      }
      if ($('yt-q-clear')) $('yt-q-clear').hidden = true;
      const box = $('yt-search-results');
      if (box) {
        box.className = '';
        box.innerHTML = '<div class="empty">Введите запрос для поиска</div>';
      }
    });
    $('yt-search-filters')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-yt-kind]');
      if (btn) setSearchKind(btn.dataset.ytKind);
    });

    // Library actions
    $('yt-playlist-new')?.addEventListener('click', () => createPlaylistPrompt());
    $('yt-library-refresh')?.addEventListener('click', () => { libraryLoaded = false; loadLibrary(true); });
    $('yt-account-login')?.addEventListener('click', () => setSection('account'));
    $('yt-library-filters')?.addEventListener('click', e => {
      const btn = e.target.closest('[data-lib-filter]');
      if (!btn) return;
      libraryFilter = btn.dataset.libFilter || 'all';
      if (libraryFilter === 'liked' && !libraryLikedTracks) loadLibraryLiked();
      else renderLibrary();
    });

    // Account OAuth + cookies
    $('yt-tab-cookies-btn')?.addEventListener('click', () => switchYtAuthTab('cookies'));
    $('yt-tab-oauth-btn')?.addEventListener('click', () => switchYtAuthTab('oauth'));
    $('yt-cookies-file')?.addEventListener('change', importCookies);
    $('yt-cookies-save-btn')?.addEventListener('click', saveCookiesText);
    $('yt-cookies-clear-btn')?.addEventListener('click', clearCookies);
    $('yt-oauth-start-btn')?.addEventListener('click', startYtOauth);
    $('yt-oauth-cancel-btn')?.addEventListener('click', cancelYtOauth);
    $('yt-account-logout')?.addEventListener('click', logoutYtAccount);

    // Station Max hardware integration (BLE remote + LED matrix OSD)
    $('yt-hw-remote')?.addEventListener('change', e => {
      updateHw({ remote_enabled: !!e.target.checked }, e.target.checked ? 'Пульт ДУ для YouTube Music включён' : 'Пульт ДУ для YouTube Music выключен');
    });
    $('yt-hw-dpad-hdmi')?.addEventListener('change', e => {
      updateHw({ remote_dpad_hdmi: !!e.target.checked });
    });
    $('yt-hw-osd')?.addEventListener('change', e => {
      updateHw({ osd_enabled: !!e.target.checked }, e.target.checked ? 'OSD на экране 25×16 включён' : 'OSD на экране 25×16 выключен');
    });
    $('yt-hw-test-heart')?.addEventListener('click', () => updateHw({ test_osd: 'heart' }, 'Показ ♥ на экране 25×16'));
    $('yt-hw-test-title')?.addEventListener('click', () => updateHw({ test_osd: 'track' }, 'Показ бегущей строки на экране 25×16'));

    // DLNA / Cast controls
    $('yt-dlna-enabled')?.addEventListener('change', e => {
      updateDlna({ enabled: !!e.target.checked }, e.target.checked ? 'DLNA MediaRenderer включён' : 'DLNA MediaRenderer выключен');
    });
    $('yt-cast-play-btn')?.addEventListener('click', () => {
      const url = ($('yt-cast-url')?.value || '').trim();
      const title = ($('yt-cast-title')?.value || '').trim();
      if (!url) { notify('Введите http:// или https:// ссылку на аудиопоток', true); return; }
      updateDlna({ action: 'cast', url, title }, 'Запуск аудиопотока…');
    });

    // Multiroom controls
    $('yt-mr-enabled')?.addEventListener('change', e => {
      updateMultiroom({ enabled: !!e.target.checked }, e.target.checked ? 'Мультирум включён' : 'Мультирум выключен');
    });
    $('yt-mr-sync-vol')?.addEventListener('change', e => {
      updateMultiroom({ sync_volume: !!e.target.checked });
    });
    $('yt-mr-add-btn')?.addEventListener('click', () => {
      const url = ($('yt-mr-add-url')?.value || '').trim();
      const name = ($('yt-mr-add-name')?.value || '').trim();
      const offset_ms = Number($('yt-mr-add-offset')?.value || 0) || 0;
      if (!url) { notify('Укажите IP-адрес ведомой колонки', true); return; }
      updateMultiroom({ action: 'add', url, name, offset_ms }, 'Колонка добавлена в мультирум');
      if ($('yt-mr-add-url')) $('yt-mr-add-url').value = '';
      if ($('yt-mr-add-name')) $('yt-mr-add-name').value = '';
    });
    $('yt-mr-sync-btn')?.addEventListener('click', () => {
      updateMultiroom({ action: 'sync' }, 'Синхронизация отправлена на ведомые колонки');
    });
    $('yt-mr-list')?.addEventListener('click', e => {
      const rm = e.target.closest('[data-mr-remove]');
      if (rm) {
        updateMultiroom({ action: 'remove', id: rm.dataset.mrRemove }, 'Колонка удалена');
        return;
      }
      const sug = e.target.closest('[data-mr-suggest-url]');
      if (sug) {
        updateMultiroom({ action: 'add', url: sug.dataset.mrSuggestUrl, name: sug.dataset.mrSuggestName || '' }, 'Колонка добавлена');
      }
    });
    $('yt-mr-list')?.addEventListener('change', e => {
      const tog = e.target.closest('[data-mr-toggle]');
      if (tog) {
        updateMultiroom({ action: 'update', id: tog.dataset.mrToggle, enabled_follower: !!tog.checked });
      }
    });

    // Navigation integration with app.js
    window.addEventListener('panel:tab', e => {
      const detail = e?.detail || {};
      if (detail.tab !== 'yt') {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
        return;
      }
      loadStatus();
      handleRoute(detail.query || '');
      scheduleStatusPoll();
    });

    window.addEventListener('yt:navigate', e => {
      const detail = e?.detail || {};
      const q = detail.query || (detail.href && detail.href.includes('?') ? detail.href.split('?')[1] : '');
      if (q) handleRoute(q);
      else if (detail.tab === 'yt') setSection(activeSection, { resetDetail: true, load: true });
    });

    window.addEventListener('hashchange', () => {
      const h = location.hash || '';
      if (h === '#yt' || h === '#vk' || h.startsWith('#yt?') || h.startsWith('#vk?')) {
        if (typeof window.showTab !== 'function') {
          handleRoute(h);
          loadStatus();
        }
      }
    });

    document.addEventListener('visibilitychange', () => {
      if (document.hidden) {
        if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
      } else if (!$('tab-yt')?.hidden) {
        loadStatus();
        scheduleStatusPoll();
      }
    });

    // Initial state if #tab-yt is already active on load (or standalone without app.js)
    const h = location.hash || '';
    const standaloneRoute = typeof window.showTab !== 'function'
      && (h === '#yt' || h === '#vk' || h.startsWith('#yt?') || h.startsWith('#vk?'));
    if (!$('tab-yt')?.hidden || standaloneRoute) {
      loadStatus();
      if (h.includes('?')) handleRoute(h);
      else if (!$('tab-yt')?.hidden) setSection(activeSection, { resetDetail: false, load: true });
      scheduleStatusPoll();
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
