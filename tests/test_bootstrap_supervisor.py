import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

import updater_bootstrap
from update_agent import UpdateAgent


def _seed_panel(path):
    files = {
        "server.py": "import config\n",
        "config.py": "VALUE = 1\n",
        "profiles.py": "PROFILES = {}\n",
        "max_profile.py": "VALUE = 1\n",
        "max_privacy.py": "VALUE = 1\n",
        "assistant_max.py": "VALUE = 1\n",
        "assistant_socks.py": "VALUE = 1\n",
        "VERSION": "0.3.5\n",
    }
    for name, content in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _make_archive():
    files = {
        "server.py": "import config\n",
        "config.py": "VALUE = 2\n",
        "profiles.py": "PROFILES = {}\n",
        "max_profile.py": "VALUE = 2\n",
        "max_privacy.py": "VALUE = 2\n",
        "assistant_max.py": "VALUE = 2\n",
        "assistant_socks.py": "VALUE = 2\n",
        "VERSION": "0.3.11\n",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr("release/" + name, content)
    return output.getvalue()


def _write_pending_journal(state, transaction="tx-1", version="0.3.11"):
    transaction_dir = state / "updater" / "transactions" / transaction
    transaction_dir.mkdir(parents=True, exist_ok=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": transaction,
        "state": "activated_pending_health",
        "version": version,
    }), encoding="utf-8")


class Child:
    def __init__(self, returncode=None, wait_error=None):
        self.returncode = returncode
        self.wait_error = wait_error
        self.terminated = False
        self.force_called = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self.wait_error:
            raise self.wait_error
        return self.returncode

    def kill(self):
        self.force_called = True
        raise AssertionError("Принудительное завершение не должно вызываться")


def test_graceful_stop_does_not_use_forceful_termination():
    child = Child(returncode=None, wait_error=subprocess.TimeoutExpired("server", 1))

    with pytest.raises(updater_bootstrap.BootstrapFailure, match="graceful stop"):
        updater_bootstrap._stop_gracefully(child, timeout=1)

    assert child.terminated is True
    assert child.force_called is False


def test_prepared_wrapper_is_restored_after_restart_before_activation(tmp_path, monkeypatch):
    root = tmp_path / "panel"
    state = tmp_path / "state"
    updater = state / "updater"
    root.mkdir()
    updater.mkdir(parents=True)
    original = b"#!/system/bin/sh\nexec legacy\n"
    patched = b"#!/system/bin/sh\nexec bootstrap\n"
    source = updater / "start.sh.before-migration"
    source.write_bytes(original)
    (updater / "start.sh.before-migration.json").write_text(json.dumps({
        "original_sha256": updater_bootstrap.hashlib.sha256(original).hexdigest(),
        "patched_sha256": updater_bootstrap.hashlib.sha256(patched).hexdigest(),
    }), encoding="utf-8")
    (root / "start.sh").write_bytes(original)
    updater_bootstrap._prepare_wrapper_intent(
        root,
        state,
        updater_bootstrap.hashlib.sha256(original).hexdigest(),
        updater_bootstrap.hashlib.sha256(patched).hexdigest(),
        "0.3.11",
    )
    (root / "start.sh").write_bytes(patched)

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = Path(state_dir) / "updater"

        def recover(self):
            return []

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child(returncode=0))

    assert updater_bootstrap.run(root, state, "python", []) == 0
    assert (root / "start.sh").read_bytes() == original
    assert not (updater / "wrapper-intent.json").exists()
    status = json.loads((updater / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "preparation_aborted"


def test_power_loss_after_apply_recovers_real_files_and_journal(tmp_path, monkeypatch):
    root = tmp_path / "panel"
    state = tmp_path / "state"
    root.mkdir()
    _seed_panel(root)
    agent = UpdateAgent(root, state)
    archive = _make_archive()
    transaction, manifest = agent.stage(archive, apply_overlays=False)
    baseline = {
        name: agent.sha256(root / name)
        for name in manifest["managed"]
        if (root / name).is_file()
    }
    journal = agent.activate(transaction, baseline=baseline)
    assert journal["state"] == "activated_pending_health"

    updater = state / "updater"
    original = b"#!/system/bin/sh\nexec legacy\n"
    patched = b"#!/system/bin/sh\nexec bootstrap\n"
    (updater / "start.sh.before-migration").write_bytes(original)
    (updater / "start.sh.before-migration.json").write_text(json.dumps({
        "original_sha256": updater_bootstrap.hashlib.sha256(original).hexdigest(),
        "patched_sha256": updater_bootstrap.hashlib.sha256(patched).hexdigest(),
    }), encoding="utf-8")
    (root / "start.sh").write_bytes(patched)

    children = [Child(returncode=None), Child(returncode=0)]
    started = []

    def start_child(*args):
        child = children.pop(0)
        started.append(child)
        return child

    monkeypatch.setattr(updater_bootstrap, "_start", start_child)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: False)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", lambda *args: True)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert agent.read_journal(transaction)["state"] == "rolled_back"
    assert (root / "VERSION").read_text(encoding="utf-8").strip() == "0.3.5"
    assert (root / "start.sh").read_bytes() == patched
    status = json.loads((updater / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "rolled_back"
    assert started[0].terminated is True


def test_crash_counter_resets_after_stable_window(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    clock = iter((100.0, 101.0, 200.0))
    monkeypatch.setattr(updater_bootstrap.time, "time", lambda: next(clock))

    assert updater_bootstrap._record_child_exit(status, 1) == 1
    assert updater_bootstrap._record_child_exit(status, 1) == 2
    assert updater_bootstrap._record_child_exit(status, 1) == 1
    assert json.loads(status.read_text(encoding="utf-8"))["child_crash_count"] == 1


def test_crash_loop_is_held_without_restarting_forever(tmp_path, monkeypatch):
    status = tmp_path / "status.json"
    held = []
    monkeypatch.setattr(
        updater_bootstrap,
        "_hold_on_failure",
        lambda path, error: held.append((Path(path), error)),
    )

    with pytest.raises(updater_bootstrap.BootstrapFailure, match="3 раза подряд"):
        updater_bootstrap._hold_after_crash_loop(status, 17)

    saved = json.loads(status.read_text(encoding="utf-8"))
    assert saved["state"] == "rollback_failed"
    assert "3 раза подряд" in saved["error"]
    assert len(held) == 1


def test_health_failure_does_not_rollback_while_child_is_stuck(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")
    _write_pending_journal(state)

    child = Child(returncode=None, wait_error=subprocess.TimeoutExpired("server", 1))
    rollback_calls = []

    class FakeAgent:
        updater_dir = state / "updater"

        def __init__(self, root, state_dir, python_executable):
            pass

        def recover(self):
            return []

        def rollback(self, transaction):
            rollback_calls.append(transaction)

    class Held(Exception):
        pass

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: child)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: False)
    monkeypatch.setattr(
        updater_bootstrap,
        "_hold_on_failure",
        lambda path, error: (_ for _ in ()).throw(Held(error)),
    )

    with pytest.raises(Held, match="безопасно остановить"):
        updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0)

    assert rollback_calls == []
    assert child.force_called is False
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["state"] == "rollback_failed"


def test_legacy_health_requires_status_and_exact_panel_version(monkeypatch):
    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = json.dumps(payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.payload

    def urlopen(request, timeout=0):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/api/status"):
            return Response({"uptime": "10.5", "model": "max"})
        return Response({"version": "0.3.5"})

    monkeypatch.setattr(updater_bootstrap.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(updater_bootstrap.time, "sleep", lambda seconds: None)

    assert updater_bootstrap._wait_for_legacy_health(8765, "0.3.5", 0.01)

    def wrong_version(request, timeout=0):
        url = request.full_url if hasattr(request, "full_url") else request
        if url.endswith("/api/status"):
            return Response({"uptime": "10.5", "model": "max"})
        return Response({"version": "0.3.11"})

    monkeypatch.setattr(updater_bootstrap.urllib.request, "urlopen", wrong_version)
    assert not updater_bootstrap._wait_for_legacy_health(8765, "0.3.5", 0.01)


def test_failed_new_health_rolls_back_and_accepts_healthy_legacy_version(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")
    _write_pending_journal(state)

    children = [Child(returncode=None), Child(returncode=0)]
    legacy_versions = []

    class FakeAgent:
        updater_dir = state / "updater"

        def __init__(self, root, state_dir, python_executable):
            self.rollback_calls = []

        def recover(self):
            return []

        def rollback(self, transaction):
            self.rollback_calls.append(transaction)
            (root / "VERSION").write_text("0.3.5\n", encoding="utf-8")

    fake_agent = FakeAgent(root, state, "python")
    monkeypatch.setattr("update_agent.UpdateAgent", lambda *args, **kwargs: fake_agent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: children.pop(0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: False)

    def legacy_health(port, version, timeout):
        legacy_versions.append(version)
        return version == "0.3.5"

    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", legacy_health)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert fake_agent.rollback_calls == ["tx-1"]
    assert legacy_versions == ["0.3.5"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_pending_journal_forces_readiness_even_when_status_is_healthy(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    updater_dir = state / "updater"
    transaction_dir = updater_dir / "transactions" / "tx-1"
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-1",
        "state": "activated_pending_health",
        "version": "0.3.11",
    }), encoding="utf-8")
    status_path = updater_dir / "status.json"
    status_path.write_text(json.dumps({
        "state": "healthy",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")
    events = []

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = updater_dir

        def recover(self):
            return []

        def mark_healthy(self, transaction):
            events.append(("mark_healthy", transaction))

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child(returncode=0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: True)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert events == [("mark_healthy", "tx-1")]
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "healthy"


def test_healthy_journal_reconciles_stale_pending_status_after_restart(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    transaction_dir = state / "updater" / "transactions" / "tx-1"
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-1",
        "state": "healthy",
        "version": "0.3.11",
    }), encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = state / "updater"

        def recover(self):
            return []

        def mark_healthy(self, transaction):
            raise AssertionError("healthy journal не должен повторно фиксироваться")

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child(returncode=0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: True)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["state"] == "healthy"
    assert saved["transaction"] == "tx-1"


def test_rolled_back_journal_reconciles_stale_pending_status_after_restart(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.5\n", encoding="utf-8")
    transaction_dir = state / "updater" / "transactions" / "tx-1"
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-1",
        "state": "rolled_back",
        "version": "0.3.11",
    }), encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = state / "updater"

        def recover(self):
            return []

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: Child(returncode=0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", lambda port, version, timeout: version == "0.3.5")

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_unknown_journal_state_holds_before_starting_panel(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    transaction_dir = state / "updater" / "transactions" / "tx-unknown"
    transaction_dir.mkdir(parents=True)
    (transaction_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-unknown",
        "state": "future_state",
        "version": "0.3.11",
    }), encoding="utf-8")

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = state / "updater"

        def recover(self):
            return []

    class Held(Exception):
        pass

    started = []
    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: started.append(args))
    monkeypatch.setattr(
        updater_bootstrap,
        "_hold_on_failure",
        lambda path, error: (_ for _ in ()).throw(Held(error)),
    )

    with pytest.raises(Held, match="Неизвестное состояние"):
        updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0)
    assert started == []


def test_active_journal_has_priority_over_stale_status_transaction(tmp_path):
    updater_dir = tmp_path / "state" / "updater"
    staged_dir = updater_dir / "transactions" / "tx-staged"
    active_dir = updater_dir / "transactions" / "tx-active"
    staged_dir.mkdir(parents=True)
    active_dir.mkdir(parents=True)
    (staged_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-staged", "state": "staged", "version": "0.3.11",
    }), encoding="utf-8")
    (active_dir / "transaction.json").write_text(json.dumps({
        "id": "tx-active", "state": "activating", "version": "0.3.11",
    }), encoding="utf-8")

    class Agent:
        def __init__(self):
            self.updater_dir = updater_dir

    durable = updater_bootstrap._durable_transaction(Agent(), "tx-staged")
    assert durable["id"] == "tx-active"
    assert durable["state"] == "activating"


def test_newer_rolled_back_journal_has_priority_over_stale_healthy_status(tmp_path):
    updater_dir = tmp_path / "state" / "updater"
    old_dir = updater_dir / "transactions" / "20261112-old"
    rollback_dir = updater_dir / "transactions" / "20261113-new"
    old_dir.mkdir(parents=True)
    rollback_dir.mkdir(parents=True)
    (old_dir / "transaction.json").write_text(json.dumps({
        "id": "20261112-old", "state": "healthy", "version": "0.3.11",
    }), encoding="utf-8")
    (rollback_dir / "transaction.json").write_text(json.dumps({
        "id": "20261113-new", "state": "rolled_back", "version": "0.3.11",
    }), encoding="utf-8")

    class Agent:
        def __init__(self):
            self.updater_dir = updater_dir

    durable = updater_bootstrap._durable_transaction(Agent(), "20261112-old")
    assert durable["id"] == "20261113-new"
    assert durable["state"] == "rolled_back"


def test_rollback_pending_health_is_not_reported_as_rolled_back(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")
    _write_pending_journal(state)
    states = []

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = state / "updater"

        def recover(self):
            return []

        def rollback(self, transaction):
            (root / "VERSION").write_text("0.3.5\n", encoding="utf-8")

    class Held(Exception):
        pass

    real_save_status = updater_bootstrap._save_status

    def save_status(path, **updates):
        if updates.get("state"):
            states.append(updates["state"])
        return real_save_status(path, **updates)

    children = [Child(returncode=None), Child(returncode=None)]
    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: children.pop(0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: False)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", lambda *args: False)
    monkeypatch.setattr(updater_bootstrap, "_save_status", save_status)

    def hold(path, error):
        real_save_status(path, state="rollback_failed", error=error, checked=0)
        raise Held(error)

    monkeypatch.setattr(
        updater_bootstrap,
        "_hold_on_failure",
        hold,
    )

    with pytest.raises(Held, match="legacy health"):
        updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0)

    assert "rollback_pending_health" in states
    assert "rolled_back" not in states
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "rollback_failed"


def test_bootstrap_keeps_launch_wrapper_for_future_recovery_after_legacy_restart(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    original_start = b"#!/system/bin/sh\nexec python3 server.py\n"
    patched_start = b"#!/system/bin/sh\nexec python3 updater_bootstrap.py\n"
    (root / "start.sh").write_bytes(patched_start)
    updater_dir = state / "updater"
    updater_dir.mkdir(parents=True)
    (updater_dir / "start.sh.before-migration").write_bytes(original_start)
    (updater_dir / "start.sh.before-migration.json").write_text(json.dumps({
        "original_sha256": updater_bootstrap.hashlib.sha256(original_start).hexdigest(),
        "patched_sha256": updater_bootstrap.hashlib.sha256(patched_start).hexdigest(),
    }), encoding="utf-8")
    status_path = updater_dir / "status.json"
    status_path.write_text(json.dumps({
        "state": "activated_pending_health",
        "version": "0.3.11",
        "transaction": "tx-1",
    }), encoding="utf-8")
    _write_pending_journal(state)

    children = [Child(returncode=None), Child(returncode=0)]

    class FakeAgent:
        def __init__(self, root, state_dir, python_executable):
            self.updater_dir = updater_dir
            self.rollback_calls = []

        def recover(self):
            return []

        def rollback(self, transaction):
            self.rollback_calls.append(transaction)
            (root / "VERSION").write_text("0.3.5\n", encoding="utf-8")

    fake_agent = FakeAgent(root, state, "python")
    monkeypatch.setattr("update_agent.UpdateAgent", lambda *args, **kwargs: fake_agent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: children.pop(0))
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: False)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_legacy_health", lambda *args: True)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert fake_agent.rollback_calls == ["tx-1"]
    assert (root / "start.sh").read_bytes() == patched_start
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "rolled_back"


def test_restarted_bootstrap_holds_without_second_rollback_after_failed_supervisor(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "state": "rollback_failed",
        "transaction": "tx-1",
        "error": "supervisor disappeared",
    }), encoding="utf-8")
    rollback_calls = []

    class FakeAgent:
        updater_dir = state / "updater"

        def __init__(self, root, state_dir, python_executable):
            pass

        def recover(self):
            return []

        def rollback(self, transaction):
            rollback_calls.append(transaction)

    class Held(Exception):
        pass

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(
        updater_bootstrap,
        "_hold_on_failure",
        lambda path, error: (_ for _ in ()).throw(Held(error)),
    )

    with pytest.raises(Held, match="supervisor disappeared"):
        updater_bootstrap.run(root, state, "python", [])

    assert rollback_calls == []


def test_supervisor_restart_code_restarts_only_for_pending_transaction(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    status_path = state / "updater" / "status.json"
    status_path.parent.mkdir(parents=True)
    status_path.write_text(json.dumps({
        "state": "healthy",
        "version": "0.3.5",
    }), encoding="utf-8")

    children = [Child(returncode=updater_bootstrap.SUPERVISOR_RESTART_CODE), Child(returncode=0)]
    starts = []

    class FakeAgent:
        updater_dir = state / "updater"

        def __init__(self, root, state_dir, python_executable):
            pass

        def recover(self):
            return []

        def mark_healthy(self, transaction):
            pass

    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)

    def start_child(*args):
        starts.append(args)
        if not starts[1:]:
            _write_pending_journal(state)
            status_path.write_text(json.dumps({
                "state": "activated_pending_health",
                "version": "0.3.11",
                "transaction": "tx-1",
            }), encoding="utf-8")
        return children.pop(0)

    monkeypatch.setattr(updater_bootstrap, "_start", start_child)
    monkeypatch.setattr(updater_bootstrap, "_wait_for_health", lambda *args: True)

    assert updater_bootstrap.run(root, state, "python", ["--port", "8765"], health_timeout=0) == 0
    assert len(starts) == 2
    assert json.loads(status_path.read_text(encoding="utf-8"))["state"] == "healthy"


def test_repeated_child_failures_eventually_hold_init_supervisor(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "panel"
    root.mkdir()
    (root / "VERSION").write_text("0.3.11\n", encoding="utf-8")
    children = [Child(returncode=23), Child(returncode=23), Child(returncode=23)]

    class FakeAgent:
        updater_dir = state / "updater"

        def __init__(self, root, state_dir, python_executable):
            pass

        def recover(self):
            return []

    class Held(Exception):
        pass

    held = []
    monkeypatch.setattr("update_agent.UpdateAgent", FakeAgent)
    monkeypatch.setattr(updater_bootstrap, "_start", lambda *args: children.pop(0))

    def hold(path, error):
        held.append(error)
        raise Held(error)

    monkeypatch.setattr(updater_bootstrap, "_hold_on_failure", hold)

    assert updater_bootstrap.run(root, state, "python", []) == 23
    assert updater_bootstrap.run(root, state, "python", []) == 23
    with pytest.raises(Held, match="3 раза подряд"):
        updater_bootstrap.run(root, state, "python", [])

    assert len(held) == 1
    saved = json.loads((state / "updater" / "status.json").read_text(encoding="utf-8"))
    assert saved["state"] == "rollback_failed"
