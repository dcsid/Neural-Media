"""Smoke tests for services/pipeline/scripts/probe_share_url.py.

The probe script is a one-off operator tool — its job is to verify a
single share-shortlink URL through ``_yt_dlp_fetch`` and print a verdict.
These tests pin the non-network parts (URL parsing, history-file
fallback, exit-code shape) by mocking the yt-dlp seam.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "services" / "pipeline" / "scripts" / "probe_share_url.py"


@pytest.fixture
def probe_module():
    """Load probe_share_url.py as a module. It's a script, not packaged."""
    spec = importlib.util.spec_from_file_location("probe_share_url", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["probe_share_url"] = mod
    spec.loader.exec_module(mod)
    try:
        yield mod
    finally:
        sys.modules.pop("probe_share_url", None)


def test_first_share_url_from_history_extracts_link(
    probe_module, tmp_path: Path,
) -> None:
    """The history fallback finds the first ``Link:`` regardless of
    surrounding whitespace and blank lines."""
    history = tmp_path / "Watch History.txt"
    history.write_text(
        "Date: 2026-05-17 12:00:00 UTC\n"
        "Link: https://www.tiktokv.com/share/video/1111/\n"
        "\n"
        "Date: 2026-05-17 12:01:00 UTC\n"
        "Link: https://www.tiktokv.com/share/video/2222/\n",
        encoding="utf-8",
    )
    url = probe_module._first_share_url_from_history(history)
    assert url == "https://www.tiktokv.com/share/video/1111/"


def test_first_share_url_returns_none_when_history_missing(
    probe_module, tmp_path: Path,
) -> None:
    assert probe_module._first_share_url_from_history(tmp_path / "missing.txt") is None


def test_main_returns_2_when_no_url_and_no_history(
    probe_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.setattr(probe_module, "DEFAULT_HISTORY", tmp_path / "missing.txt")
    rc = probe_module.main([])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no URL" in err


def test_main_returns_0_on_successful_fetch_with_mocked_seam(
    probe_module,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Replace ``_yt_dlp_fetch`` with a stub that writes a fake mp4.

    The probe should exit 0 because the destination file exists with
    non-zero size after the (mocked) fetch returns.
    """
    seen: dict[str, Any] = {}

    def fake_fetch(url: str, dest: Path, user_agent: str) -> None:
        seen["url"] = url
        seen["user_agent"] = user_agent
        dest.write_bytes(b"FAKE_MP4_BYTES")

    # The script imports _yt_dlp_fetch lazily inside main() — patching
    # the attribute on the downloader module is enough.
    from neural_media_pipeline import downloader as dl_mod
    monkeypatch.setattr(dl_mod, "_yt_dlp_fetch", fake_fetch)

    target_url = "https://www.tiktokv.com/share/video/7640163791312801054/"
    rc = probe_module.main([target_url])
    assert rc == 0
    out = capsys.readouterr().out
    assert target_url in out
    assert "OK" in out
    assert seen["url"] == target_url
    # The script uses a desktop Chrome UA — any non-empty value is fine
    # for the smoke; what matters is that the seam was wired through.
    assert seen["user_agent"]


def test_main_returns_1_when_fetch_produces_no_file(
    probe_module,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """yt-dlp returns successfully but leaves no file → exit 1."""
    def silent_fetch(url: str, dest: Path, user_agent: str) -> None:
        # Intentionally do not write anything.
        return None

    from neural_media_pipeline import downloader as dl_mod
    monkeypatch.setattr(dl_mod, "_yt_dlp_fetch", silent_fetch)

    rc = probe_module.main(["https://www.tiktokv.com/share/video/1/"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "no file" in out
