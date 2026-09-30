from __future__ import annotations

from io import BytesIO
from pathlib import Path
import tarfile
import zipfile

import pytest

from deeptutor.tools.tex_downloader import TexDownloader


@pytest.mark.parametrize("archive_type", ["tar", "zip"])
def test_archive_extraction_rejects_path_traversal(tmp_path: Path, archive_type: str) -> None:
    workspace = tmp_path / "workspace"
    extract_dir = workspace / "extract"
    extract_dir.mkdir(parents=True)
    archive = tmp_path / f"payload.{archive_type}"
    unsafe_target = workspace / "extract_evil" / "escaped.txt"

    if archive_type == "tar":
        with tarfile.open(archive, "w") as source:
            member = tarfile.TarInfo("../extract_evil/escaped.txt")
            payload = b"must not escape"
            member.size = len(payload)
            source.addfile(member, BytesIO(payload))
    else:
        with zipfile.ZipFile(archive, "w") as source:
            source.writestr("../extract_evil/escaped.txt", "must not escape")

    downloader = TexDownloader(str(workspace))
    if archive_type == "tar":
        downloader._extract_tar(archive, extract_dir)
    else:
        downloader._extract_zip(archive, extract_dir)

    assert not unsafe_target.exists()
    assert not (extract_dir / "extract_evil" / "escaped.txt").exists()
