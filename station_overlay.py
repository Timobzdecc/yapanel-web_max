"""Проверяемые локальные overlays для Station Max."""
from __future__ import annotations

import hashlib
from pathlib import Path


V0311_SERVER_SHA256 = "6076708994bf4820d97490f7b79f659e8eec02641013ea8b4c0fc6af644c1dd9"
PATH_MARKER = "export PATH=/data/yapanel/bin:$PATH"
SERVER_MARKER = "# station-max-update-overlay-v2"
SUPERVISOR_MARKER = "# station-max-supervisor-v1"
HEALTH_MARKER = "# station-max-health-overlay-v1"
STATIC_MARKER = "/* station-max-update-ui-v2 */"
MAX_PROFILE_UPSTREAM_SHA256 = {
    "0.3.11": "49e7ea5e6d132abb9b91134f85d7ebd5d49159dde38abf02737cbe486018b098",
}
MAX_PROFILE_OVERLAY_SHA256 = {
    "0.3.11": "1a411df1621e172e3379e1760f6ec86010fd10dc15959e6a1f7789996e8f9d64",
}
MAX_PROFILE_ANCHOR = "    def max_hook_status(self):"
OVERLAY_ID = "station-max-local-path-v2"
BRIDGE_IMPORT = "from panel_update_integration import install as _install_station_updater"
BRIDGE_CALL = "_install_station_updater(PanelUpdater)"


STATIC_UPDATE_BLOCK = r'''/* station-max-update-ui-v2 */
if ($('#upd-go')) $('#upd-go').onclick = e => {
  if (!confirm(`Обновить панель до ${panelVer.latest}? Сервер перезапустится, страница обновится сама.`)) return;
  const btn = e.currentTarget;
  return act(btn, async () => {
    btn.textContent = 'Обновляем…';
    try {
      const updated = await api('/api/panel/update', {});
      toast('Обновление подготовлено, проверяем запуск…');
      const until = Date.now() + 60000;
      await new Promise(r => setTimeout(r, 2500));
      while (Date.now() < until) {
        const response = await PanelContext.fetch('/api/panel/update/status');
        if (response.ok) {
          const status = await response.json();
          if (status.state === 'healthy' && status.version === updated.version) { location.reload(); return; }
          if (['blocked', 'rolled_back', 'rollback_failed'].includes(status.state)) {
            throw new Error(status.error || 'проверка обновления не пройдена');
          }
        }
        await new Promise(r => setTimeout(r, 1000));
      }
      throw new Error('Сервер не прошёл readiness check за минуту');
    } finally { btn.textContent = 'Обновить'; }
  });
};
'''


MAX_PROFILE_BLOCK = r'''    def max_assistant_hook_fallback(self, users):
        """Проверяет существующий worker автозапуска ассистента на Android."""
        if "ассистент" not in users or getattr(self.adb, "is_local", False) is not True:
            return False
        out, _, _ = self.adb.shell(
            f"[ -x /system/bin/sh ] && [ -d /data/quasar ] && "
            f"[ -f {s.ASSIST_CONF} ] && [ -x {s.ASSISTD} ] && "
            f"[ ! -f {s.ASSIST_NO_AUTOSTART} ] && echo assistant-worker || true",
            check=False,
        )
        return "assistant-worker" in out.split()

    def max_hook_status(self):
        """Показывает статус boot hook для Android TV 9 / Station Max.

        Свежесть проверяется по реальному MD5, а boot-route - read-only
        проверкой caller. Одного наличия boot_hook.sh недостаточно, чтобы
        подтвердить его выполнение после перезагрузки.
        """
        expected = hashlib.md5(max_boot_hook_script().encode("utf-8")).hexdigest()
        possible = True
        reason = ""
        try:
            self._boot_route()
        except AdbError as exc:
            possible = False
            reason = str(exc)
        out, _, _ = self.adb.shell(
            f"{self._hook_users_script()}; [ -f {s.BOOT_HOOK} ] && echo hook; "
            f"md5sum {s.BOOT_HOOK} 2>/dev/null; true", check=False)
        out = s.strip_ansi(out)
        flags = set(out.split())
        users = [l.strip()[1:] for l in out.splitlines() if l.strip().startswith("@")]
        hook = "hook" in flags
        fresh = expected in out
        assistant_fallback = self.max_assistant_hook_fallback(users)
        required_users = [user for user in users if not (user == "ассистент" and assistant_fallback)]
        if assistant_fallback and not required_users:
            reason = "boot_hook не требуется: автозапуск ассистента выполняет worker панели"
        return {
            "hook": hook,
            "fresh": hook and fresh,
            "users": users,
            "possible": possible,
            "supported": True,
            "reason": reason,
            "broken": bool(required_users) and not (hook and fresh),
            "rootkeeper": "/odm/bin/adbd_watchdog.sh",
            "file": "/data/quasar/webpanel/boot_hook.sh",
        }

    def max_ensure_hook(self, remount=False):
        """Write the Station Max boot_hook.sh (Android TV 9, no Buildroot parts)."""
        # Не записываем файл, пока не доказано, что Android действительно
        # вызывает его при загрузке. Иначе repair может убрать предупреждение,
        # но оставить хук невыполняемым после перезагрузки.
        self._boot_route()
        hook_script = max_boot_hook_script()
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "hook"
            with open(f, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(hook_script)
            self.adb.shell(f"mkdir -p {s.WEB_DIR}")
            self.adb.push(f, s.BOOT_HOOK)
        self.adb.shell(f"chmod 755 {s.BOOT_HOOK}; true")

    def max_hook_repair(self, remount=False):
        self._ensure_hook(remount=False)
        return self.hook_status()

    def max_boot_route(self):
        """Подтверждает реальный Android boot caller для boot_hook.sh."""
        caller = shlex.quote(s.ROOTKEEPER_CALLER)
        hook = shlex.quote(s.BOOT_HOOK)
        command = (
            f"[ -f {caller} ] && "
            f"sed '/^[[:space:]]*#/d' {caller} 2>/dev/null | "
            f"grep -F {hook} >/dev/null 2>&1 && echo watchdog || exit 3"
        )
        out, err, rc = self.adb.shell(command, check=False)
        if rc or out.strip() != "watchdog":
            detail = err.strip() or "caller не найден или не вызывает boot_hook.sh"
            raise AdbError(
                f"Автозапуск boot_hook.sh не подтверждён: {s.ROOTKEEPER_CALLER}: {detail}"
            )
        return "watchdog"

    def max_hook_users_script(self):
        """Android TV 9 hook users (strictly no Buildroot daemons)."""
        users = [
            ("заставка", s.BOOT_CUSTOM),
            ("звук запуска", s.SOUND_CUSTOM),
            ("блокировка аналитики", s.HOSTS_CUSTOM),
            ("звуки Bluetooth", s.BT_MEMES_AUTOSTART),
            ("выключенный bluetooth", s.BT_OFF),
            ("агент", s.AGENT_TOKENS),
            ("ассистент", s.ASSIST_CONF),
            ("слова активации", s.WAKE_AUTOSTART),
            ("активация Алисы (Frida)", s.WAKE_FRIDA_AUTOSTART),
        ]
        commands = []
        for name, path in users:
            condition = f"[ -f {path} ]"
            if name == "ассистент":
                condition += f" && [ ! -f {s.ASSIST_NO_AUTOSTART} ]"
            commands.append(f"{condition} && echo {shlex.quote('@' + name)}")
        cmd = "; ".join(commands)
        cmd += f"; find {s.OVR_DIR}/sounds -type f 2>/dev/null | grep -q . && echo '@свои звуки'"
        cmd += f"; find {s.OVR_DIR}/ledpatterns -type f 2>/dev/null | grep -q . && echo '@свои анимации'"
        return cmd

'''


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _has_supervisor_protocol(source):
    required = (
        "PANEL_SUPERVISOR_MODE",
        "RESTART_HOOK",
        "os._exit(75)",
    )
    return all(item in source for item in required) and (
        "def _supervised_restart" in source or "def _station_supervised_restart" in source
    )


def _inject_bridge(source):
    if BRIDGE_CALL in source and BRIDGE_IMPORT in source:
        return source
    if "class PanelUpdater:" not in source:
        raise RuntimeError("Overlay Station Max не нашёл class PanelUpdater")
    anchor = "\n\n# Платформа колонки Станция Макс"
    if source.count(anchor) != 1:
        raise RuntimeError("Overlay Station Max не нашёл однозначный конец PanelUpdater")
    hook = (
        f"\n\n{SERVER_MARKER}\n"
        f"{BRIDGE_IMPORT}\n"
        f"{BRIDGE_CALL}\n"
    )
    return source.replace(anchor, hook + anchor, 1)


def _inject_supervisor(source):
    if _has_supervisor_protocol(source):
        return source
    anchor = "RESTART_HOOK = None   # (аргументы без имени скрипта) → перезапуск снаружи, см. android/"
    if source.count(anchor) != 1:
        raise RuntimeError(
            "Overlay Station Max не нашёл проверяемый supervisor protocol; staging заблокирован"
        )
    block = (
        f"{SUPERVISOR_MARKER}\n"
        "def _station_supervised_restart(_args):\n"
        "    sys.stdout.flush()\n"
        "    sys.stderr.flush()\n"
        "    os._exit(75)\n"
        "\n"
        'RESTART_HOOK = _station_supervised_restart if os.environ.get("PANEL_SUPERVISOR_MODE") == "1" else None'
    )
    return source.replace(anchor, block, 1)


def _inject_server(source, version, server_path):
    source = _inject_bridge(source)
    source = _inject_supervisor(source)
    if BRIDGE_CALL not in source or not _has_supervisor_protocol(source):
        raise RuntimeError("Overlay Station Max не подтвердил bridge и supervisor protocol")

    if PATH_MARKER not in source:
        if str(version) == "0.3.11" and _sha256(server_path) != V0311_SERVER_SHA256:
            raise RuntimeError("Overlay Station Max несовместим с upstream server.py 0.3.11")
        needle = (
            "    def shell(self, cmd, timeout=15, check=True):\n"
            "        if getattr(self, \"is_local\", False):\n"
        )
        replacement = (
            "    def shell(self, cmd, timeout=15, check=True):\n"
            "        # На Station Max локальные root-утилиты лежат в каталоге панели, а не в /odm/bin.\n"
            "        if getattr(self, \"is_local\", False):\n"
            "            cmd = \"export PATH=/data/yapanel/bin:$PATH; \" + cmd\n"
        )
        if source.count(needle) != 1:
            raise RuntimeError("Overlay Station Max не нашёл однозначное место для PATH")
        source = source.replace(needle, replacement, 1)

    if HEALTH_MARKER not in source:
        public = '"/api/panel-auth/login"}'
        if source.count(public) == 1:
            source = source.replace(public, '"/api/panel-auth/login", "/api/panel/health"}', 1)
        route = '            "/api/panel/version": lambda: self.updater.check(q.get("refresh") == "1"),\n'
        if source.count(route) != 1:
            raise RuntimeError("Overlay Station Max не нашёл route для health")
        source = source.replace(
            route,
            route + '            "/api/panel/health": lambda: {"ready": bool(getattr(Handler, "ready", False)), "version": VERSION, "critical_errors": len(getattr(Handler, "critical_errors", ()))},\n',
            1,
        )
        if '"/api/panel/update/status"' not in source:
            source = source.replace(
                '            "/api/panel/health": lambda: {"ready": bool(getattr(Handler, "ready", False)), "version": VERSION, "critical_errors": len(getattr(Handler, "critical_errors", ()))},\n',
                '            "/api/panel/health": lambda: {"ready": bool(getattr(Handler, "ready", False)), "version": VERSION, "critical_errors": len(getattr(Handler, "critical_errors", ()))},\n'
                '            "/api/panel/update/status": lambda: self.updater.status(),\n',
                1,
            )
        ready_anchor = "    args.port = srv.server_address[1]\n"
        if source.count(ready_anchor) != 1:
            raise RuntimeError("Overlay Station Max не нашёл момент готовности HTTP-сервера")
        source = source.replace(ready_anchor, ready_anchor + "    # station-max-health-overlay-v1\n    Handler.ready = True\n", 1)

    server_path.write_text(source, encoding="utf-8")
    return source


def _inject_static(static_path):
    if not static_path.is_file():
        return {"applied": False, "reason": "static-not-present"}
    source = static_path.read_text(encoding="utf-8")
    if STATIC_MARKER in source or ("/api/panel/update/status" in source and "activated_pending_health" in source):
        return {"applied": False, "reason": "already-present"}
    start = source.find("if ($('#upd-go'))")
    end = source.find("\nloadPanelVersion().catch", start)
    if start < 0 or end < 0 or source.find("if ($('#upd-go'))", start + 1) >= 0:
        raise RuntimeError("Overlay Station Max не нашёл однозначный update UI")
    static_path.write_text(source[:start] + STATIC_UPDATE_BLOCK + source[end:], encoding="utf-8")
    return {"applied": True}


def _inject_max_profile(profile_path, version):
    """Применяем только проверенный overlay к upstream max_profile.py."""
    if not profile_path.is_file():
        return {"applied": False, "reason": "max-profile-not-present"}
    version = str(version)
    source = profile_path.read_text(encoding="utf-8")
    actual = _sha256(profile_path)
    target = MAX_PROFILE_OVERLAY_SHA256.get(version)
    if actual in MAX_PROFILE_OVERLAY_SHA256.values():
        return {"applied": False, "reason": "already-present", "sha256": actual}
    if MAX_PROFILE_ANCHOR not in source:
        return {"applied": False, "reason": "max-profile-overlay-not-applicable"}
    expected = MAX_PROFILE_UPSTREAM_SHA256.get(version)
    if expected is None or actual != expected:
        raise RuntimeError(
            f"Overlay Station Max не знает совместимый max_profile.py для {version}: {actual}"
        )
    start = source.find(MAX_PROFILE_ANCHOR)
    end = source.find("    def max_drop_hook_if_unused", start)
    if start < 0 or end < 0 or source.count(MAX_PROFILE_ANCHOR) != 1:
        raise RuntimeError("Overlay Station Max не нашёл однозначный блок max_profile.py")
    source = source[:start] + MAX_PROFILE_BLOCK + source[end:]
    binding = "    P.hook_status = max_hook_status\n"
    if source.count(binding) != 1:
        raise RuntimeError("Overlay Station Max не нашёл binding hook_status")
    source = source.replace(
        binding,
        binding + "    P.max_assistant_hook_fallback = max_assistant_hook_fallback\n",
        1,
    )
    with profile_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(source)
    actual = _sha256(profile_path)
    if actual != target:
        raise RuntimeError(
            f"Overlay max_profile.py дал неожиданный SHA-256: {actual}, ожидался {target}"
        )
    return {"applied": True, "sha256": actual, "version": version}


def apply(content_root, version):
    root = Path(content_root)
    server = root / "server.py"
    if not server.is_file():
        raise RuntimeError("Overlay: в staging отсутствует server.py")
    initial_source = server.read_text(encoding="utf-8")
    server_already_patched = SERVER_MARKER in initial_source and HEALTH_MARKER in initial_source and PATH_MARKER in initial_source
    static_path = root / "static" / "app.js"
    static_already_patched = (not static_path.is_file()
                              or STATIC_MARKER in static_path.read_text(encoding="utf-8")
                              or ("/api/panel/update/status" in static_path.read_text(encoding="utf-8")
                                  and "activated_pending_health" in static_path.read_text(encoding="utf-8")))
    max_profile = _inject_max_profile(root / "max_profile.py", version)
    source = _inject_server(initial_source, version, server)
    if server_already_patched and static_already_patched and not max_profile["applied"]:
        return {"id": OVERLAY_ID, "version": str(version), "applied": False,
                "max_profile": max_profile, "static": {"reason": "already-present"}}
    static = _inject_static(static_path)
    return {"id": OVERLAY_ID, "version": str(version), "applied": True,
            "max_profile": max_profile, "static": static}
