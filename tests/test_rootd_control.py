from __future__ import annotations

import base64
import json

import pytest

from tools import rootd_control
from tools.rootd_control import RootdError, extract_output, parse_result


def test_parse_result_accepts_successful_rootd_response():
    result = parse_result(b"id=yp-1\nstatus=executed\nresult=exited\nexit=0\n")

    assert result["id"] == "yp-1"
    assert result["exit"] == 0


def test_parse_result_rejects_failed_rootd_response():
    with pytest.raises(RootdError, match="не подтвердил"):
        parse_result(b"id=yp-1\nstatus=error\nreason=queue unavailable\n")


def test_extract_output_returns_only_requested_block():
    raw = (
        b"old output\n"
        b"--- rootd request=yp-1 begin ---\n"
        b"0\n"
        b"u:r:adbd:s0\n"
        b"--- rootd request=yp-1 exited exit=0 ---\n"
        b"future output\n"
    )

    assert extract_output(raw, "yp-1") == "0\nu:r:adbd:s0\n"


def test_extract_output_rejects_missing_block():
    with pytest.raises(RootdError, match="отсутствует начало"):
        extract_output(b"unrelated output\n", "yp-1")


def test_main_decodes_base64_command_without_losing_shell_quotes(monkeypatch, capsys):
    command = 'case "$value" in *" server.py "*) echo ok;; esac'
    monkeypatch.setattr(rootd_control, "run", lambda actual, serial, adb, timeout: (actual, 0))

    assert rootd_control.main([
        "--serial", "station",
        "--command-base64", base64.b64encode(command.encode()).decode(),
    ]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["stdout"] == command
