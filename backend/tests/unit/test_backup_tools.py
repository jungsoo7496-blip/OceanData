"""08번 백업 도구의 버전 호환성: 서버와 다른 주버전은 덤프·복원 전에 거부한다."""

from __future__ import annotations

import subprocess

import pytest

from app.services.backup import _check_pg_version


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
@pytest.mark.parametrize("version", ["16.10", "16.11 (Debian 16.11-1.pgdg13+1)"])
def test_matching_pg_major(monkeypatch, tool: str, version: str) -> None:
    def run(args, **kwargs):
        assert args == [f"/tools/{tool}", "--version"]
        assert kwargs["timeout"] == 10 and kwargs["check"] is True
        return subprocess.CompletedProcess(args, 0, stdout=f"{tool} (PostgreSQL) {version}\n")

    monkeypatch.setattr(subprocess, "run", run)
    _check_pg_version(f"/tools/{tool}", 16, tool)


@pytest.mark.parametrize("tool", ["pg_dump", "pg_restore"])
@pytest.mark.parametrize("version", ["15.14", "18.0", "19beta1"])
def test_mismatched_pg_major(monkeypatch, tool: str, version: str) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, stdout=f"{tool} (PostgreSQL) {version}\n"))
    with pytest.raises(ValueError, match="DB 서버 주버전 16과 다릅니다"):
        _check_pg_version(tool, 16, tool)


def test_unrecognized_tool_version(monkeypatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(
        a[0], 0, stdout="다른 도구\n"))
    with pytest.raises(ValueError, match="주버전을 확인하지 못했습니다"):
        _check_pg_version("pg_dump", 16, "pg_dump")


def test_missing_tool_is_not_accepted(monkeypatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("도구가 없습니다")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(FileNotFoundError):
        _check_pg_version("pg_dump", 16, "pg_dump")
