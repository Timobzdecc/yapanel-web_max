"""Изолированная fixture-станция для полного apply_station_file_patch.ps1."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


ROOT = Path(os.environ["YA_MAINTENANCE_FIXTURE"]).resolve()
REPO = Path(os.environ["YA_REPO_ROOT"]).resolve()
DATA = ROOT / "data"
PANEL = DATA / "yapanel"
STATE = DATA / "yapanel-state"
UPDATER = STATE / "updater"
SCENARIO = os.environ.get("YA_MAINTENANCE_SCENARIO", "success")
STATE_PATH = ROOT / "emulator-state.json"
COMPONENTS = (
    "update_agent.py",
    "station_overlay.py",
    "updater_bootstrap.py",
    "panel_update_integration.py",
    "max_profile.py.patch",
)


def _state():
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _save_state(value):
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def _set_metadata(path, value):
    metadata_path = ROOT / "fixture-metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    metadata[str(path)] = value
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _map(remote):
    remote = str(remote).replace("\\", "/")
    if remote == "/data":
        return DATA
    if remote.startswith("/data/"):
        return DATA / remote[6:]
    if remote == "/odm":
        return ROOT / "odm"
    if remote.startswith("/odm/"):
        return ROOT / remote[5:]
    if remote == "/system":
        return ROOT / "system"
    if remote.startswith("/system/"):
        return ROOT / remote[8:]
    raise RuntimeError(f"Недопустимый путь fixture: {remote}")


def _active_transaction():
    paths = list((UPDATER / "transactions").glob("*/transaction.json"))
    journals = []
    for path in paths:
        try:
            journals.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return max(journals, key=lambda item: str(item.get("id", "")), default=None)


def _facts():
    status = json.loads((UPDATER / "status.json").read_text(encoding="utf-8"))
    state = _state()
    pending = []
    for path in (UPDATER / "transactions").glob("*/transaction.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if value.get("state") in {"backup", "activating", "activated_pending_health", "rollback_started", "rollback_pending_health"}:
            pending.append(value["state"])
    start = (PANEL / "start.sh").read_text(encoding="utf-8")
    result = "\n".join((
        f"VERSION={ (PANEL / 'VERSION').read_text(encoding='utf-8').strip() }",
        f"START_SHA256={_sha(PANEL / 'start.sh')}",
        f"PATCH_SHA256={_sha(PANEL / 'max_profile.py')}",
        "START_BOOTSTRAP=yes" if "updater_bootstrap.py" in start else "START_BOOTSTRAP=no",
        "YAPANEL=running" if state["service"] == "running" else "YAPANEL=stopped",
        "ROOTD=running",
        "ADBGUARD=running",
        "ABSSHD=running",
        f"ROOTD_SHA256={_sha(ROOT / 'odm' / 'bin' / 'rootd')}",
        f"MOUNTS_SHA256={hashlib.sha256(b'/etc/hosts-ota /system/etc/hosts\\n/etc/hosts-full /system/etc/hosts\\n/odm ro\\n').hexdigest()}",
        "START_PARAMS=yes" if all(item in start for item in ("--device local", "--host 0.0.0.0", "--port 8765", "PANEL_ALLOW_NETWORKS=192.168.31.0/24")) else "START_PARAMS=no",
        f"STATE={status.get('state', 'unknown')}",
        f"TRANSACTION={status.get('transaction', '')}",
        f"PENDING={' '.join(pending)}",
        f"PANEL_COUNT={'1' if state['service'] == 'running' else '0'}",
        f"PANEL_PID={state.get('panel_pid', '')}",
    )) + "\n"
    return result


def _tar_backup():
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        archive.add(PANEL, arcname="yapanel", recursive=True)
        archive.add(STATE, arcname="yapanel-state", recursive=True)
    sys.stdout.buffer.write(stream.getvalue())
    sys.stdout.buffer.flush()


def _decode_command(value):
    if "printf %s " in value and "/system/bin/base64 -d" in value:
        encoded = value.split("printf %s ", 1)[1].split(" |", 1)[0]
        return base64.b64decode(encoded).decode("utf-8")
    return value


def ssh_main(argv):
    command = _decode_command(argv[-1] if argv else "")
    if "printf 'VERSION='" in command and "PANEL_COUNT=" in command:
        print(_facts(), end="")
        return 0
    if "tar -C /data -czf - yapanel yapanel-state" in command:
        _tar_backup()
        return 0
    if command.startswith("cat /data/yapanel-state/updater/transactions/"):
        path = command.split()[1]
        sys.stdout.buffer.write(_map(path).read_bytes())
        return 0
    if "printf 'version='" in command:
        print(f"version={(PANEL / 'VERSION').read_text(encoding='utf-8').strip()}")
        print(f"start_sha256={_sha(PANEL / 'start.sh')}")
        return 0
    return 0


def _stage_validation(command):
    stage_match = re.search(r"stage='([^']+)'", command)
    if stage_match:
        stage = _map(stage_match.group(1))
    else:
        path_match = re.search(r"test -f '(/data/yapanel-state/updater/\.maintenance-staging/[^']+)", command)
        if not path_match:
            return 0
        stage = _map(path_match.group(1)).parent
    if SCENARIO in {"corrupt_payload", "hash_failure"}:
        return 43
    for name in COMPONENTS:
        path = stage / name
        if not path.is_file() or _sha(path) != _sha(REPO / ("max_profile.py" if name == "max_profile.py.patch" else name)):
            return 44
    return 0


def _publish(command):
    stage = _map(re.search(r"stage='([^']+)'", command).group(1))
    backup = _map(re.search(r"backup='([^']+)'", command).group(1))
    backup.mkdir(parents=True, exist_ok=True)
    original = {}
    for name in COMPONENTS:
        target = UPDATER / name
        if target.exists():
            original[name] = target.read_bytes()
            (backup / f"{name}.before").write_bytes(original[name])
        else:
            original[name] = None
            (backup / f"{name}.missing").write_text("", encoding="utf-8")
    failure_at = {"publish_failure_second": 2, "publish_failure_third": 3}.get(SCENARIO)
    try:
        for index, name in enumerate(COMPONENTS, start=1):
            if failure_at == index:
                raise OSError(f"fixture publication failure {index}")
            target = UPDATER / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(stage / name), str(target))
            os.chmod(target, 0o600)
            _set_metadata(f"/data/yapanel-state/updater/{name}", "shell:shell:600")
        (backup / "publish.complete").write_text("complete\n", encoding="utf-8")
        shutil.rmtree(stage, ignore_errors=True)
        return 0
    except OSError:
        for name, data in original.items():
            target = UPDATER / name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                target.write_bytes(data)
                os.chmod(target, 0o600)
        shutil.rmtree(stage, ignore_errors=True)
        return 45


def _run_cli(command):
    args = shlex.split(command)
    script_index = next(index for index, value in enumerate(args) if value.endswith(".py"))
    script_name = Path(args[script_index]).name
    translated = []
    for value in args[script_index + 1:]:
        if value == "/data/yapanel":
            translated.append(str(PANEL))
        elif value == "/data/yapanel-state":
            translated.append(str(STATE))
        elif value.startswith("/data/"):
            translated.append(str(_map(value)))
        else:
            translated.append(value)
    script = UPDATER / script_name
    result = subprocess.run(
        [sys.executable, str(script), *translated],
        cwd=str(REPO), capture_output=True, text=True, check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def _rootd_command(remote):
    request = _map(remote)
    command = request.read_text(encoding="utf-8").strip()
    request_id = request.name.removeprefix("rootcmd-")
    if "kill -TERM" in command:
        state = _state()
        state["panel_pid"] = str(state["next_pid"])
        state["next_pid"] += 1
        _save_state(state)
        active = _active_transaction()
        if active:
            active["state"] = "healthy"
            active["healthy_at"] = 1
            path = UPDATER / "transactions" / active["id"] / "transaction.json"
            path.write_text(json.dumps(active, indent=2), encoding="utf-8")
    response = _map(f"/data/local/tmp/rootres.{request_id}")
    output = _map("/data/local/tmp/rootout")
    response.write_text(f"status=executed\nresult=exited\nid={request_id}\nexit=0\n", encoding="utf-8")
    output.write_text(f"--- rootd request={request_id} begin ---\n--- rootd request={request_id} exited exit=0 ---\n", encoding="utf-8")


def adb_main(argv):
    args = list(argv)
    if args and args[0] == "connect":
        return 0
    if "push" in args:
        index = args.index("push")
        source = Path(args[index + 1])
        remote = args[index + 2]
        target = _map(remote)
        target.parent.mkdir(parents=True, exist_ok=True)
        if SCENARIO == "incomplete_transfer" and source.name == "updater_bootstrap.py":
            return 46
        data = source.read_bytes()
        if SCENARIO == "corrupt_payload" and source.name == "max_profile.py":
            data += b"\ncorrupted fixture payload\n"
        target.write_bytes(data)
        return 0
    if "shell" not in args:
        return 0
    command = " ".join(args[args.index("shell") + 1:])
    if command == "id":
        print("uid=2000(shell) gid=2000(shell) groups=2000(shell)")
        return 0
    command = _decode_command(command)
    if "publish.complete" in command and "backup=" in command:
        return _publish(command)
    if "sha256sum" in command and "/.maintenance-staging/" in command:
        return _stage_validation(command)
    if command.startswith("mkdir -p "):
        match = re.search(r"mkdir -p '([^']+)'", command)
        if match:
            _map(match.group(1)).mkdir(parents=True, exist_ok=True)
        return 0
    if command.startswith("rm -rf "):
        match = re.search(r"rm -rf '([^']+)'", command)
        if match:
            shutil.rmtree(_map(match.group(1)), ignore_errors=True)
        return 0
    if "mv -f" in command and "/data/local/tmp/" in command:
        values = re.findall(r"/data/local/tmp/[^\s']+", command)
        if len(values) >= 2:
            source, target = values[-2], values[-1]
            shutil.move(str(_map(source)), str(_map(target)))
            _rootd_command(target)
        return 0
    if command.startswith("chmod "):
        match = re.search(r"chmod 600 '([^']+)'", command)
        if match:
            _set_metadata(match.group(1), "shell:shell:600")
        return 0
    if command.startswith("/data/python/bin/python3 "):
        return _run_cli(command)
    if command.startswith("[ -f "):
        match = re.search(r"\[ -f ['\"]?([^'\" ]+)['\"]? \]", command)
        return 0 if match and _map(match.group(1)).is_file() else 1
    if command.startswith("cat "):
        path = command.split()[1].strip("'")
        sys.stdout.buffer.write(_map(path).read_bytes())
        return 0
    if command.startswith("rm -f "):
        for path in re.findall(r"/data/local/tmp/[^\s']+", command):
            _map(path).unlink(missing_ok=True)
        return 0
    return 0


if __name__ == "__main__":
    mode = sys.argv[1]
    raise SystemExit(ssh_main(sys.argv[2:]) if mode == "ssh" else adb_main(sys.argv[2:]))
