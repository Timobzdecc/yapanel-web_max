"""HTTP-маршруты yam 2 (пакетный менеджер)."""
from .router import Router


def register(router: Router):
    router.get("/api/yam2/status", lambda r: r.panel.yam2_status(r.q("refresh") == "1"))
    router.get("/api/yam2/packages", lambda r: r.panel.yam2_packages())
    router.get("/api/yam2/catalog", lambda r: r.panel.yam2_catalog(r.q("refresh") == "1"))
    router.get("/api/yam2/log", lambda r: r.panel.yam2_service_log(r.q("id", "")))

    router.post("/api/yam2/install", lambda r: r.panel.yam2_install())
    router.post("/api/yam2/update", lambda r: r.panel.yam2_update())
    router.post("/api/yam2/uninstall", lambda r: r.panel.yam2_uninstall())
    router.post("/api/yam2/registry", lambda r: r.panel.yam2_registry_add())
    router.post("/api/yam2/doctor", lambda r: r.panel.yam2_doctor(bool(r.data.get("repair"))))
    router.post("/api/yam2/pkg", lambda r: r.panel.yam2_pkg(
        r.data.get("action"), r.data["name"], bool(r.data.get("yes"))))
    router.post("/api/yam2/service", lambda r: r.panel.yam2_service(
        r.data["id"], r.data["action"], bool(r.data.get("stop_legacy"))))
