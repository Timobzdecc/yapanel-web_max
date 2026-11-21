/* In-browser backend emulator for YapAnel (Yandex Station Max) on GitHub Pages. */
(() => {
  'use strict';

  const ORIG_FETCH = window.fetch ? window.fetch.bind(window) : null;
  const ORIG_WS = window.WebSocket;
  const ORIG_ES = window.EventSource;
  const BASE = new URL('./', document.baseURI);
  const COVER_BASE = location.protocol === 'https:'
    ? new URL('static/demo-covers/', BASE).href
    : 'https://timobzdecc.github.io/yapanel-web_max/static/demo-covers/';

  // Prevent stale Service Worker caching in static demo mode.
  try {
    if (navigator.serviceWorker && typeof navigator.serviceWorker.register === 'function') {
      const dummyReg = {
        scope: BASE.href,
        active: null,
        installing: null,
        waiting: null,
        update: async () => {},
        unregister: async () => true,
        addEventListener: () => {},
        removeEventListener: () => {},
      };
      navigator.serviceWorker.register = async () => dummyReg;
    }
  } catch (_) {}

  // Ensure animated equalizer visualizer is turned on by default for first-time visitors.
  try {
    if (localStorage.getItem('ui.viz') === null) {
      localStorage.setItem('ui.viz', '1');
      window.__vizOff = false;
    }
    if (!localStorage.getItem('fs.cwd')) {
      localStorage.setItem('fs.cwd', '/data/yapanel');
    }
  } catch (_) {}

  const NORM_MIN_DB = -16.0;
  const NORM_MAX_DB = 4.0;
  function computeNormGain(loudnessDb) {
    if (loudnessDb === null || loudnessDb === undefined || Number.isNaN(Number(loudnessDb))) return null;
    const gainDb = Math.max(NORM_MIN_DB, Math.min(NORM_MAX_DB, -Number(loudnessDb)));
    return Number((10 ** (gainDb / 20)).toFixed(4));
  }

  // ---------- Comical Tracks, Mixes, Artists & Synced Karaoke Lyrics ----------
  const TRACKS = [
    {
      id: 'demo_perf_01',
      ytId: 'dPerf01Demo',
      ymId: '9001001',
      title: 'Перфоратор соседа (10 Hours Bass Boosted)',
      artist: 'DJ Несущая Стена',
      artistId: 'UC_wall_dj',
      album: 'Ремонт без выходных (Remastered)',
      albumId: '88001',
      duration: 214,
      loudness_db: 8.4,
      cover: COVER_BASE + 'c1.svg',
      genre: 'Индустриальный перфокор',
      lyrics: [
        [0, 'Суббота, восемь ровно на часах…'],
        [5, 'И кто-то снова сверлит в трёх местах!'],
        [11, 'Сверло восьмёрка бьёт в бетонный швеллер,'],
        [17, 'Качает лучше, чем сабвуфер в гелике.'],
        [23, '🎛️ Убавь на −8 dB! (нормализация −14 LUFS спасла обои)'],
        [30, 'Др-р-р-р-р-р — сольная партия перфоратора!'],
        [38, 'Алиса, включи громче, чтобы перекрыть ремонт!'],
      ],
    },
    {
      id: 'demo_brick_02',
      ytId: 'dBrick02Dem',
      ymId: '9001002',
      title: 'Алиса, не тупи (feat. Станция в кирпиче)',
      artist: 'MC Перепрошивка',
      artistId: 'UC_flash_mc',
      album: 'boot-root.img без бэкапа',
      albumId: '88002',
      duration: 185,
      loudness_db: -3.2,
      cover: COVER_BASE + 'c2.svg',
      genre: 'Киберпанк-эмбиент',
      lyrics: [
        [0, 'Я только dd if=boot.img хотел проверить…'],
        [6, 'Теперь моргает красным и не верит!'],
        [12, 'UART подпаян, термофен в руке,'],
        [18, 'Жду telnet 2323 на светлом огоньке.'],
        [24, '«Алиса, ты жива?» — молчит колонка строго,'],
        [30, 'Но ожил ADB — и с души свалилась тревога!'],
      ],
    },
    {
      id: 'demo_boris_03',
      ytId: 'dBoris03Dem',
      ymId: '9001003',
      title: 'Кот Борис уронил роутер в 4 утра',
      artist: 'Пушистый Тыгыдык',
      artistId: 'UC_boris_cat',
      album: 'Лоток, корм и MikroTik',
      albumId: '88003',
      duration: 198,
      loudness_db: 4.1,
      cover: COVER_BASE + 'c3.svg',
      genre: 'Ночной тыгыдык-дэнс',
      lyrics: [
        [0, 'Четыре утра. Мигали светодиоды тихо…'],
        [6, 'Но в темноте крадётся мохнатое лихо.'],
        [12, 'Один удар лапой по блоку питания —'],
        [18, 'И всё локальное облако пало без сознания!'],
        [24, 'Кот Борис доволен, миска пуста,'],
        [30, 'А админ бежит с патчкордом до утра!'],
      ],
    },
    {
      id: 'demo_backup_04',
      ytId: 'dBack04Demo',
      ymId: '9001004',
      title: 'Песня про забытый backup boot.img',
      artist: 'Хор Слёз и Паяльника',
      artistId: 'UC_solder_ch',
      album: 'Антология «Авось прокатит»',
      albumId: '88004',
      duration: 240,
      loudness_db: 6.5,
      cover: COVER_BASE + 'c4.svg',
      genre: 'Драматический шансон',
      lyrics: [
        [0, '«Да зачем этот бэкап, раздел же простой!»'],
        [6, 'Сказал разработчик перед прошивкой ночной.'],
        [13, 'Но опечатка в bs=4096 и счётчике count —'],
        [20, 'И вместо музыки поёт лишь серый экран-аккаунт.'],
        [27, 'Запомни, дружище, завет золотой:'],
        [33, 'Сделай sha256sum и бэкап родной!'],
      ],
    },
    {
      id: 'demo_sub_05',
      ytId: 'dSub05Demo1',
      ymId: '9001005',
      title: 'Я спросил у Яндекса: где моя подписка?',
      artist: 'Глагол & Токен',
      artistId: 'UC_glagol_tk',
      album: 'Локальный OAuth Forever',
      albumId: '88005',
      duration: 162,
      loudness_db: 2.4,
      cover: COVER_BASE + 'c5.svg',
      genre: 'Токен-фанк',
      lyrics: [
        [0, 'Я спросил у Алисы: «Включи YouTube на Макс!»'],
        [6, 'Она: «Подключи телевизор и оформи бакс!»'],
        [12, 'Но hook_location.js перехватил поток,'],
        [18, 'Подставил GStreamer — и заиграл тяжёлый рок!'],
        [24, 'Алиса удивлённо погасила свой экран,'],
        [30, 'А дома в Hi-Fi качает локальный меломан!'],
      ],
    },
    {
      id: 'demo_bass_06',
      ytId: 'dBass06Demo',
      ymId: '9001006',
      title: 'Басс такой, что шкаф открылся сам',
      artist: 'TAS5805M & Сабсоник 48Гц',
      artistId: 'UC_dsp_hifi',
      album: 'Живая АЧХ без резонансов',
      albumId: '88006',
      duration: 205,
      loudness_db: 9.1,
      cover: COVER_BASE + 'c6.svg',
      genre: 'Суббас 48 Гц',
      lyrics: [
        [0, 'Сабсоник выключен — гудит сервант у тёщи,'],
        [6, 'Включён режектор 120 Гц — звук стал намного чётче!'],
        [13, 'Dynamic Loudness поддал басов на 10% громкости,'],
        [20, 'И даже соседи сверху оценили чёткость без колкости!'],
      ],
    },
    {
      id: 'demo_wifi_07',
      ytId: 'dWifi07Demo',
      ymId: '9001007',
      title: 'Симфония отвала Wi-Fi на 5 ГГц',
      artist: 'Оркестр Микротика и NGINX',
      artistId: 'UC_mikrotik',
      album: 'MITM Live in 10.10.50.2',
      albumId: '88007',
      duration: 271,
      loudness_db: -2.0,
      cover: COVER_BASE + 'c7.svg',
      genre: 'Неоклассика 802.11ac',
      lyrics: [
        [0, 'DoT 853 зарезан файерволом на лету,'],
        [7, 'Падает буфер прямо посреди ноты… ту-ту…'],
        [14, 'Переключил на 160-й канал — вздохнул RSSI −45,'],
        [21, 'И музыка льётся плавно, не пропадая опять!'],
      ],
    },
    {
      id: 'demo_kettle_08',
      ytId: 'dKett08Demo',
      ymId: '9001008',
      title: 'Чайник закипел, но ты уже уснул',
      artist: 'Умный Дом (Без Интернета)',
      artistId: 'UC_smart_off',
      album: 'Локальный MQTT и тишина',
      albumId: '88008',
      duration: 150,
      loudness_db: -3.8,
      cover: COVER_BASE + 'c8.svg',
      genre: 'Чайный лоу-фай',
      lyrics: [
        [0, 'Лампы Tuya мигают в такт кипящей воде,'],
        [6, 'Но ты заснул на диване, забыв про кофе на столе…'],
        [12, 'Ночной режим DSP убавил высокие плавно,'],
        [18, 'И кот Борис мурчит в унисон — вот это славно.'],
      ],
    },
    {
      id: 'demo_root_09',
      ytId: 'dRoot09Demo',
      ymId: '9001009',
      title: "Мама, я в root'е (UID=0 Remix)",
      artist: 'Telnet 2323',
      artistId: 'UC_telnet_0',
      album: 'CapEff: 0000001fffffffff',
      albumId: '88009',
      duration: 190,
      loudness_db: 1.8,
      cover: COVER_BASE + 'c9.svg',
      genre: 'Хакер-синтвейв',
      lyrics: [
        [0, 'Захожу по adb connect :5555 — кто там? uid=2000…'],
        [6, 'Запускаю telnetd 2323 — и сразу uid=0(root)!'],
        [12, 'Снёс аналитику Яндекса в hosts навсегда,'],
        [18, 'Теперь колонка слушается только меня!'],
      ],
    },
    {
      id: 'demo_oom_10',
      ytId: 'dOom10Demo1',
      ymId: '9001010',
      title: "Колыбельная для OOM Killer'а",
      artist: '1 ГБ ОЗУ на Первой Станции',
      artistId: 'UC_oom_gen1',
      album: 'Баю-бай, com.yandex.io.sdk',
      albumId: '88010',
      duration: 228,
      loudness_db: -4.0,
      cover: COVER_BASE + 'c10.svg',
      genre: 'Сонная электроника',
      lyrics: [
        [0, 'Спи, старый демон, памяти больше нет,'],
        [6, 'В одном гигабайте тесно на весь белый свет…'],
        [12, 'Хорошо, что на Максе целых два гига DDR4,'],
        [18, 'И веб-панель с 3D летает в уютной квартире!'],
      ],
    },
  ];

  const MIXES = [
    {
      browse_id: 'VLRDTMAK_super',
      kind: 'playlist',
      title: 'Мой супермикс: Отвал ADB и басы',
      subtitle: 'Персональный микс · DJ Несущая Стена и другие',
      thumb: COVER_BASE + 'm1.svg',
      trackIndexes: [0, 5, 1, 8, 2, 4],
    },
    {
      browse_id: 'VLRDTMAK_uart',
      kind: 'playlist',
      title: 'Микс для пайки UART в 3 часа ночи',
      subtitle: 'Ночной код и припой ПОС-61',
      thumb: COVER_BASE + 'm2.svg',
      trackIndexes: [1, 3, 8, 7, 9, 6],
    },
    {
      browse_id: 'VLRDTMAK_replay',
      kind: 'playlist',
      title: 'Риплей Микс: Соседи вызвали участкового',
      subtitle: 'То, что чаще всего звучало на 80% громкости',
      thumb: COVER_BASE + 'm3.svg',
      trackIndexes: [0, 5, 2, 3, 4],
    },
    {
      browse_id: 'VLRDTMAK_archive',
      kind: 'playlist',
      title: 'Архивный микс: До патча .50',
      subtitle: 'Ностальгия по открытому 5555 без MITM',
      thumb: COVER_BASE + 'm4.svg',
      trackIndexes: [3, 6, 8, 9, 1],
    },
    {
      browse_id: 'VLRDTMAK_friday',
      kind: 'playlist',
      title: 'Для бодрости: Дебаг на проде в пятницу',
      subtitle: 'Когда релиз уже ушёл, а в README старая версия',
      thumb: COVER_BASE + 'm5.svg',
      trackIndexes: [4, 2, 0, 8, 5],
    },
    {
      browse_id: 'VLRDTMAK_dns',
      kind: 'playlist',
      title: 'Фитнес-микс: Бег к роутеру перезагружать DNS',
      subtitle: 'Кардио до коридора и обратно (160 BPM)',
      thumb: COVER_BASE + 'm6.svg',
      trackIndexes: [6, 2, 5, 0, 7],
    },
  ];

  const ARTISTS = TRACKS.map(t => ({
    id: 'ext:yt:' + t.artistId,
    browse_id: t.artistId,
    kind: 'artist',
    source: 'yt',
    name: t.artist,
    title: t.artist,
    subtitle: t.genre,
    cover: t.cover,
    thumb: t.cover,
    tracks: 42,
    albums: 7,
    description: `Легенда умного дома (${t.genre}). Автор бессмертного хита «${t.title}». Слушается идеально с Hi-Fi DSP и нормализацией −14 LUFS.`,
    popularIdx: [TRACKS.indexOf(t)],
  }));

  function toYtTrack(t, rank) {
    return {
      source: 'yt',
      id: t.ytId,
      video_id: t.ytId,
      title: t.title,
      artist: t.artist,
      album: t.album,
      duration: t.duration,
      thumb: t.cover,
      cover: t.cover,
      loudness_db: t.loudness_db,
      rank: rank || undefined,
    };
  }
  function toQueueTrack(t) {
    return {
      id: 'ext:yt:' + t.ytId,
      title: t.title,
      artists: t.artist,
      cover: t.cover,
      duration: t.duration,
      album: t.album,
    };
  }

  // ---------- Mock State ----------
  const nowSec = () => Date.now() / 1000;
  const state = {
    color: 'graphite',
    volume: 42,
    muted: false,
    playing: true,
    trackIndex: 0,
    progress: 14,
    progressAt: nowSec(),
    shuffle: false,
    repeat: 'none', // none | all | one
    autoplayRadio: true,
    queueIndexes: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    likedYt: new Set([TRACKS[0].ytId, TRACKS[2].ytId, TRACKS[5].ytId]),
    extSource: 'yt',
    normalizeEnabled: true,
    osdEnabled: true,
    remoteEnabled: true,
    remoteDpadHdmi: false,
    dlnaEnabled: true,
    multiroomEnabled: false,
    multiroomSyncVol: false,
    multiroomFollowers: [
      { id: 'mr1', name: 'Кухня (Станция Мини 2)', url: 'http://192.168.1.148:8765', offset_ms: 0, enabled: false, last_status: 'готова', latency_ms: 4 },
    ],
    dspProfile: 'hifi',
    dspFilters: { subsonic: true, notch120: true, phase: true, treble: true, drc_bypass: true, loudness: true },
    dspParams: { bass_gain: 0.0, treble_gain: 3.0, subsonic_freq: 48, eq: [0, 0, 0, 0, 0, 0, 0, 0, 0, 0] },
    party: {
      running: true,
      mode: 'music',
      preset: 'aurora',
      speed: 50,
      bright: 85,
      auto_bright: true,
      fade_ms: 140,
      sensitivity: 65,
      bulbs: [
        { id: 'b1', name: 'Люстра «Гроза соседей»', ip: '192.168.1.201', key: 'demo-key-01', role: 'ceiling', side: 'center', pos_x: 0.5, ver: '3.3', enabled: true, online: true, rgb: [255, 110, 40] },
        { id: 'b2', name: 'Подсветка за телеком', ip: '192.168.1.202', key: 'demo-key-02', role: 'ambilight', side: 'left', pos_x: 0.15, ver: '3.4', enabled: true, online: true, rgb: [90, 170, 255] },
        { id: 'b3', name: 'Торшер у лотка кота Бориса', ip: '192.168.1.203', key: 'demo-key-03', role: 'ambient', side: 'right', pos_x: 0.85, ver: '3.3', enabled: true, online: true, rgb: [190, 80, 255] },
        { id: 'b4', name: 'Диско-шар в серверной', ip: '192.168.1.204', key: 'demo-key-04', role: 'accent', side: 'center', pos_x: 0.5, ver: '3.5', enabled: false, online: true, rgb: [40, 220, 140] },
      ],
    },
    clockText: '',
    clockBright: 80,
    bootAlways: true,
    bootMode: 'auto',
    ringRgb: [255, 106, 0],
    displayMode: null,
    activePresetId: null,
    animCustom: { clock: [], ring: ['alice_listening.led'] },
    patterns: {
      clock: ['boot.binscled.zst', 'music_eq.binscled.zst', 'weather_sun.binscled.zst'],
      ring: ['Privetstvie-02.led', 'alice_listening.led', 'alice_speaking.led', 'alice_thinking.led', 'bluetooth_playing.led', 'budilnik-12.led', 'error.led', 'sleep.led', 'my/Кот_Борис_Светится.led'],
    },
    phrases: [
      { id: 'ph1', name: 'Доброе утро от Бориса', text: 'Мяу! Пора вставать, лоток сам себя не почистит.' },
      { id: 'ph2', name: 'Предупреждение соседям', text: 'Внимание! Включаю перфоратор на минималках.' },
    ],
    greeting: { on: true, text: 'Добро пожаловать домой, повелитель рута!', delay: 45 },
    privacy: { enabled: true, blocked: true, managed: true, applied: true, backend: 'hosts' },
    btPowered: true,
    btKeepOff: false,
    btMemesRunning: true,
    btMemesInstalled: true,
    assistant: {
      enabled: true,
      mode: 'hybrid',
      model: 'gpt-4o-mini',
      wakeword: 'Джарвис',
      history: [
        { q: 'Джарвис, кот Борис снова сбросил Wi-Fi?', a: 'Подтверждаю, сэр. Обнаружен пушистый объект весом 5 кг на корпусе роутера. Рекомендую дать ему вкусняшку.' },
      ],
    },
    yam2: {
      installed: true,
      version: '2.4.1',
      latest: '2.4.1',
      platform: 'yandexstation_2',
      host: '192.168.1.144',
      packages: [
        {
          id: 'wakeword',
          name: 'wakeword',
          version: '1.8.0',
          target: 'arm64',
          services: [{ id: 'yam~wakeword~svc', name: 'wakeword', exec: ['/data/wakeword/wakeword', '--word', 'Джарвис'], running: true, boot: true }],
        },
        {
          id: 'luna-glow',
          name: 'luna-glow',
          version: '0.9.3',
          target: 'arm64',
          services: [{ id: 'yam~luna~glow', name: 'luna', exec: ['/data/yam/bin/luna', '--port', '8099'], running: true, boot: true }],
        },
      ],
      catalog: [
        { name: 'wakeword', latest: '1.8.0', fits: true, summary: 'Свои слова активации (Джарвис, Алёша, Борис)', panel: { tab: 'Активация', tested: ['wakeword'], names: { wakeword: 'Голос' } } },
        { name: 'luna-glow', latest: '0.9.3', fits: true, summary: 'Модинг подсветки кольца и матрицы без тормозов', panel: { tab: 'Анимации', tested: ['ring'], names: { ring: 'Кольцо' } } },
        { name: 'boris-feeder', latest: '1.0.2', fits: true, summary: 'Автокормушка кота Бориса по голосовой команде «Мяу»', panel: null },
      ],
    },
    wakewords: ['Джарвис', 'Алёша', 'Борис'],
    fs: {
      '/data/yapanel': [
        { name: 'extmusic', is_dir: true, size: 0, mtime: 1795265000, mode: 'drwxr-xr-x' },
        { name: 'static', is_dir: true, size: 0, mtime: 1795265000, mode: 'drwxr-xr-x' },
        { name: 'secret_cat_photos.txt', is_dir: false, size: 420, mtime: 1795265100, mode: '-rw-r--r--', content: '=== СЕКРЕТНЫЙ АРХИВ КОТА БОРИСА ===\n[03:14] Фото 1: Борис сидит на тёплой Станции Макс и греет пузо.\n[03:42] Фото 2: Борис смотрит на патч-корд с преступным интересом.\n[04:01] Фото 3: Роутер на полу. Миссия выполнена. Мур-мяу.\n' },
        { name: 'how_to_unbrick_station.md', is_dir: false, size: 680, mtime: 1795265200, mode: '-rw-r--r--', content: '# Как не превратить Станцию Макс в дорогой пресс-папье\n\n1. НИКОГДА не заливай boot-root.img без sha256sum!\n2. Перед тем как трогать /dev/block/by-name/boot — проверь размер блока.\n3. Если всё же окирпичил — наливай чай, доставай USB-TTL и вспоминай, куда дел оригинальный boot.img.\n4. P.S. Кот Борис не виноват (наверное).\n' },
        { name: 'who_ate_the_sausage.log', is_dir: false, size: 310, mtime: 1795265300, mode: '-rw-r--r--', content: '2026-11-21 02:11:04 [AUDIT] Холодильник открыт: user=unknown\n2026-11-21 02:11:09 [SENSOR] Докторская колбаса (350 г): исчезла\n2026-11-21 02:11:15 [MIC_7] Зафиксировано громкое чавканье под столом\n2026-11-21 02:12:00 [VERDICT] Это точно не хозяин. Подозреваемый №1: Кот Борис.\n' },
        { name: 'neighbors_complaints.json', is_dir: false, size: 380, mtime: 1795265400, mode: '-rw-r--r--', content: JSON.stringify({ apartment: 42, status: 'Подпрыгивает люстра на 48 Гц', note_to_self: 'Переключиться в ночной DSP-профиль после 23:00', subwoofer_test_track: 'DJ Несущая Стена — Перфоратор соседа' }, null, 2) },
      ],
      '/data/yapanel/extmusic': [
        { name: 'service.py', is_dir: false, size: 129847, mtime: 1795265000, mode: '-rw-r--r--', content: '# YapAnel ExtMusic Service (Live Demo Mode)\n# Всё в браузере, ни один соседский сабвуфер не пострадал!\n' },
        { name: 'gstbridge.py', is_dir: false, size: 51628, mtime: 1795265000, mode: '-rw-r--r--', content: '# Normalizing YouTube tracks to -14 LUFS via native GStreamer volume=normalization\n' },
      ],
      '/data/quasar': [
        { name: 'webpanel', is_dir: true, size: 0, mtime: 1795264000, mode: 'drwxr-xr-x' },
        { name: 'README_QUASAR.txt', is_dir: false, size: 190, mtime: 1795264000, mode: '-rw-r--r--', content: 'Системная папка Quasar. Кот Борис просит не удалять её командой rm -rf /.\n' },
      ],
    },
  };

  function currentTrack() {
    return TRACKS[state.trackIndex] || TRACKS[0];
  }

  function currentProgress() {
    const tr = currentTrack();
    if (!state.playing) return Math.min(tr.duration, state.progress);
    const elapsed = Math.max(0, nowSec() - state.progressAt);
    const pos = state.progress + elapsed;
    if (pos >= tr.duration && tr.duration > 0) {
      advanceTrack(1, true);
      return state.progress;
    }
    return pos;
  }

  function advanceTrack(delta, auto = false) {
    if (auto && state.repeat === 'one') {
      state.progress = 0;
      state.progressAt = nowSec();
      return;
    }
    const q = state.queueIndexes;
    if (!q.length) return;
    let qi = q.indexOf(state.trackIndex);
    if (qi < 0) qi = 0;
    if (state.shuffle && q.length > 1) {
      qi = (qi + 1 + Math.floor(Math.random() * (q.length - 1))) % q.length;
    } else {
      qi = (qi + delta + q.length) % q.length;
    }
    state.trackIndex = q[qi];
    state.progress = 0;
    state.progressAt = nowSec();
    state.playing = true;
    synthNoteBurst();
  }

  function playTrackById(id) {
    const clean = String(id || '').replace(/^ext:yt:/, '');
    const idx = TRACKS.findIndex(t => t.ytId === clean || t.ymId === clean || t.id === clean);
    if (idx >= 0) {
      state.trackIndex = idx;
      if (!state.queueIndexes.includes(idx)) state.queueIndexes.push(idx);
      state.progress = 0;
      state.progressAt = nowSec();
      state.playing = true;
      synthNoteBurst();
    }
  }

  // ---------- Optional WebAudio Funny Synth (toggled by user pill) ----------
  let synthEnabled = false;
  let audioCtx = null;
  let synthTimer = null;
  const MELODY = [220, 261.63, 329.63, 293.66, 220, 392, 349.23, 329.63];
  let melodyStep = 0;
  function ensureAudioCtx() {
    if (!audioCtx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (AC) audioCtx = new AC();
    }
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
    return audioCtx;
  }
  function playTone(freq, dur = 0.14, gainVal = 0.04) {
    if (!synthEnabled) return;
    const ctx = ensureAudioCtx();
    if (!ctx) return;
    const osc = ctx.createOscillator();
    const g = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.value = freq;
    const norm = state.normalizeEnabled ? (computeNormGain(currentTrack().loudness_db) || 1.0) : 1.0;
    const v = Math.max(0.002, Math.min(0.12, gainVal * (state.volume / 60) * norm * (state.muted ? 0 : 1)));
    g.gain.setValueAtTime(v, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0005, ctx.currentTime + dur);
    osc.connect(g);
    g.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + dur + 0.02);
  }
  function synthNoteBurst() {
    if (!synthEnabled) return;
    playTone(440, 0.08, 0.05);
    setTimeout(() => playTone(554.37, 0.1, 0.05), 80);
  }
  function setSynth(on) {
    synthEnabled = !!on;
    if (synthTimer) { clearInterval(synthTimer); synthTimer = null; }
    if (synthEnabled) {
      ensureAudioCtx();
      synthTimer = setInterval(() => {
        if (state.playing && !state.muted) {
          playTone(MELODY[melodyStep % MELODY.length], 0.16, 0.035);
          melodyStep++;
        }
      }, 280);
    }
  }

  // ---------- Animated 25x16 Matrix & 24-LED Ring Generator ----------
  const FONT_3X5 = {
    '0': [7, 5, 5, 5, 7], '1': [2, 6, 2, 2, 7], '2': [7, 1, 7, 4, 7], '3': [7, 1, 7, 1, 7],
    '4': [5, 5, 7, 1, 1], '5': [7, 4, 7, 1, 7], '6': [7, 4, 7, 5, 7], '7': [7, 1, 1, 1, 1],
    '8': [7, 5, 7, 5, 7], '9': [7, 5, 7, 1, 7], ':': [0, 2, 0, 2, 0],
  };
  function drawClockDigit(px, ch, ox, oy, val) {
    const g = FONT_3X5[ch];
    if (!g) return;
    for (let r = 0; r < 5; r++) {
      for (let c = 0; c < 3; c++) {
        if ((g[r] >> (2 - c)) & 1) {
          const y = oy + r, x = ox + c;
          if (y >= 0 && y < 16 && x >= 0 && x < 25) px[y * 25 + x] = val;
        }
      }
    }
  }
  function makeLivePixelsAndRing() {
    const px = new Array(400).fill(0);
    const t = Date.now() / 1000;
    const br = Math.round(80 + (state.clockBright / 100) * 170);

    // Subtle animated equalizer bars at bottom when music is playing
    const bands = [];
    for (let i = 0; i < 4; i++) {
      const b = state.playing
        ? (0.28 + 0.55 * Math.abs(Math.sin(t * (2.6 + i * 0.7) + i * 1.1)))
        : 0.06;
      bands.push(Number(b.toFixed(3)));
    }
    if (state.playing && !window.__vizOff) {
      window.__spectrum = { bands, t: performance.now() };
    }

    if (state.displayMode === 'plasma') {
      for (let y = 0; y < 16; y++) {
        for (let x = 0; x < 25; x++) {
          const v = 0.5 + 0.5 * Math.sin(x * 0.35 + t * 2.5) * Math.cos(y * 0.4 - t * 1.8);
          px[y * 25 + x] = Math.round(v * br);
        }
      }
    } else if (state.displayMode === 'fire') {
      for (let y = 0; y < 16; y++) {
        for (let x = 0; x < 25; x++) {
          const flame = Math.max(0, (y / 15) * (0.65 + 0.35 * Math.sin(x * 0.9 - t * 6 + y * 0.4)));
          px[y * 25 + x] = Math.round(flame * br);
        }
      }
    } else if (state.clockText) {
      // Simple dotted wave banner when custom text is shown
      for (let x = 0; x < 25; x++) {
        const y = Math.round(7.5 + Math.sin(x * 0.4 + t * 4) * 4);
        px[y * 25 + x] = br;
      }
    } else {
      // Clock in upper half + audio equalizer bars below
      const d = new Date();
      const hh = String(d.getHours()).padStart(2, '0');
      const mm = String(d.getMinutes()).padStart(2, '0');
      drawClockDigit(px, hh[0], 3, 2, br);
      drawClockDigit(px, hh[1], 7, 2, br);
      if (Math.floor(t * 2) % 2 === 0) drawClockDigit(px, ':', 11, 2, br);
      drawClockDigit(px, mm[0], 15, 2, br);
      drawClockDigit(px, mm[1], 19, 2, br);
      if (state.playing) {
        for (let x = 2; x < 23; x++) {
          const h = Math.max(1, Math.round(bands[x % 4] * 6));
          for (let y = 15; y > 15 - h; y--) {
            px[y * 25 + x] = Math.round(br * 0.72);
          }
        }
      }
    }

    // 24 RGB LEDs ring
    const ring = [];
    for (let i = 0; i < 24; i++) {
      if (state.playing) {
        const phase = (i / 24) * Math.PI * 2 - t * 2.4;
        const glow = 0.35 + 0.65 * Math.max(0, Math.cos(phase));
        ring.push([
          Math.round(state.ringRgb[0] * glow),
          Math.round(state.ringRgb[1] * glow),
          Math.round(state.ringRgb[2] * glow),
        ]);
      } else {
        ring.push([
          Math.round(state.ringRgb[0] * 0.18),
          Math.round(state.ringRgb[1] * 0.18),
          Math.round(state.ringRgb[2] * 0.18),
        ]);
      }
    }
    return { pixels: px, ring, ring_all: ring, spectrum: { bands } };
  }

  // ---------- DSP Frequency Curve Calculator ----------
  function buildDspPayload() {
    const freqs = [];
    for (let i = 0; i < 60; i++) {
      freqs.push(Math.round(20 * Math.pow(1000, i / 59)));
    }
    const f = state.dspFilters;
    const p = state.dspParams;
    const bassOff = state.dspProfile === 'bass' ? 4.0 : state.dspProfile === 'club' ? 6.0 : state.dspProfile === 'night' ? -6.0 : state.dspProfile === 'hifi' ? 0.5 : (p.bass_gain || 0);
    const trebOff = state.dspProfile === 'night' ? -2.5 : state.dspProfile === 'hifi' ? 2.5 : (p.treble_gain || 0);
    const loudBass = f.loudness ? Math.max(0, (55 - state.volume) * 0.11) : 0;
    const loudTreb = f.loudness ? Math.max(0, (55 - state.volume) * 0.04) : 0;

    const eqBands = [
      { id: '40', freq: 40, q: 1.41, channel: 'woofer', reg: '3E', label: '40 Гц' },
      { id: '80', freq: 80, q: 1.41, channel: 'woofer', reg: '3F', label: '80 Гц' },
      { id: '160', freq: 160, q: 1.41, channel: 'woofer', reg: '40', label: '160 Гц' },
      { id: '400', freq: 400, q: 1.41, channel: 'tweeter', reg: '39', label: '400 Гц' },
      { id: '800', freq: 800, q: 1.41, channel: 'tweeter', reg: '3A', label: '800 Гц' },
      { id: '1600', freq: 1600, q: 1.41, channel: 'tweeter', reg: '3B', label: '1.6 кГц' },
      { id: '3150', freq: 3150, q: 1.41, channel: 'tweeter', reg: '3D', label: '3.2 кГц' },
      { id: '6300', freq: 6300, q: 1.41, channel: 'tweeter', reg: '3E', label: '6.3 кГц' },
      { id: '12500', freq: 12500, q: 1.41, channel: 'tweeter', reg: '3F', label: '12.5 кГц' },
      { id: '16000', freq: 16000, q: 1.41, channel: 'tweeter', reg: '40', label: '16 кГц' },
    ];

    const woofer_db = [];
    const tweeter_db = [];
    const system_db = [];
    for (const fq of freqs) {
      let subCut = (f.subsonic && fq < (p.subsonic_freq || 48)) ? -24 * Math.log2((p.subsonic_freq || 48) / fq) : 0;
      let notch = f.notch120 ? -4.2 * Math.exp(-Math.pow((fq - 120) / 28, 2)) : 0;
      let eqSum = 0;
      for (let b = 0; b < 10; b++) {
        const g = (p.eq && p.eq[b]) || 0;
        if (g) {
          const oct = Math.log2(fq / eqBands[b].freq);
          eqSum += g * Math.exp(-oct * oct * 3.5);
        }
      }
      const bassShelf = fq < 180 ? (bassOff + loudBass) * (1 - fq / 220) : 0;
      const trebShelf = (f.treble && fq > 3500) ? (trebOff + loudTreb) * Math.min(1, (fq - 3500) / 9000) : 0;
      const sys = Math.max(-42, Math.min(15, subCut + notch + bassShelf + trebShelf + eqSum));
      const xo = 1 / (1 + Math.pow(fq / 2200, 4));
      woofer_db.push(Number((sys + 20 * Math.log10(Math.max(0.01, xo))).toFixed(1)));
      tweeter_db.push(Number((sys + 20 * Math.log10(Math.max(0.01, 1 - xo))).toFixed(1)));
      system_db.push(Number(sys.toFixed(1)));
    }
    return {
      ok: true,
      supported: true,
      active_profile: state.dspProfile,
      profile: state.dspProfile,
      profiles: [
        { id: 'hifi', name: 'Студийный звук', badge: 'Студия', desc: 'Ровный, чистый звук: без дрожи мебели и гула.' },
        { id: 'stock', name: 'Как с завода', badge: 'Завод', desc: 'Стандартное звучание без обработки.' },
        { id: 'night', name: 'Ночной режим', badge: 'Ночь', desc: 'Соседи спят спокойно, кот Борис тоже.' },
        { id: 'bass', name: 'Плотный бас', badge: 'Бас', desc: 'Упругий бас 48 Гц без раскачки серванта.' },
        { id: 'club', name: 'Клубный бас', badge: 'Клуб', desc: 'Чтобы открылся шкаф у DJ Несущая Стена.' },
        { id: 'custom', name: 'Свои настройки', badge: 'Своё', desc: 'Ручной 10-полосный эквалайзер и фильтры.' },
      ],
      filters: { ...state.dspFilters },
      params: { ...state.dspParams },
      eq: [...(state.dspParams.eq || [])],
      eq_bands: eqBands,
      loudness: {
        enabled: !!state.dspFilters.loudness,
        volume: state.volume,
        bass_db: Number(loudBass.toFixed(1)),
        treble_db: Number(loudTreb.toFixed(1)),
        ref_volume: 55,
      },
      response: { freqs, woofer_db, tweeter_db, system_db },
    };
  }

  function buildMusicPayload() {
    const t = currentTrack();
    const pos = Number(currentProgress().toFixed(2));
    const ts = nowSec();
    const normGain = state.normalizeEnabled ? computeNormGain(t.loudness_db) : null;
    const extCurrent = {
      source: 'yt',
      id: t.ytId,
      title: t.title,
      artist: t.artist,
      album: t.album,
      duration: t.duration,
      thumb: t.cover,
      loudness_db: t.loudness_db,
      norm_gain: normGain,
    };
    return {
      auth: { logged_in: true, login: 'demo_user', method: 'qr', load_error: null },
      glagol: {
        connected: true,
        error: null,
        updated: ts,
        progress_at: ts,
        state: {
          aliceState: 'IDLE',
          playing: state.playing,
          canStop: state.playing,
          volume: state.volume / 100,
          hdmi: { capable: true, present: false },
          playerState: {
            id: 'ext:yt:' + t.ytId,
            title: t.title,
            subtitle: t.artist,
            duration: t.duration,
            progress: pos,
            playerType: 'external',
            extSource: 'yt',
            playlistType: 'Track',
            playlistDescription: 'YouTube Music · Demo (−14 LUFS)',
            native: false,
            buffering: false,
            autoplayRadio: state.autoplayRadio,
            liked: state.likedYt.has(t.ytId),
            entityInfo: { shuffled: state.shuffle, repeatMode: state.repeat === 'one' ? 'One' : state.repeat === 'all' ? 'All' : 'None' },
            hasPrev: state.queueIndexes.length > 1,
            hasNext: state.queueIndexes.length > 1,
            extra: { coverURI: t.cover, album: t.album },
          },
        },
      },
      extmusic: {
        source: 'yt',
        configured: true,
        current: extCurrent,
        autoplay_radio: state.autoplayRadio,
        shuffle: state.shuffle,
        repeat: state.repeat,
      },
    };
  }

  function buildExtStatusPayload() {
    const t = currentTrack();
    const posMs = Math.round(currentProgress() * 1000);
    return {
      source: 'yt',
      configured: true,
      account: 'boris.the.cat@gmail.com',
      user_id: 'demo_uid_0',
      sources: {
        vk: { configured: false, account: '' },
        yt: { configured: true, account: 'boris.the.cat@gmail.com', authenticated: true, auth_type: 'oauth', gst: true, native: false, cookies: false, oauth: true },
      },
      current: toYtTrack(t),
      queue: state.queueIndexes.map(i => toYtTrack(TRACKS[i])),
      index: Math.max(0, state.queueIndexes.indexOf(state.trackIndex)),
      shuffle: state.shuffle,
      repeat: state.repeat,
      autoplay_radio: state.autoplayRadio,
      native: false,
      error: '',
      skipped: [],
      player: {
        ok: true,
        playing: state.playing,
        state: state.playing ? 'playing' : 'paused',
        position: posMs,
        duration: t.duration * 1000,
        error: '',
      },
    };
  }

  function buildHwPayload() {
    const t = currentTrack();
    const gain = state.normalizeEnabled ? computeNormGain(t.loudness_db) : null;
    return {
      ok: true,
      supported: true,
      osd: { supported: true, enabled: state.osdEnabled, active: false, last_kind: 'track', last_at: nowSec() },
      remote: { supported: true, enabled: state.remoteEnabled, dpad_hdmi: state.remoteDpadHdmi, connected: true, grabbed: state.playing, device_name: 'Yandex Remote 2 (Demo)', device_path: '/dev/input/event4' },
      normalize: { enabled: state.normalizeEnabled, loudness_db: t.loudness_db, gain },
    };
  }

  // ---------- Mock HTTP Router (/api/*) ----------
  function jsonResponse(data, status = 200) {
    const body = JSON.stringify(data);
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? 'OK' : 'Error',
      headers: new Headers({ 'content-type': 'application/json; charset=utf-8' }),
      json: async () => JSON.parse(body),
      text: async () => body,
      blob: async () => new Blob([body], { type: 'application/json' }),
    };
  }

  function parseBody(options) {
    if (!options || !options.body) return {};
    if (typeof options.body === 'string') {
      try { return JSON.parse(options.body); } catch { return {}; }
    }
    return {};
  }

  function showDemoToast(msg, isErr) {
    if (typeof window.toast === 'function') {
      try { window.toast(msg, !!isErr); } catch (_) {}
    }
  }

  async function routeApi(rawPath, options = {}) {
    const u = new URL(rawPath, 'https://demo.local');
    const path = u.pathname;
    const method = String(options.method || 'GET').toUpperCase();
    const body = parseBody(options);

    // Core Auth & Connection & Profile
    if (path === '/api/panel-auth') {
      return jsonResponse({ enabled: false, authed: true, sessions: 1, secure: false, ports: { http: 8765 }, ca: false });
    }
    if (path === '/api/conn') {
      return jsonResponse({ state: 'online', serial: 'demo-emulator', since: nowSec() - 3600, attempts: 0, retry_in: null, log: [{ t: nowSec(), level: 'info', msg: 'Интерактивный браузерный эмулятор Станции Макс' }] });
    }
    if (path === '/api/profile') {
      return jsonResponse({
        id: 'max', name: 'Станция Макс (Demo)', platform: 'yandexstation_2', led_prefix: 'max',
        clock: true, boot: true, alarms: false, scenes: false, assistant: true, apps: true,
        light: true, privacy: true, matrix: true, matrix_cols: 25, matrix_rows: 16, ring_leds: 24,
        tested: ['anims', 'sounds', 'say', 'viz', 'privacy', 'yam2', 'assistant', 'dsp'],
        auto: false, ledpanel: false, hook_supported: true, ring_preview_supported: true,
      });
    }
    if (path === '/api/status') {
      const m = buildMusicPayload();
      return jsonResponse({
        hostname: 'Yandex-Station-Max',
        hardware_model: 'yandexstation_2',
        model: 'max',
        color: state.color,
        uptime: String(Math.round(4200 + performance.now() / 1000)),
        load: '1.12 0.94 0.88',
        mem_total: '2038272',
        mem_avail: '1184256',
        temp: '48200',
        kernel: '4.9.113',
        date: new Date().toLocaleString('ru-RU'),
        ip_address: '192.168.1.144',
        ssid: 'Kvartira_Без_Соседей_5G',
        freq: '5180',
        rssi: '-44',
        linkspeed: '866.7',
        quasar: 'running',
        quasar_version: '3.367.34 (Demo)',
        device_id: 'DEMO-STATION-MAX-0001',
        vol_level: state.volume,
        vol_master: state.volume,
        volume_source: 'glagol',
        muted: state.muted,
        matrix: { cols: 25, rows: 16, pixels: 400 },
        ring_leds: 24,
        bt_connected: [],
        glagol: m.glagol.state,
        clock_text: state.clockText,
        brightness: state.clockBright,
        preset: state.activePresetId,
      });
    }
    if (path === '/api/panel/version') {
      return jsonResponse({ version: '0.6.1', install: 'demo', repo: 'https://github.com/Timobzdecc/yapanel-web_max', checked: nowSec(), latest: '0.6.1', latest_date: '2026-11-21', url: 'https://github.com/Timobzdecc/yapanel-web_max/releases/tag/v0.6.1', update: false });
    }
    if (path === '/api/volume') {
      if (method === 'POST' && body.level !== undefined) {
        state.volume = Math.max(0, Math.min(100, Number(body.level) || 0));
      }
      return jsonResponse({ ok: true, level: state.volume, muted: state.muted });
    }
    if (path === '/api/mute') {
      if (method === 'POST') state.muted = body.muted !== undefined ? !!body.muted : !state.muted;
      return jsonResponse({ ok: true, muted: state.muted });
    }
    if (path === '/api/say') {
      const txt = String(body.text || '').trim();
      showDemoToast(`🗣️ Алиса в эмуляторе: «${txt || 'Привет!'}»`);
      return jsonResponse({ ok: true, spoken: txt });
    }
    if (path === '/api/stop') {
      state.playing = false;
      return jsonResponse({ ok: true });
    }
    if (path === '/api/system') {
      if (body.color) state.color = String(body.color);
      return jsonResponse({ ok: true, color: state.color });
    }

    // DSP
    if (path === '/api/dsp') {
      if (method === 'POST') {
        if (body.profile) state.dspProfile = String(body.profile);
        if (body.filters && typeof body.filters === 'object') Object.assign(state.dspFilters, body.filters);
        if (body.params && typeof body.params === 'object') Object.assign(state.dspParams, body.params);
        if (Array.isArray(body.eq)) state.dspParams.eq = body.eq.slice(0, 10).map(Number);
        if (body.bass_gain !== undefined) state.dspParams.bass_gain = Number(body.bass_gain);
        if (body.treble_gain !== undefined) state.dspParams.treble_gain = Number(body.treble_gain);
        if (body.subsonic_freq !== undefined) state.dspParams.subsonic_freq = Number(body.subsonic_freq);
      }
      return jsonResponse(buildDspPayload());
    }

    // LEDs, Clock & Animations
    if (path === '/api/clock/state' || path === '/api/leds/live') {
      const fr = makeLivePixelsAndRing();
      return jsonResponse({ ok: true, v: fr.pixels, pixels: fr.pixels, ring: fr.ring, ring_all: fr.ring, spectrum: fr.spectrum });
    }
    if (path === '/api/clock') {
      if (body.text !== undefined) state.clockText = String(body.text || '');
      if (body.brightness !== undefined) state.clockBright = Number(body.brightness);
      state.displayMode = null;
      return jsonResponse({ ok: true });
    }
    if (path === '/api/display') {
      state.displayMode = body.mode || 'plasma';
      return jsonResponse({ ok: true, mode: state.displayMode });
    }
    if (path === '/api/display/stop') {
      state.displayMode = null;
      state.clockText = '';
      return jsonResponse({ ok: true });
    }
    if (path === '/api/clock/boot') {
      return jsonResponse({ ok: true, supported: true, hook: true, custom: true, mounted: true, patched: true, always: state.bootAlways, mode: state.bootMode, default: [{ flag: 0, v: makeLivePixelsAndRing().pixels, ms: 200 }] });
    }
    if (path === '/api/clock/boot/always') {
      state.bootAlways = !!body.enabled;
      return jsonResponse({ ok: true, supported: true, hook: true, custom: true, mounted: true, always: state.bootAlways });
    }
    if (path === '/api/clock/boot/reset' || path === '/api/clock/play') {
      return jsonResponse({ ok: true, supported: true, hook: true, custom: false, mounted: true, always: state.bootAlways });
    }
    if (path === '/api/clock/frames') {
      return jsonResponse([
        { flag: 0, v: makeLivePixelsAndRing().pixels, ms: 200 },
        { flag: 0, v: makeLivePixelsAndRing().pixels.map((x, idx) => (idx % 2 ? x : 0)), ms: 200 },
      ]);
    }
    if (path === '/api/patterns') {
      return jsonResponse(state.patterns);
    }
    if (path === '/api/pattern') {
      state.activePresetId = body.name || 'plasma';
      state.displayMode = 'plasma';
      return jsonResponse({ ok: true });
    }
    if (path === '/api/pattern/stop') {
      state.activePresetId = null;
      state.displayMode = null;
      return jsonResponse({ ok: true });
    }
    if (path === '/api/anim/custom') return jsonResponse(state.animCustom);
    if (path === '/api/anim/original') {
      const kind = u.searchParams.get('kind');
      if (kind === 'ring') return jsonResponse({ text: 'FRAME 120 255,106,0\nFRAME 120 0,180,255\n' });
      return jsonResponse({ frames: [{ flag: 0, v: makeLivePixelsAndRing().pixels, ms: 150 }] });
    }
    if (path === '/api/anim/install' || path === '/api/anim/reset') return jsonResponse(state.animCustom);
    if (path === '/api/anim/my') {
      const kind = body.kind === 'ring' ? 'ring' : 'clock';
      const name = 'my/' + String(body.name || 'demo_anim') + (kind === 'ring' ? '.led' : '.binscled.zst');
      if (!state.patterns[kind].includes(name)) state.patterns[kind].push(name);
      return jsonResponse({ name, patterns: state.patterns });
    }
    if (path === '/api/anim/my/delete') {
      const kind = body.kind === 'ring' ? 'ring' : 'clock';
      state.patterns[kind] = state.patterns[kind].filter(n => n !== body.name);
      return jsonResponse({ patterns: state.patterns });
    }
    if (path === '/api/ring') {
      if (body.red !== undefined) state.ringRgb = [Number(body.red) || 0, Number(body.green) || 0, Number(body.blue) || 0];
      return jsonResponse({ ok: true, rgb: state.ringRgb });
    }
    if (path === '/api/ring/frames') {
      return jsonResponse({
        loop: true,
        text: 'FRAME 150 255,106,0\nFRAME 150 255,40,120\n',
        frames: [
          { ms: 150, c: Array.from({ length: 24 }, (_, i) => i % 2 ? [255, 106, 0] : [255, 190, 90]) },
          { ms: 150, c: Array.from({ length: 24 }, (_, i) => i % 2 ? [90, 180, 255] : [255, 106, 0]) },
        ],
      });
    }
    if (path === '/api/ring/try' || path === '/api/ring/reset') return jsonResponse({ ok: true });
    if (path === '/api/viz') {
      return jsonResponse({ supported: true, installed: true, running: !!state.playing, outdated: false, bands: makeLivePixelsAndRing().spectrum.bands, rms: 0.42, error: '', age: 0.1, device: 'hw:0,0' });
    }

    // Player & Music & Glagol
    if (path === '/api/music') return jsonResponse(buildMusicPayload());
    if (path === '/api/music/cmd') {
      const cmd = body.command;
      if (cmd === 'play') {
        state.playing = true;
        state.progressAt = nowSec();
      } else if (cmd === 'stop') {
        state.progress = currentProgress();
        state.playing = false;
      } else if (cmd === 'next') {
        advanceTrack(1, false);
      } else if (cmd === 'prev') {
        advanceTrack(-1, false);
      } else if (cmd === 'rewind' && body.position !== undefined) {
        state.progress = Math.max(0, Math.min(currentTrack().duration, Number(body.position) || 0));
        state.progressAt = nowSec();
      } else if (cmd === 'setVolume' && body.volume !== undefined) {
        state.volume = Math.round(Number(body.volume) * 100);
      } else if (cmd === 'sendText') {
        showDemoToast(`🗣️ Сказано Алисе: «${body.text || ''}»`);
      }
      return jsonResponse({ ok: true });
    }
    if (path === '/api/music/play') {
      playTrackById(body.id);
      return jsonResponse({ ok: true, id: currentTrack().ytId });
    }
    if (path === '/api/music/like' || path === '/api/music/dislike') {
      const tr = currentTrack();
      if (path.endsWith('/dislike')) {
        state.likedYt.delete(tr.ytId);
        advanceTrack(1, false);
      } else {
        if (state.likedYt.has(tr.ytId)) state.likedYt.delete(tr.ytId);
        else state.likedYt.add(tr.ytId);
      }
      return jsonResponse({ ok: true, liked: state.likedYt.has(currentTrack().ytId) });
    }
    if (path === '/api/music/info') {
      const rawId = u.searchParams.get('id') || currentTrack().ytId;
      const clean = rawId.replace(/^ext:yt:/, '');
      const tr = TRACKS.find(t => t.ytId === clean || t.ymId === clean) || currentTrack();
      return jsonResponse({
        id: 'ext:yt:' + tr.ytId,
        title: tr.title,
        artists: tr.artist,
        artistList: [{ id: 'ext:yt:' + tr.artistId, name: tr.artist }],
        album: { id: tr.albumId, title: tr.album },
        cover: tr.cover,
        coverBig: tr.cover,
        duration: tr.duration,
        liked: state.likedYt.has(tr.ytId),
      });
    }
    if (path === '/api/music/lyrics') {
      const rawId = u.searchParams.get('id') || currentTrack().ytId;
      const clean = rawId.replace(/^ext:yt:/, '');
      const tr = TRACKS.find(t => t.ytId === clean || t.ymId === clean) || currentTrack();
      return jsonResponse({
        sync: tr.lyrics,
        text: tr.lyrics.map(l => l[1]).join('\n'),
        source: 'YouTube Music',
      });
    }
    if (path === '/api/music/queue') {
      const curIdx = Math.max(0, state.queueIndexes.indexOf(state.trackIndex));
      return jsonResponse({
        title: 'Шуточная очередь эмулятора',
        current: curIdx,
        tracks: state.queueIndexes.map(i => toQueueTrack(TRACKS[i])),
      });
    }
    if (path === '/api/music/similar') {
      return jsonResponse(TRACKS.filter((_, i) => i !== state.trackIndex).slice(0, 5).map(toQueueTrack));
    }
    if (path === '/api/music/artist') {
      const bid = (u.searchParams.get('id') || '').replace(/^ext:yt:/, '');
      const nameQ = (u.searchParams.get('artist') || '').toLowerCase();
      const art = ARTISTS.find(a => a.browse_id === bid || (nameQ && a.name.toLowerCase().includes(nameQ))) || ARTISTS[0];
      return jsonResponse({
        ...art,
        similar: ARTISTS.filter(a => a.browse_id !== art.browse_id).slice(0, 4),
        popular: TRACKS.map(toQueueTrack).slice(0, 5),
      });
    }

    // ExtMusic (YouTube Music hub)
    if (path === '/api/extmusic/status') return jsonResponse(buildExtStatusPayload());
    if (path === '/api/extmusic/hw') {
      if (method === 'POST') {
        if (body.osd_enabled !== undefined) state.osdEnabled = !!body.osd_enabled;
        if (body.remote_enabled !== undefined) state.remoteEnabled = !!body.remote_enabled;
        if (body.remote_dpad_hdmi !== undefined) state.remoteDpadHdmi = !!body.remote_dpad_hdmi;
        if (body.normalize_enabled !== undefined) state.normalizeEnabled = !!body.normalize_enabled;
      }
      return jsonResponse(buildHwPayload());
    }
    if (path === '/api/extmusic/dlna') {
      if (method === 'POST' && body.enabled !== undefined) state.dlnaEnabled = !!body.enabled;
      return jsonResponse({
        ok: true, enabled: state.dlnaEnabled, running: state.dlnaEnabled,
        friendly_name: 'Яндекс Станция Макс (Demo)', port: 8789,
        device_url: 'http://192.168.1.144:8789/dlna/device.xml',
        udn: 'uuid:yapanel-max-demo',
      });
    }
    if (path === '/api/extmusic/multiroom') {
      if (method === 'POST') {
        if (body.enabled !== undefined) state.multiroomEnabled = !!body.enabled;
        if (body.sync_volume !== undefined) state.multiroomSyncVol = !!body.sync_volume;
        if (body.action === 'add' && body.url) {
          state.multiroomFollowers.push({ id: 'mr' + Date.now(), name: body.name || 'Колонка', url: body.url, offset_ms: body.offset_ms || 0, enabled: true, last_status: 'ok', latency_ms: 6 });
        } else if (body.action === 'remove') {
          state.multiroomFollowers = state.multiroomFollowers.filter(f => f.id !== body.id);
        } else if (body.action === 'update') {
          const f = state.multiroomFollowers.find(x => x.id === body.id);
          if (f && body.enabled_follower !== undefined) f.enabled = !!body.enabled_follower;
        }
      }
      return jsonResponse({ ok: true, enabled: state.multiroomEnabled, sync_volume: state.multiroomSyncVol, followers: state.multiroomFollowers, suggestions: [] });
    }
    if (path === '/api/extmusic/play') {
      const tr = body.track || {};
      playTrackById(tr.id || tr.video_id);
      if (Array.isArray(body.queue) && body.queue.length) {
        const nextQ = body.queue.map(q => TRACKS.findIndex(t => t.ytId === (q.video_id || q.id))).filter(i => i >= 0);
        if (nextQ.length) state.queueIndexes = nextQ;
      }
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/stop') {
      state.progress = currentProgress();
      state.playing = false;
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/shuffle') {
      state.shuffle = body.enable !== undefined ? !!body.enable : !state.shuffle;
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/autoplay') {
      state.autoplayRadio = body.enabled !== undefined ? !!body.enabled : !state.autoplayRadio;
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/radio') {
      state.queueIndexes = TRACKS.map((_, i) => i).sort(() => Math.random() - 0.5);
      state.trackIndex = state.queueIndexes[0];
      state.progress = 0;
      state.progressAt = nowSec();
      state.playing = true;
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/queue/add') {
      const tr = body.track || {};
      const idx = TRACKS.findIndex(t => t.ytId === (tr.video_id || tr.id));
      if (idx >= 0) {
        if (body.mode === 'next') {
          const curAt = state.queueIndexes.indexOf(state.trackIndex);
          state.queueIndexes.splice(curAt + 1, 0, idx);
        } else {
          state.queueIndexes.push(idx);
        }
      }
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/queue/remove' && body.index !== undefined) {
      const idx = Number(body.index);
      if (idx >= 0 && idx < state.queueIndexes.length) state.queueIndexes.splice(idx, 1);
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/queue/clear') {
      state.queueIndexes = [state.trackIndex];
      return jsonResponse(buildExtStatusPayload());
    }
    if (path === '/api/extmusic/yt/like') {
      const vid = String(body.id || '');
      if (body.like) state.likedYt.add(vid);
      else state.likedYt.delete(vid);
      return jsonResponse({ ok: true, liked: state.likedYt.has(vid) });
    }
    if (path === '/api/extmusic/yt/account') {
      return jsonResponse({ ok: true, configured: true, authenticated: true, account: 'boris.the.cat@gmail.com', auth_type: 'oauth' });
    }
    if (path === '/api/extmusic/yt/account/liked') {
      return jsonResponse({ tracks: TRACKS.filter(t => state.likedYt.has(t.ytId)).map(toYtTrack) });
    }
    if (path === '/api/extmusic/yt/account/library') {
      return jsonResponse({
        configured: true,
        account: 'boris.the.cat@gmail.com',
        liked: { title: 'Понравившаяся музыка', subtitle: `${state.likedYt.size} треков`, kind: 'liked', browse_id: 'VLLM', thumb: TRACKS[0].cover },
        playlists: MIXES,
        artists: ARTISTS.slice(0, 6),
      });
    }
    if (path === '/api/extmusic/yt/home') {
      return jsonResponse({
        recent: TRACKS.slice(0, 4).map(toYtTrack),
        quick_picks: TRACKS.map(t => toYtTrack(t)),
        mixes: MIXES,
        feed: TRACKS.slice(4).map(toYtTrack),
      });
    }
    if (path === '/api/extmusic/yt/charts') {
      return jsonResponse({
        tracks: TRACKS.map((t, i) => toYtTrack(t, i + 1)),
        artists: ARTISTS.slice(0, 6),
        videos: TRACKS.slice(0, 4).map(toYtTrack),
      });
    }
    if (path === '/api/extmusic/yt/explore') {
      return jsonResponse({
        releases: MIXES,
        moods: [
          { title: 'Поздняя отладка и чай', color: '#ff6a00', token: 'mood_debug' },
          { title: 'Сабвуфер 48 Гц', color: '#3b82f6', token: 'mood_bass' },
          { title: 'Кот Борис спит', color: '#10b981', token: 'mood_cat' },
          { title: 'Ремонт у соседа', color: '#ef4444', token: 'mood_drill' },
        ],
      });
    }
    if (path === '/api/extmusic/yt/mood' || path === '/api/extmusic/yt/account/mood') {
      return jsonResponse({ title: 'Тематическая подборка', tracks: TRACKS.map(toYtTrack), collections: MIXES.slice(0, 3) });
    }
    if (path === '/api/extmusic/yt/collection') {
      const bid = u.searchParams.get('browse_id') || '';
      const mix = MIXES.find(m => m.browse_id === bid);
      if (mix) {
        return jsonResponse({ ...mix, tracks: mix.trackIndexes.map(i => toYtTrack(TRACKS[i])) });
      }
      const art = ARTISTS.find(a => a.browse_id === bid);
      if (art) {
        return jsonResponse({
          ...art,
          tracks: TRACKS.filter(t => t.artistId === art.browse_id).concat(TRACKS.slice(0, 3)).map(toYtTrack),
          albums: MIXES.slice(0, 2),
          similar: ARTISTS.filter(a => a.browse_id !== art.browse_id).slice(0, 4),
        });
      }
      return jsonResponse({ kind: 'playlist', browse_id: bid, title: 'Избранный микс', tracks: TRACKS.map(toYtTrack) });
    }
    if (path === '/api/extmusic/yt/search') {
      const q = (u.searchParams.get('q') || '').toLowerCase();
      const matched = TRACKS.filter(t => (t.title + ' ' + t.artist + ' ' + t.album).toLowerCase().includes(q));
      return jsonResponse({ items: (matched.length ? matched : TRACKS).map(toYtTrack) });
    }

    // Party Lights (Tuya)
    if (path === '/api/party/status') {
      return jsonResponse({ running: state.party.running, master_enabled: true, workers_count: state.party.bulbs.filter(b => b.enabled).length, bulbs: state.party.bulbs });
    }
    if (path === '/api/party/bulbs') return jsonResponse(state.party.bulbs);
    if (path === '/api/party/settings') {
      if (method === 'POST') Object.assign(state.party, body);
      return jsonResponse({ mode: state.party.mode, preset: state.party.preset, speed: state.party.speed, bright: state.party.bright, auto_bright: state.party.auto_bright, fade_ms: state.party.fade_ms, sensitivity: state.party.sensitivity });
    }
    if (path === '/api/party/service') {
      if (body.action === 'start') state.party.running = true;
      else if (body.action === 'stop') state.party.running = false;
      return jsonResponse({ running: state.party.running });
    }
    if (path === '/api/party/bulbs/scan') return jsonResponse([]);
    if (path === '/api/party/bulbs/add') {
      state.party.bulbs.push({ id: body.id || ('b' + Date.now()), name: body.name || 'Новая лампа', ip: body.ip || '192.168.1.209', key: body.key || 'demo', role: body.role || 'ambient', side: body.side || 'center', pos_x: body.pos_x ?? 0.5, ver: body.ver || '3.3', enabled: true, online: true });
      return jsonResponse({ ok: true });
    }
    if (path === '/api/party/bulbs/delete') {
      state.party.bulbs = state.party.bulbs.filter(b => b.id !== body.id);
      return jsonResponse({ ok: true });
    }
    if (path === '/api/party/bulbs/test' || path.startsWith('/api/party/')) return jsonResponse({ ok: true });

    // Assistant & Wakewords (yam2)
    if (path === '/api/assistant') {
      return jsonResponse({
        supported: true, installed: true, running: true, autostart: true, outdated: false,
        config: { enabled: state.assistant.enabled, mode: state.assistant.mode, model: state.assistant.model, keys: { openai: 'sk-demo-****' }, llm: { proxy: '', proxy_set: false, key: 'demo' }, tts: { key: '' } },
        state: { pid: 5491, version: 'max-1.0.0-demo', state: 'idle', error: '', config_error: '', enabled: state.assistant.enabled, history: state.assistant.history, at: nowSec(), mode: state.assistant.mode, route: false },
      });
    }
    if (path === '/api/assistant/ask') {
      const q = String(body.text || body.phrase || '').trim();
      const answers = [
        'Колонка чувствует себя отлично! Кот Борис тоже, но проверяет кабель питания.',
        'Громкость нормализована до −14 LUFS. Шкаф больше не открывается от сабвуфера.',
        'Согласно датчикам, сосед начал сверлить ровно 12 секунд назад. Рекомендую включить DJ Несущая Стена.',
        'На Первой Станции уже кончилась бы память, а у нас на Максе 2 ГБ и полёт отличный!',
      ];
      const a = answers[Math.abs(q.length) % answers.length];
      state.assistant.history.unshift({ q: q || 'Привет!', a });
      return jsonResponse({ ok: true, answer: a, history: state.assistant.history.slice(0, 10) });
    }
    if (path.startsWith('/api/assistant/')) return jsonResponse({ ok: true });
    if (path === '/api/yam2/status') {
      return jsonResponse({ installed: state.yam2.installed, version: state.yam2.version, latest: state.yam2.latest, update: false, platform: state.yam2.platform, host: state.yam2.host, registries: [{ name: 'official', url: 'https://yam.yastation.demo/registry.json' }], latest_url: 'https://github.com/Timobzdecc/yapanel-web_max' });
    }
    if (path === '/api/yam2/packages') return jsonResponse({ packages: state.yam2.packages });
    if (path === '/api/yam2/catalog') return jsonResponse({ configured: true, packages: state.yam2.catalog });
    if (path === '/api/yam2/log') return jsonResponse({ text: '[yam2 demo] wakeword listening ("Джарвис", "Борис") · CPU 1.4% · OK\n' });
    if (path.startsWith('/api/yam2/')) return jsonResponse({ ok: true, installed_tag: state.yam2.version });

    // Bluetooth, Sounds, Phrases, Privacy, Footprint, Agent, Hook
    if (path === '/api/bt') {
      if (method === 'POST' && body.powered !== undefined) state.btPowered = !!body.powered;
      return jsonResponse({
        powered: state.btPowered, keep_off: state.btKeepOff, discoverable: false, pairable: true,
        alias: 'Яндекс Станция Макс (Demo)', pair_until: null,
        devices: [
          { mac: '54:A9:C8:97:C1:4E', name: 'Yandex Remote 2 (Пульт)', icon: 'phone', paired: true, trusted: true, connected: true, battery: 88 },
          { mac: 'AA:BB:CC:DD:EE:01', name: 'Наушники от соседа', icon: 'audio-headphones', paired: true, trusted: true, connected: false, battery: null },
        ],
      });
    }
    if (path === '/api/bt/memes') {
      return jsonResponse({
        supported: true, installed: state.btMemesInstalled, running: state.btMemesRunning, autostart: true, version: '2.0.0',
        sounds: {
          on: { name: 'meows_on.opus', size: 2583, present: true, target_size: 2583 },
          off: { name: 'meows_off.opus', size: 2644, present: true, target_size: 2644 },
          silent: { name: 'silent.mp3', size: 10797, present: true, target_size: 10797 },
        },
      });
    }
    if (path === '/api/sounds') return jsonResponse(['common/boot.wav', 'common/alarm.mp3', 'common/bluetooth_turning_on_jingle.opus']);
    if (path === '/api/sounds/custom') return jsonResponse({ custom: [] });
    if (path === '/api/phrases') {
      if (method === 'POST' && Array.isArray(body.phrases)) state.phrases = body.phrases.map((p, i) => ({ id: p.id || ('ph' + Date.now() + i), ...p }));
      return jsonResponse({ phrases: state.phrases, greeting: state.greeting, limit: 20 });
    }
    if (path === '/api/phrases/greeting') {
      if (method === 'POST') Object.assign(state.greeting, body);
      return jsonResponse({ phrases: state.phrases, greeting: state.greeting, limit: 20 });
    }
    if (path === '/api/phrases/say' || path === '/api/phrases/greeting/say' || path === '/api/sounds/play-original') {
      showDemoToast('🔊 Воспроизведено на эмуляторе Станции Макс!');
      return jsonResponse({ ok: true });
    }
    if (path === '/api/privacy') return jsonResponse({ ...state.privacy, hosts: ['clck.yandex.net', 'appmetrica.yandex.net', 'metrica.yandex.net'], queued: 0 });
    if (path === '/api/footprint') return jsonResponse({ items: [{ key: 'anims', name: 'Свои анимации кольца', on: true, detail: 'своих: 1' }, { key: 'privacy', name: 'Блокировка аналитики', on: true }] });
    if (path === '/api/agent') return jsonResponse({ network: true, installed: true, active: true, version: '0.6.1', outdated: false, boot: true });
    if (path === '/api/hook') return jsonResponse({ supported: true, active: true, pid: 5133 });

    // Charts & Metrics
    if (path === '/api/metrics') {
      const n = 45, nowT = Math.floor(nowSec());
      const tArr = [], cpu = [], temp = [], mem = [], rssi = [], disk = [], load = [], play = [];
      for (let i = 0; i < n; i++) {
        tArr.push(nowT - (n - i) * 20);
        cpu.push(Number((18 + 14 * Math.abs(Math.sin(i * 0.4))).toFixed(1)));
        temp.push(Number((47 + 2.5 * Math.sin(i * 0.25)).toFixed(1)));
        mem.push(740 + (i % 6) * 14);
        rssi.push(-44 - (i % 4));
        disk.push(2640.5);
        load.push(Number((0.9 + 0.3 * Math.cos(i * 0.3)).toFixed(2)));
        play.push(i > 8 ? 1 : 0);
      }
      return jsonResponse({ t: tArr, cpu, temp, mem, rssi, disk, load, play, events: [{ t: nowT - 480, kind: 'online', label: 'Эмулятор запущен' }], totals: { mem: 1990, disk: 7800, uptime: 4200, play_pct: 82 }, step: 20, from: tArr[0], to: nowT, interval: 20, keep: 86400 });
    }

    // Logs & Report
    if (path === '/api/logs') return jsonResponse(['yapanel.log', 'assistant.log', 'maind.log', 'dmesg']);
    if (path === '/api/log') {
      const name = u.searchParams.get('name') || 'panel';
      return jsonResponse({
        text: [
          `[2026-11-21 17:05:12] [INFO] Демо-режим эмулятора Станции Макс (${name})`,
          `[2026-11-21 17:05:14] [INFO] GstBridge: Нормализация YouTube Music активна (−14 LUFS, clamp −16..+4 dB)`,
          `[2026-11-21 17:05:18] [INFO] Нарушитель тишины: «DJ Несущая Стена» (${state.normalizeEnabled ? 'приглушено до −14 LUFS' : 'на полную катушку!'})`,
          `[2026-11-21 17:05:25] [WARN] Кот Борис замечен в 15 см от кабеля роутера`,
        ].join('\n'),
      });
    }
    if (path === '/api/report') {
      return jsonResponse({ text: '### Отчёт об эмуляторе YapAnel v0.6.1\n- Платформа: In-Browser Demo Emulator (GitHub Pages)\n- Кот Борис: спит на роутере\n- Проблем не обнаружено!' });
    }

    // Filesystem
    if (path === '/api/fs/list') {
      const dir = u.searchParams.get('path') || '/data/yapanel';
      const entries = (state.fs[dir] || state.fs['/data/yapanel']).map(({ name, is_dir, size, mtime, mode }) => ({
        name, is_dir, type: is_dir ? 'dir' : 'file', size, mtime, mode,
      }));
      return jsonResponse({ path: state.fs[dir] ? dir : '/data/yapanel', writable: true, entries });
    }
    if (path === '/api/fs/read') {
      const fp = u.searchParams.get('path') || '';
      const slash = fp.lastIndexOf('/');
      const dir = fp.slice(0, slash) || '/';
      const name = fp.slice(slash + 1);
      const item = (state.fs[dir] || []).find(x => x.name === name);
      return jsonResponse({ text: item ? (item.content || '') : '# Пустой файл', binary: false, truncated: false });
    }
    if (path === '/api/fs/write') {
      const fp = String(body.path || '');
      const slash = fp.lastIndexOf('/');
      const dir = fp.slice(0, slash) || '/';
      const name = fp.slice(slash + 1);
      if (state.fs[dir]) {
        const ex = state.fs[dir].find(x => x.name === name);
        if (ex) { ex.content = String(body.text || ''); ex.size = ex.content.length; }
        else state.fs[dir].push({ name, is_dir: false, size: String(body.text || '').length, mtime: Math.floor(nowSec()), mode: '-rw-r--r--', content: String(body.text || '') });
      }
      return jsonResponse({ ok: true });
    }
    if (path === '/api/fs/mkdir' || path === '/api/fs/rename' || path === '/api/fs/delete' || path === '/api/fs/play') {
      return jsonResponse({ ok: true });
    }

    return jsonResponse({ ok: true });
  }

  // ---------- Install Mock PanelContext, EventSource & WebSocket ----------
  window.PanelContext = Object.freeze({
    basePath: BASE.pathname,
    deviceId: '',
    url: path => new URL(String(path).replace(/^\//, ''), BASE).href,
    fetch: (path, options) => {
      const p = String(path || '');
      if (p.startsWith('/api/') || p.includes('/api/')) return routeApi(p, options);
      if (ORIG_FETCH) return ORIG_FETCH(new URL(p.replace(/^\//, ''), BASE).href, options);
      return Promise.resolve(jsonResponse({}));
    },
    websocket: path => 'wss://demo.local' + String(path || ''),
    storageKey: key => 'demo.' + key,
  });

  // Mock EventSource for /api/leds/stream
  window.EventSource = function DemoEventSource(url) {
    this.url = url;
    this.readyState = 1;
    const self = this;
    const timer = setInterval(() => {
      if (self.readyState !== 1) return;
      if (typeof self.onmessage === 'function') {
        self.onmessage({ data: JSON.stringify(makeLivePixelsAndRing()) });
      }
    }, 80);
    setTimeout(() => { if (typeof self.onopen === 'function') self.onopen({}); }, 20);
    this.close = () => { self.readyState = 2; clearInterval(timer); };
  };

  // Mock WebSocket for /api/term (xterm.js root console)
  window.WebSocket = function DemoWebSocket(url) {
    const strUrl = String(url || '');
    if (!strUrl.includes('/api/term') && ORIG_WS) return new ORIG_WS(strUrl);
    const self = this;
    this.url = strUrl;
    this.readyState = 0;
    this.binaryType = 'arraybuffer';
    const enc = new TextEncoder();
    let lineBuf = '';

    const emit = str => {
      if (typeof self.onmessage === 'function') {
        self.onmessage({ data: enc.encode(str).buffer });
      }
    };
    const promptStr = '\x1b[1;32myandexmax\x1b[0m:\x1b[1;34m/data/yapanel\x1b[0m# ';

    setTimeout(() => {
      self.readyState = 1;
      if (typeof self.onopen === 'function') self.onopen({});
      emit(
        '\x1b[38;5;208m╔══════════════════════════════════════════════════════════════╗\x1b[0m\r\n'
        + '\x1b[38;5;208m║\x1b[0m  \x1b[1mYapAnel Root Emulator v0.6.1\x1b[0m · Yandex Station Max (YNDX-0008) \x1b[38;5;208m║\x1b[0m\r\n'
        + '\x1b[38;5;208m║\x1b[0m  \x1b[90mВведите \x1b[1;37mhelp\x1b[0;90m, \x1b[1;37mboris\x1b[0;90m, \x1b[1;37mps\x1b[0;90m, \x1b[1;37mtinymix\x1b[0;90m или \x1b[1;37muname -a\x1b[0m          \x1b[38;5;208m║\x1b[0m\r\n'
        + '\x1b[38;5;208m╚══════════════════════════════════════════════════════════════╝\x1b[0m\r\n'
        + promptStr
      );
    }, 60);

    function runCmd(cmd) {
      const c = cmd.trim();
      if (!c) return '';
      if (c === 'help') return '\r\nДоступные команды: \x1b[36mhelp, id, uname -a, ls, ps, free -m, df -h, tinymix, dmesg, boris, clear\x1b[0m\r\n';
      if (c === 'id') return '\r\nuid=0(root) gid=0(root) groups=0(root) context=u:r:su:s0 (CapEff: 0000001fffffffff)\r\n';
      if (c.startsWith('uname')) return '\r\nLinux Yandex-Station-Max 4.9.113 #1 SMP PREEMPT aarch64 GNU/Linux\r\n';
      if (c.startsWith('ls')) return '\r\n\x1b[34mextmusic/\x1b[0m  \x1b[34mstatic/\x1b[0m  \x1b[32mserver.py\x1b[0m  dsp_max.py  secret_cat_photos.txt  who_ate_the_sausage.log\r\n';
      if (c === 'ps' || c.startsWith('ps ')) return '\r\nroot   5133  com.yandex.io.sdk [frida: norm_enabled=1]\r\nroot   7642  python3 server.py --port 8765 --model max\r\nroot   7837  python3 assistant_max.py\r\ncat       1  boris_router_watcher --sleep-on-modem\r\n';
      if (c.startsWith('free')) return '\r\n              total        used        free      shared  buff/cache   available\r\nMem:           1990         740         520          18         730        1156\r\n';
      if (c.startsWith('df')) return '\r\nFilesystem      Size  Used Avail Use% Mounted on\r\n/dev/block/data 7.8G  2.6G  5.2G  34% /data\r\n';
      if (c === 'tinymix') return '\r\nTAS5805M DSP Profile: ' + state.dspProfile + ' | Normalization: ' + (state.normalizeEnabled ? '-14 LUFS' : 'OFF') + ' | Subsonic: 48Hz\r\n';
      if (c === 'dmesg') return '\r\n[ 4210.12] [DSP] Anti-Neighbor Subsonic 48Hz armed\r\n[ 4211.00] [CAT] Boris paw detected near WiFi antenna (RSSI -44 dBm)\r\n';
      if (c === 'boris') return '\r\n\x1b[33m /\\_/\\\r\n( o.o )  «Мяу! Поставь лайк треку про роутер, иначе выдерну кабель!»\r\n > ^ <\x1b[0m\r\n';
      if (c === 'clear') return '\x1b[2J\x1b[H';
      if (c.startsWith('rm -rf')) return '\r\n\x1b[31mОтказано! Агент Кот Борис заблокировал саморазрушение колонки.\x1b[0m\r\n';
      return `\r\nsh: ${c.split(' ')[0]}: выполнено в песочнице (в демо можно всё, даже не сломать boot.img!)\r\n`;
    }

    this.send = payload => {
      try {
        const msg = JSON.parse(payload);
        if (msg.t !== 'i') return;
        for (const ch of String(msg.d || '')) {
          if (ch === '\r' || ch === '\n') {
            const out = runCmd(lineBuf);
            lineBuf = '';
            emit(out + promptStr);
          } else if (ch === '\x7f' || ch === '\b') {
            if (lineBuf.length) { lineBuf = lineBuf.slice(0, -1); emit('\b \b'); }
          } else if (ch === '\x03') {
            lineBuf = '';
            emit('^C\r\n' + promptStr);
          } else if (ch === '\x0c') {
            lineBuf = '';
            emit('\x1b[2J\x1b[H' + promptStr);
          } else if (ch >= ' ') {
            lineBuf += ch;
            emit(ch);
          }
        }
      } catch (_) {}
    };
    this.close = () => { self.readyState = 3; if (typeof self.onclose === 'function') self.onclose({}); };
  };

  // ---------- Floating Demo Pill Banner ----------
  function mountDemoPill() {
    if (document.getElementById('yapanel-demo-pill')) return;
    const style = document.createElement('style');
    style.textContent = `
      #yapanel-demo-pill {
        position: fixed; right: 14px; top: 12px; z-index: 9999;
        display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
        padding: 6px 12px; border-radius: 999px;
        background: rgba(20, 20, 26, 0.88); backdrop-filter: blur(12px);
        border: 1px solid rgba(255, 106, 0, 0.45); color: #f5f5f7;
        font: 600 12px/1.2 Manrope, system-ui, sans-serif;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.35);
      }
      #yapanel-demo-pill button, #yapanel-demo-pill a {
        border: 0; border-radius: 999px; padding: 4px 10px;
        font: inherit; font-weight: 700; cursor: pointer; text-decoration: none;
      }
      #yapanel-demo-synth { background: rgba(255, 255, 255, 0.12); color: #fff; }
      #yapanel-demo-synth.on { background: #ff6a00; color: #111; }
      #yapanel-demo-gh { background: rgba(255, 106, 0, 0.18); color: #ffb37a; }
      @media (max-width: 760px) {
        #yapanel-demo-pill { top: auto; bottom: 72px; right: 8px; left: 8px; justify-content: center; font-size: 11px; }
      }
    `;
    document.head.appendChild(style);
    const pill = document.createElement('div');
    pill.id = 'yapanel-demo-pill';
    pill.innerHTML = '<span>🕹️ Демо-эмулятор Станции Макс</span>'
      + '<button type="button" id="yapanel-demo-synth">🔇 Звук-синт: ВЫКЛ</button>'
      + '<a id="yapanel-demo-gh" href="https://github.com/Timobzdecc/yapanel-web_max" target="_blank" rel="noopener">GitHub ↗</a>';
    document.body.appendChild(pill);
    const btn = document.getElementById('yapanel-demo-synth');
    btn.addEventListener('click', () => {
      setSynth(!synthEnabled);
      btn.classList.toggle('on', synthEnabled);
      btn.textContent = synthEnabled ? '🔊 Звук-синт: ВКЛ' : '🔇 Звук-синт: ВЫКЛ';
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountDemoPill);
  else mountDemoPill();
})();
