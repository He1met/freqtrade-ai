from pathlib import Path

import pytest

from app.adapters.okx_demo.write_semantics import OkxDemoWriteBlocked
from app.adapters.okx_demo.writer_lock import OkxDemoWriterProcessLock


def test_process_lock_blocks_contender_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "okx-demo-writer.lock"
    first = OkxDemoWriterProcessLock(path)
    contender = OkxDemoWriterProcessLock(path)

    first.acquire()
    assert path.read_text().strip().isdigit()
    with pytest.raises(OkxDemoWriteBlocked, match="another"):
        contender.acquire()

    first.release()
    contender.acquire()
    contender.release()


def test_process_lock_requires_absolute_path() -> None:
    with pytest.raises(OkxDemoWriteBlocked, match="absolute"):
        OkxDemoWriterProcessLock(Path("relative.lock"))


def test_process_lock_context_releases_after_exception(tmp_path: Path) -> None:
    path = tmp_path / "okx-demo-writer.lock"

    with pytest.raises(RuntimeError):
        with OkxDemoWriterProcessLock(path):
            raise RuntimeError("test")

    with OkxDemoWriterProcessLock(path):
        pass


def test_process_lock_refuses_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("preserve")
    link = tmp_path / "okx-demo-writer.lock"
    link.symlink_to(target)

    with pytest.raises(OkxDemoWriteBlocked, match="safe local file"):
        OkxDemoWriterProcessLock(link).acquire()

    assert target.read_text() == "preserve"
