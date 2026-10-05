"""The reading-material upload ceiling rejects oversize files mid-stream.

``upload_material`` streams the request to a temp file with a running size
check, so the ceiling has to be proven without pushing 200 MB through the
socket. The guard reads ``MAX_MATERIAL_BYTES`` at request time, so the test
lowers the constant, streams past it, and asserts the rejection and the temp
cleanup — the same code path a real 201 MB upload takes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import BackgroundTasks, HTTPException, UploadFile

from deeptutor.api.routers import reading


def _upload(size: int) -> UploadFile:
    return UploadFile(filename="p27-oversize.md", file=io.BytesIO(b"x" * size))


@pytest.mark.asyncio
async def test_upload_over_the_ceiling_is_rejected_and_cleaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(reading, "MAX_MATERIAL_BYTES", 1024)
    created: list[Path] = []
    real_mkdtemp = reading.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        # Keep the temp tree out of the shared /tmp namespace of the suite.
        created.append(Path(path))
        return path

    monkeypatch.setattr(reading.tempfile, "mkdtemp", recording_mkdtemp)

    with pytest.raises(HTTPException) as excinfo:
        await reading.upload_material(BackgroundTasks(), _upload(4096))

    assert excinfo.value.status_code == 413
    assert "exceeds the" in str(excinfo.value.detail)
    assert created and not created[0].exists(), "oversize upload left its temp file behind"
