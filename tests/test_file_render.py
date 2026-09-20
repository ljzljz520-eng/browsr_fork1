"""
Tests for the generation-based, non-blocking file preview pipeline.

A fake remote filesystem with artificial latency drives the same code path
that slow remote UPaths (S3 / SFTP / GitHub) exercise.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple

import pytest
from rich.text import Text
from textual_universal_directorytree import UPath

from browsr.base import TextualAppContext
from browsr.browsr import Browsr
from browsr.widgets.windows import (
    FileToStringResult,
    StaticWindow,
    WindowSwitcher,
)

FILES: dict[str, str] = {
    "README.md": "# Hello README\n",
    "a.txt": "alpha content",
    "b.txt": "beta content",
    "c.txt": "gamma content",
    "data.json": '{"key": "value"}',
    "broken.txt": "broken content",
    "data.csv": "name,value\nalpha,1\nbeta,2\n",
}


class RemoteDir(NamedTuple):
    """A UPath with a configurable per-file latency map."""

    path: UPath
    delays: dict[str, float]


@pytest.fixture
def remote_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteDir:
    """
    A local UPath whose preview ``prepare_file`` calls have per-file latency,
    emulating a slow remote filesystem.
    """
    for name, content in FILES.items():
        (tmp_path / name).write_text(content)
    directory = UPath(str(tmp_path))

    delays: dict[str, float] = {}
    original_prepare = WindowSwitcher.prepare_file

    def slow_prepare(
        self: WindowSwitcher, file_path: Any, scroll_home: bool = True
    ) -> Any:
        time.sleep(delays.get(file_path.name, 0.0))
        return original_prepare(self, file_path=file_path, scroll_home=scroll_home)

    monkeypatch.setattr(WindowSwitcher, "prepare_file", slow_prepare)
    return RemoteDir(path=directory, delays=delays)


def _select(code_browser: Any, path: UPath) -> Any:
    """
    Simulate a ``DirectoryTree.FileSelected`` message for ``path``.
    """
    code_browser.handle_file_selected(SimpleNamespace(path=path))
    return code_browser._render_worker


@pytest.mark.asyncio
async def test_rapid_selection_only_last_generation_commits(
    remote_dir: RemoteDir,
) -> None:
    """
    Selecting A, B, C in quick succession leaves only C on screen:
    content, path, FileInfo and subtitle all describe C, even though A/B
    finish after C.
    """
    directory, delays = remote_dir
    delays.update({"a.txt": 0.6, "b.txt": 0.4, "c.txt": 0.05})
    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        switcher = code_browser.window_switcher
        await pilot.pause()
        assert switcher.rendered_file == directory / "README.md"

        worker_a = _select(code_browser, directory / "a.txt")
        worker_b = _select(code_browser, directory / "b.txt")
        worker_c = _select(code_browser, directory / "c.txt")

        await worker_c.wait()
        # Let the cancelled A/B workers finish their uninterruptible sleeps
        # and attempt their (rejected) late commits.
        await asyncio.sleep(0.75)
        await pilot.pause()

        assert worker_a.is_cancelled
        assert worker_b.is_cancelled
        assert not worker_c.is_cancelled

        assert code_browser.selected_file_path == directory / "c.txt"
        assert switcher.rendered_file == directory / "c.txt"
        assert switcher.text_window.display is True
        assert switcher.vim_scroll.display is False
        assert switcher.datatable_window.display is False
        assert switcher.text_window.text == "gamma content"
        assert str(switcher.rendered_file) in app.sub_title
        file_info = app.code_browser_screen.file_information
        assert file_info.file_info is not None
        assert file_info.file_info.file == directory / "c.txt"


@pytest.mark.asyncio
async def test_keys_and_directory_switch_processed_during_preview(
    remote_dir: RemoteDir,
) -> None:
    """
    f/? and directory switching stay responsive while a preview reads.
    Closing an overlay mid-render moves focus onto the window that the
    in-flight generation commits.
    """
    directory, delays = remote_dir
    delays["c.txt"] = 1.0
    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        switcher = code_browser.window_switcher
        await pilot.pause()

        worker = _select(code_browser, directory / "c.txt")
        await pilot.pause()
        assert worker.is_running

        # f toggles the tree immediately instead of waiting for the read.
        show_tree_before = code_browser.show_tree
        await pilot.press("f")
        assert code_browser.show_tree is not show_tree_before

        # ? opens the shortcuts overlay while the read is still blocked.
        await pilot.press("question_mark")
        assert code_browser.shortcuts_window.display is True
        assert worker.is_running

        # Closing the overlay defers focus until the pending generation commits.
        await pilot.press("escape")
        assert code_browser.shortcuts_window.display is False
        assert app.focused is not switcher.text_window

        await worker.wait()
        await pilot.pause()

        assert switcher.rendered_file == directory / "c.txt"
        assert switcher.text_window.display is True
        assert app.focused is switcher.text_window

        # Switching directory during a slow read must be processed too.
        # Show the tree again (f hid it above) so the "." binding is active.
        await pilot.press("f")
        assert code_browser.has_class("-show-tree")
        delays["a.txt"] = 0.5
        worker_two = _select(code_browser, directory / "a.txt")
        await pilot.pause()
        assert worker_two.is_running
        await pilot.press(".")
        assert code_browser.directory_tree.path == directory.parent
        await worker_two.wait()
        await pilot.pause()
        assert switcher.rendered_file == directory / "a.txt"


@pytest.mark.asyncio
async def test_commit_while_overlay_open_restores_new_window_on_close(
    remote_dir: RemoteDir,
) -> None:
    """
    A render that commits while an overlay is open refreshes the captured
    window state: closing the overlay restores (and focuses) the freshly
    committed window, never the stale one.
    """
    directory, delays = remote_dir
    delays["c.txt"] = 0.2
    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        switcher = code_browser.window_switcher
        await pilot.pause()

        worker = _select(code_browser, directory / "c.txt")
        await pilot.pause()
        assert worker.is_running
        await pilot.press("question_mark")
        assert code_browser.shortcuts_window.display is True

        # Let the c.txt generation commit while the overlay covers content.
        await worker.wait()
        await pilot.pause()
        assert switcher.rendered_file == directory / "c.txt"
        # Content windows stay physically hidden behind the overlay.
        assert switcher.text_window.display is False
        # But the captured restore state already points at the new window.
        assert code_browser._content_display_state[switcher.text_window] is True

        await pilot.press("escape")
        assert code_browser.shortcuts_window.display is False
        assert switcher.text_window.display is True
        assert switcher.vim_scroll.display is False
        assert app.focused is switcher.text_window


@pytest.mark.asyncio
async def test_stat_ok_read_failure_is_recoverable(
    remote_dir: RemoteDir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A successful stat followed by a failed content read shows:
    - the FileInfo (status bar) for the selected path
    - a recoverable error in the content window
    A successful reload clears the error and restores the normal window.
    """
    directory, delays = remote_dir
    delays.clear()
    original_file_to_string = StaticWindow.file_to_string

    def broken_file_to_string(
        self: StaticWindow, file_path: Any, max_lines: int | None = None
    ) -> FileToStringResult:
        if file_path.name == "broken.txt":
            return FileToStringResult(
                result="",
                error_occurred=True,
                error=OSError("simulated remote read failure"),
            )
        return original_file_to_string(self, file_path=file_path, max_lines=max_lines)

    monkeypatch.setattr(StaticWindow, "file_to_string", broken_file_to_string)

    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        switcher = code_browser.window_switcher
        info_bar = app.code_browser_screen.file_information
        await pilot.pause()

        worker = _select(code_browser, directory / "broken.txt")
        await worker.wait()
        await pilot.pause()

        # Status bar stays consistent with the selected path.
        assert info_bar.file_info is not None
        assert info_bar.file_info.file == directory / "broken.txt"
        assert info_bar.display is True
        assert switcher.rendered_file == directory / "broken.txt"

        # Error renders in the static window and mentions recovery.
        assert switcher.vim_scroll.display is True
        assert switcher.text_window.display is False
        error_renderable = switcher.static_window.content
        assert isinstance(error_renderable, Text)
        assert "UNABLE TO READ FILE" in error_renderable.plain
        assert "simulated remote read failure" in error_renderable.plain
        assert "Press r to reload" in error_renderable.plain
        assert str(directory / "broken.txt") in app.sub_title

        # Make the read succeed again and reload.
        monkeypatch.undo()
        await pilot.press("r")
        reload_worker = code_browser._render_worker
        assert reload_worker is not None
        await reload_worker.wait()
        await pilot.pause()

        assert switcher.text_window.display is True
        assert switcher.vim_scroll.display is False
        assert switcher.text_window.text == "broken content"
        assert switcher.rendered_file == directory / "broken.txt"
        assert info_bar.file_info.file == directory / "broken.txt"


@pytest.mark.asyncio
async def test_startup_readme_preview_and_json_routing(remote_dir: RemoteDir) -> None:
    """
    The startup README auto-preview and JSON routing both go through the
    generation pipeline and never flash stale file content.
    """
    directory, delays = remote_dir
    delays.clear()
    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        switcher = code_browser.window_switcher
        info_bar = app.code_browser_screen.file_information
        await pilot.pause()

        # Startup README preview committed through a generation.
        assert code_browser._committed_generation == code_browser._render_generation
        assert switcher.rendered_file == directory / "README.md"
        assert switcher.vim_scroll.display is True
        assert str(directory / "README.md") in app.sub_title
        assert info_bar.file_info is not None
        assert info_bar.file_info.file == directory / "README.md"

        # JSON routes to the text window with json highlighting.
        worker = _select(code_browser, directory / "data.json")
        await worker.wait()
        await pilot.pause()
        assert switcher.text_window.display is True
        assert switcher.vim_scroll.display is False
        assert switcher.text_window.language == "json"
        assert switcher.text_window.text == '{\n  "key": "value"\n}'
        assert switcher.rendered_file == directory / "data.json"
        assert info_bar.file_info.file == directory / "data.json"

        # Tables route to the datatable window through a generation commit.
        worker = _select(code_browser, directory / "data.csv")
        await worker.wait()
        await pilot.pause()
        assert switcher.datatable_window.display is True
        assert switcher.vim_scroll.display is False
        assert switcher.text_window.display is False
        assert switcher.rendered_file == directory / "data.csv"
        assert str(directory / "data.csv") in app.sub_title
        table = switcher.datatable_window
        assert [str(column.label) for column in table.ordered_columns] == [
            "",
            "name",
            "value",
        ]
        assert table.get_cell_at((0, 1)) == "alpha"
        assert table.get_cell_at((0, 2)) == "1"


@pytest.mark.asyncio
async def test_quitting_during_preview_never_writes_to_dead_widgets(
    remote_dir: RemoteDir,
) -> None:
    """
    Quitting while a preview is in flight cancels the worker; the late
    result is dropped instead of written to torn-down widgets.
    """
    directory, delays = remote_dir
    delays["c.txt"] = 0.5
    app = Browsr(config_object=TextualAppContext(file_path=str(directory)))
    async with app.run_test(size=(120, 40)) as pilot:
        code_browser = app.code_browser_screen.code_browser
        await pilot.pause()
        worker = _select(code_browser, directory / "c.txt")
        assert worker.is_running
        # q is processed even though the read is blocked.
        await pilot.press("q")

    # App has exited while the read was still sleeping.
    assert worker.is_cancelled
    assert code_browser.is_attached is False
    # Give the dead worker's thread time to wake up; it must not raise.
    time.sleep(0.7)
