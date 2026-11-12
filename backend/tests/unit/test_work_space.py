"""05번 작업 13: 처리 작업 임시 디스크 여유 확인과 비정상 종료 잔여 임시 파일 정리."""

from __future__ import annotations

import os
import time
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import pipeline
from tests.conftest import test_settings as make_settings

Usage = namedtuple("Usage", "total used free")
GB = 1024**3


def _c(tmp: Path, **kw):  # type: ignore[no-untyped-def]
    return SimpleNamespace(settings=make_settings(work_dir=str(tmp), **kw))


def test_work_space_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _c(tmp_path, work_min_free_bytes=2 * GB, work_size_factor=2.5)
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: Usage(100 * GB, 90 * GB, 10 * GB))
    pipeline.ensure_work_space(c, 3 * GB)  # 3 × 2.5 + 2 = 9.5GB ≤ 10GB
    with pytest.raises(pipeline.WorkSpaceShortage):
        pipeline.ensure_work_space(c, 4 * GB)  # 12GB 필요


def test_local_copy_refuses_before_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    storage = SimpleNamespace(iter_object=lambda b, k: calls.append(k) or iter([b"x"]))
    c = SimpleNamespace(settings=make_settings(work_dir=str(tmp_path)), storage=storage)
    monkeypatch.setattr(pipeline.shutil, "disk_usage", lambda _p: Usage(10 * GB, 10 * GB - 1, 1))
    asset = SimpleNamespace(storage_zone="RAW", variant="ORIGINAL", storage_key="k", byte_size=10 * GB,
                            mime_type="video/mp4")
    with pytest.raises(pipeline.WorkSpaceShortage), pipeline.local_copy(c, asset):  # type: ignore[arg-type]
        pass
    assert calls == [] and list(tmp_path.iterdir()) == []  # 내려받지 않고, 임시 파일도 남기지 않는다


def test_sweep_stale_work(tmp_path: Path) -> None:
    c = _c(tmp_path, work_stale_after_s=3600)
    old_file, old_dir, fresh, other = (tmp_path / "marine-a.mp4", tmp_path / "marine-proxy-x", tmp_path / "marine-b.mp4",
                                       tmp_path / "keep-me.txt")
    for p in (old_file, fresh, other):
        p.write_bytes(b"x")
    old_dir.mkdir()
    (old_dir / "out.mp4").write_bytes(b"y")
    past = time.time() - 7200
    for p in (old_file, old_dir, other):
        os.utime(p, (past, past))
    assert pipeline.sweep_stale_work(c) == 2  # type: ignore[arg-type]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep-me.txt", "marine-b.mp4"]
