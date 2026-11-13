"""Безопасная Windows-модель SSH/ADB границы тестовой станции.

Эмулятор не подменяет UpdateAgent или PowerShell installer. Он ограничивает
доступ к временному fixture-каталогу и воспроизводит только транспортные и
Android init операции, необходимые production installer.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(os.environ["YA_STATION_FIXTURE"]).resolve()
DATA = ROOT / "data"
PANEL = DATA / "yapanel"
STATE = DATA / "yapanel-state"
UPDATER = STATE / "updater"
ADB_STATE = ROOT / "adb-state"
STATE_FILE = ROOT / "emulator-state.json"
SCENARIO = os.environ.get("YA_STATION_SCENARIO", "success")


def _load_state():
    if not STATE_FILE.exists():
        return {
            "service": "running",
            "panel_pid": "10011",
            "next_pid": 10012,
            "handoff_count": 0,
            "bootstrap_started": False,
            "handoff_attempted": False,
            "rollback_owner": None,
            "updater_access_ready": False,
        }
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    state.setdefault("updater_access_ready", False)
    return state


def _save_state(value):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def _map_path(remote):
    remote = str(remote).replace("\\", "/")
    if remote == "/data" or remote.startswith("/data/"):
        return DATA / remote.removeprefix("/data/") if remote != "/data" else DATA
    if remote == "/odm" or remote.startswith("/odm/"):
        return ROOT / remote.removeprefix("/") if remote != "/odm" else ROOT / "odm"
    if remote == "/system" or remote.startswith("/system/"):
        return ROOT / remote.removeprefix("/") if remote != "/system" else ROOT / "system"
    if remote == "/data/local/tmp" or remote.startswith("/data/local/tmp/"):
        return DATA / "local" / "tmp" / remote.removeprefix("/data/local/tmp/")
    raise RuntimeError(f"Недопустимый fixture path: {remote}")


def _safe_write(path, data):
    path = path.resolve()
    if ROOT not in path.parents:
        raise RuntimeError(f"Выход за fixture root: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version():
    return (PANEL / "VERSION").read_text(encoding="utf-8").strip()


def _status_path():
    return UPDATER / "status.json"


def _set_status(state, **extra):
    payload = {}
    if _status_path().exists():
        try:
            payload = json.loads(_status_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
    payload.update(state=state, **extra)
    _status_path().parent.mkdir(parents=True, exist_ok=True)
    _status_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _active_transaction():
    candidates = []
    transactions = UPDATER / "transactions"
    if not transactions.exists():
        return None
    for path in transactions.glob("*/transaction.json"):
        try:
            journal = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if journal.get("state") in {"activating", "activated_pending_health", "rollback_started"}:
            candidates.append(journal)
    return max(candidates, key=lambda value: str(value.get("id", "")), default=None)


def _snapshot():
    state = _load_state()
    start = PANEL / "start.sh"
    rootd = ROOT / "odm" / "bin" / "rootd"
    mounts = b"/etc/hosts-ota /system/etc/hosts\n/etc/hosts-full /system/etc/hosts\n/odm ro\n"
    start_text = start.read_text(encoding="utf-8") if start.exists() else ""
    service = state.get("service", "running")
    pid = str(state.get("panel_pid", "")) if service == "running" else ""
    labels = {
        "VERSION": _version(),
        "START_SHA256": _sha(start),
        "START_OWNER": "2000:2000:700",
        "PANEL_PID": pid,
        "PANEL_CANDIDATE_COUNT": "1" if pid else "0",
        "ROOTD_SHA256": _sha(rootd),
        "YAPANEL": service,
        "ROOTD": "running",
        "ADBGUARD": "running",
        "ABSSHD": "running",
        "SUPERVISOR_OWNER_PRESENT": "yes" if state.get("bootstrap_started") else "no",
        "MOUNTS_SHA256": hashlib.sha256(mounts).hexdigest(),
        "START_DEVICE_LOCAL": "yes" if "--device local" in start_text else "no",
        "START_HOST": "yes" if "--host 0.0.0.0" in start_text else "no",
        "START_PORT": "yes" if "--port 8765" in start_text else "no",
        "START_NETWORKS": "yes" if "PANEL_ALLOW_NETWORKS=192.168.31.0/24" in start_text else "no",
        "START_CA": "yes" if "SSL_CERT_FILE=" in start_text else "no",
    }
    return "\n".join(f"{key}={value}" for key, value in labels.items()) + "\n"


def _make_backup():
    output = sys.stdout.buffer
    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            archive.add(PANEL, arcname="yapanel", recursive=True)
            archive.add(STATE, arcname="yapanel-state", recursive=True)
        output.write(temp_path.read_bytes())
        output.flush()
    finally:
        temp_path.unlink(missing_ok=True)


def _run_update_agent(args):
    executable = str((UPDATER / args[0]).resolve())
    translated = []
    for value in args[1:]:
        if value == "/data/yapanel":
            translated.append(str(PANEL))
        elif value == "/data/yapanel-state":
            translated.append(str(STATE))
        elif value.startswith("/data/"):
            translated.append(str(_map_path(value)))
        else:
            translated.append(value)
    result = subprocess.run(
        [sys.executable, executable, *translated],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if SCENARIO == "apply_error_after_activation" and args[0] == "update_agent.py" and "apply" in args:
        return result.stdout, "Искусственный отказ после durable activation\n", 31
    return result.stdout, result.stderr, result.returncode


def _rollback_active():
    journal = _active_transaction()
    if not journal:
        return
    script = UPDATER / "update_agent.py"
    result = subprocess.run(
        [sys.executable, str(script), "rollback", journal["id"], "--root", str(PANEL), "--state", str(STATE)],
        cwd=str(ROOT), capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        _set_status("rollback_failed", error=result.stderr or result.stdout)
        return
    wrapper_source = UPDATER / "start.sh.before-migration"
    if wrapper_source.exists():
        shutil.copy2(wrapper_source, PANEL / "start.sh")
    _set_status("rolled_back", transaction=journal["id"], version="0.3.5")


def _spawn_recovery():
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "recover"],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def _handoff(command):
    state = _load_state()
    expected = re.search(r'panel_pid="?([0-9]+)', command)
    expected_pid = expected.group(1) if expected else str(state.get("panel_pid"))
    if expected_pid != str(state.get("panel_pid")) or state.get("service") != "running":
        return "unexpected_panel_pid\n", "", 21
    state["handoff_attempted"] = True
    state["bootstrap_started"] = True
    state["handoff_count"] = int(state.get("handoff_count", 0)) + 1
    state["panel_pid"] = str(state.get("next_pid", 10012))
    state["next_pid"] = int(state["panel_pid"]) + 1
    _save_state(state)

    if SCENARIO == "supervisor_unavailable":
        _set_status("rollback_failed", error="fixture supervisor unavailable")

    if SCENARIO == "power_loss_before_handoff":
        return "power loss before handoff\n", "fixture power loss\n", 70
    return f"previous_pid={expected_pid}\ncurrent_pid={state['panel_pid']}\nstate=running\n", "", 0


def _execute_shell(command):
    command = command.strip()
    try:
        (ROOT / "commands.log").open("a", encoding="utf-8").write(command[:2000] + "\n---\n")
    except OSError:
        pass
    if command.startswith("echo VERSION="):
        return _snapshot().encode(), b"", 0
    if "chown -R shell:shell" in command and "/data/yapanel-state/updater" in command:
        if SCENARIO == "updater_permissions_failure":
            return b"", b"fixture updater permission failure", 42
        state = _load_state()
        state["updater_access_ready"] = True
        _save_state(state)
        return b"", b"", 0
    if command.startswith("test -d /data/yapanel"):
        return b"", b"", 0
    if "tar -C /data -czf - yapanel yapanel-state" in command:
        _make_backup()
        return b"", b"", 0
    if command.startswith("find /data/yapanel-state/updater/transactions"):
        paths = sorted((UPDATER / "transactions").glob("*/transaction.json")) if (UPDATER / "transactions").exists() else []
        return ("\n".join(f"/data/yapanel-state/updater/transactions/{path.parent.name}/transaction.json" for path in paths) + "\n").encode(), b"", 0
    if "panel_pids=\"\"" in command and "kill -TERM" in command:
        stdout, stderr, code = _handoff(command)
        return stdout.encode(), stderr.encode(), code
    if command.startswith("cat ") and ">" not in command:
        match = re.match(r"cat\s+([^\s]+)", command)
        if match:
            path = _map_path(match.group(1))
            return path.read_bytes(), b"", 0
    if command.startswith("cat /data/yapanel-state/updater/status.json"):
        try:
            return _status_path().read_bytes(), b"", 0
        except FileNotFoundError:
            return b"", b"", 0
    if command.startswith("/data/python/bin/python3 "):
        args = shlex.split(command)[1:]
        script_index = next((index for index, value in enumerate(args) if value.endswith(".py")), None)
        if script_index is None:
            return b"", b"python script not found", 2
        script = Path(args[script_index]).name
        command_args = [script, *args[script_index + 1:]]
        stdout, stderr, code = _run_update_agent(command_args)
        return stdout.encode(), stderr.encode(), code
    if command.startswith("set -eu") and "mv /data/yapanel-state/updater/start.sh.new" in command:
        if SCENARIO == "wrapper_install_failure":
            return b"", b"fixture wrapper installation failure", 41
        source = _map_path("/data/yapanel-state/updater/start.sh.new")
        target = _map_path("/data/yapanel/start.sh")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        return b"", b"", 0
    if "source=/data/yapanel-state/updater/start.sh.before-migration" in command:
        source = _map_path("/data/yapanel-state/updater/start.sh.before-migration")
        target = _map_path("/data/yapanel/start.sh")
        shutil.copy2(source, target)
        return b"", b"", 0
    if command.startswith("printf 'version='"):
        return f"version={_version()}\nstart_sha256={_sha(PANEL / 'start.sh')}\n".encode(), b"", 0
    if command.startswith("mkdir -p") or command in {"sync", "true"}:
        return b"", b"", 0
    if command.startswith("rm -f"):
        for remote in re.findall(r"/data/[^\s;&]+", command):
            _map_path(remote).unlink(missing_ok=True)
        return b"", b"", 0
    if command.startswith("cp -p"):
        values = shlex.split(command)
        sources = [value for value in values if value.startswith("/data/")]
        if len(sources) >= 2:
            destination = _map_path(sources[-1])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(_map_path(sources[-2]), destination)
        metadata = re.search(r"printf '%s\\n' '([^']+)' > (/data/[^\s;&]+)", command)
        if metadata:
            _safe_write(_map_path(metadata.group(2)), metadata.group(1).encode() + b"\n")
        return b"", b"", 0
    if command.startswith("chown ") or command.startswith("chmod ") or "/system/bin/sh -n" in command:
        return b"", b"", 0
    if command.startswith("mv "):
        values = shlex.split(command)
        paths = [value for value in values if value.startswith("/data/")]
        if len(paths) >= 2:
            shutil.move(str(_map_path(paths[-2])), str(_map_path(paths[-1])))
        return b"", b"", 0
    return b"", f"Неподдержанная fixture SSH-команда: {command[:120]}".encode(), 2


def ssh_main(argv):
    command = argv[-1] if argv else ""
    if "fixture" in argv:
        command = " ".join(argv[argv.index("fixture") + 1:])
    if "printf %s " in command and "/system/bin/base64 -d" in command:
        encoded = command.split("printf %s ", 1)[1].split(" |", 1)[0]
        command = base64.b64decode(encoded).decode("utf-8")
    if command.startswith("cat > "):
        remote = command.split("cat > ", 1)[1].strip().split()[0]
        _safe_write(_map_path(remote), sys.stdin.buffer.read())
        return 0
    if command.startswith("cat ") and ">" not in command:
        path = command.split("cat ", 1)[1].strip().split()[0]
        sys.stdout.buffer.write(_map_path(path).read_bytes())
        return 0
    stdout, stderr, code = _execute_shell(command)
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return code


def _rootd_output(request_id, command):
    if "id -u; id -Z; getprop init.svc.yapanel" in command:
        output = "0\ncontext=u:r:shell:s0\nrunning\n"
        code = 0
    elif "kill -TERM" in command:
        output, error, code = _handoff(command)
        if error:
            output += error
    else:
        output = ""
        code = 0
    response = (
        f"status=executed\nresult=exited\nid={request_id}\nexit={code}\n"
    ).encode()
    output_block = (
        f"--- rootd request={request_id} begin ---\n{output}"
        f"--- rootd request={request_id} exited exit={code} ---\n"
    ).encode()
    return response, output_block


def adb_main(argv):
    args = list(argv)
    if args and args[0] == "connect":
        print("connected to fixture")
        return 0
    if "shell" not in args:
        if "push" in args:
            index = args.index("push")
            source = Path(args[index + 1])
            remote = args[index + 2]
            _safe_write(_map_path(remote), source.read_bytes())
            return 0
        print("uid=2000(shell) gid=2000(shell) groups=2000(shell)")
        return 0
    command = args[args.index("shell") + 1:]
    shell = " ".join(command)
    if "printf %s " in shell and "/system/bin/base64 -d" in shell:
        encoded = shell.split("printf %s ", 1)[1].split(" |", 1)[0]
        shell = base64.b64decode(encoded).decode("utf-8")
        stdout, stderr, code = _execute_shell(shell)
        sys.stdout.buffer.write(stdout)
        sys.stderr.buffer.write(stderr)
        return code
    if shell == "id":
        print("uid=2000(shell) gid=2000(shell) groups=2000(shell)")
        return 0
    match = re.match(r"test -[rx] (.+)$", shell)
    if match:
        state = _load_state()
        return 0 if state.get("updater_access_ready") and "/data/yapanel-state/updater" in match.group(1) else 1
    match = re.search(r"\[ -f '([^']+)' \]", shell)
    if match:
        return 0 if _map_path(match.group(1)).is_file() else 1
    if shell.startswith("cat "):
        path = shell.split("cat ", 1)[1].strip().strip("'")
        sys.stdout.buffer.write(_map_path(path).read_bytes())
        return 0
    if "mv -f" in shell and "/data/local/tmp/" in shell:
        paths = re.findall(r"/data/local/tmp/[^\s']+", shell)
        if len(paths) >= 2:
            source, request = paths[-2], paths[-1]
            source_path = _map_path(source)
            request_path = _map_path(request)
            request_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(request_path))
            command_text = request_path.read_text(encoding="utf-8").strip()
            request_id = request_path.name.removeprefix("rootcmd-")
            response, output = _rootd_output(request_id, command_text)
            _safe_write(_map_path(f"/data/local/tmp/rootres.{request_id}"), response)
            _safe_write(_map_path("/data/local/tmp/rootout"), output)
        return 0
    if shell.startswith("rm -f"):
        for remote in re.findall(r"/data/local/tmp/[^\s']+", shell):
            _map_path(remote).unlink(missing_ok=True)
        return 0
    return 0


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "recover":
        _rollback_active()
        raise SystemExit(0)
    raise SystemExit(ssh_main(sys.argv[2:]) if mode == "ssh" else adb_main(sys.argv[2:]))
