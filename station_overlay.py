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
    source = _inject_server(initial_source, version, server)
    if server_already_patched and static_already_patched:
        return {"id": OVERLAY_ID, "version": str(version), "applied": False, "static": {"reason": "already-present"}}
    static = _inject_static(static_path)
    return {"id": OVERLAY_ID, "version": str(version), "applied": True, "static": static}
