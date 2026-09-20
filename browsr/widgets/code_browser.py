"""
The Primary Content Container
"""

from __future__ import annotations

import inspect
import pathlib
import shutil
from typing import Any

import pyperclip
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Container
from textual.events import Mount
from textual.reactive import var
from textual.widget import Widget
from textual.widgets import DirectoryTree
from textual.worker import Worker, get_current_worker
from textual_universal_directorytree import (
    UPath,
    is_remote_path,
)

from browsr.base import (
    TextualAppContext,
)
from browsr.config import favorite_themes
from browsr.utils import (
    handle_duplicate_filenames,
)
from browsr.widgets.base import BaseOverlay, BasePopUp
from browsr.widgets.confirmation import ConfirmationPopUp, ConfirmationWindow
from browsr.widgets.double_click_directory_tree import DoubleClickDirectoryTree
from browsr.widgets.files import CurrentFileInfoBar
from browsr.widgets.shortcuts import ShortcutsPopUp, ShortcutsWindow
from browsr.widgets.universal_directory_tree import BrowsrDirectoryTree
from browsr.widgets.windows import (
    DataTableWindow,
    FileRenderPayload,
    StaticWindow,
    WindowSwitcher,
)


class CodeBrowser(Container):
    """
    The Code Browser

    This container contains the primary content of the application:

    - Universal Directory Tree
    - Space to view the selected file:
        - Code
        - Table
        - Image
        - Exceptions
    """

    theme_index = var(0)
    rich_themes = favorite_themes
    show_tree = var(True)
    force_show_tree = var(False)
    selected_file_path: UPath | None | var[None] = var(None)

    def __init__(
        self,
        config_object: TextualAppContext,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the Browsr Renderer
        """
        super().__init__(*args, **kwargs)
        self.config_object = config_object
        # Path Handling
        file_path = self.config_object.path
        if not file_path.exists():
            msg = f"Unknown File Path: {file_path}"
            raise FileNotFoundError(msg)
        elif file_path.is_file():
            self.selected_file_path = file_path  # type: ignore[assignment]
            file_path = file_path.parent
        elif file_path.is_dir() and file_path.joinpath("README.md").exists():
            self.selected_file_path = file_path.joinpath("README.md")  # type: ignore[assignment]
            self.force_show_tree = True
        self.initial_file_path = file_path
        self.directory_tree = BrowsrDirectoryTree(file_path, id="tree-view")
        self.window_switcher = WindowSwitcher(config_object=self.config_object)
        self.confirmation = ConfirmationPopUp()
        self.confirmation_window = ConfirmationWindow(
            self.confirmation, id="confirmation-container"
        )
        self.confirmation_window.display = False
        self.shortcuts = ShortcutsPopUp()
        self.shortcuts_window = ShortcutsWindow(
            self.shortcuts, id="shortcuts-container"
        )
        self.shortcuts_window.display = False
        self._content_display_state: dict[Widget, bool] = {}
        # File preview generation tracking. Every new selection / reload /
        # startup preview bumps the generation. Only the latest generation is
        # allowed to commit content, FileInfo or the subtitle.
        self._render_generation: int = 0
        self._committed_generation: int = -1
        self._render_worker: Worker[FileRenderPayload] | None = None
        # Generation whose commit should (re)focus the content window, used to
        # defer focus restoration when an overlay closes mid-render.
        self._pending_focus_generation: int | None = None
        self._overlay_history: list[BaseOverlay] = []
        self._overlay_popup_map: dict[BaseOverlay, BasePopUp] = {
            self.confirmation_window: self.confirmation,
            self.shortcuts_window: self.shortcuts,
        }
        # Copy Pasting
        self._copy_function = pyperclip.determine_clipboard()[0]
        self._copy_supported = inspect.isfunction(self._copy_function)

    @property
    def datatable_window(self) -> DataTableWindow:
        """
        Get the datatable window
        """
        return self.window_switcher.datatable_window

    @property
    def static_window(self) -> StaticWindow:
        """
        Get the static window
        """
        return self.window_switcher.static_window

    def compose(self) -> ComposeResult:
        """
        Compose the content of the container
        """
        yield self.directory_tree
        yield self.window_switcher
        yield self.confirmation_window
        yield self.shortcuts_window

    @on(Mount)
    def bind_keys(self) -> None:
        """
        Bind Keys
        """
        if self._copy_supported:
            self.app.bind(
                keys="c", action="copy_file_path", description="Copy Path", show=False
            )
            self.app.bind(
                keys="C",
                action="copy_text",
                description="Copy Text",
                show=False,
                key_display="shift+c",
            )

        if is_remote_path(self.initial_file_path):  # type: ignore[arg-type]
            self.app.bind(
                keys="x", action="download_file", description="Download", show=True
            )

    def watch_show_tree(self, show_tree: bool) -> None:
        """
        Called when show_tree is modified.
        """
        self.set_class(show_tree, "-show-tree")

    def copy_file_path(self) -> None:
        """
        Copy the file path to the clipboard.
        """
        if self.selected_file_path and self._copy_supported:
            self._copy_function(str(self.selected_file_path))
            self.notify(
                message=f"{self.selected_file_path}",
                title="Copied to Clipboard",
                severity="information",
                timeout=1,
            )

    @on(ConfirmationPopUp.ConfirmationWindowDownload)
    def handle_download_confirmation(
        self, _: ConfirmationPopUp.ConfirmationWindowDownload
    ) -> None:
        """
        Handle the download confirmation.
        """
        self.download_selected_file()

    @on(ConfirmationPopUp.DisplayToggle)
    def handle_confirmation_window_display_toggle(
        self, _: ConfirmationPopUp.DisplayToggle
    ) -> None:
        """
        Handle the confirmation window display toggle.
        """
        self._close_overlay(self.confirmation_window)

    @on(ShortcutsPopUp.DisplayToggle)
    def handle_shortcuts_window_display_toggle(
        self, _: ShortcutsPopUp.DisplayToggle
    ) -> None:
        """
        Handle the shortcuts window display toggle.
        """
        self._close_overlay(self.shortcuts_window)

    def _get_content_window_display_state(self) -> dict[Widget, bool]:
        """
        Capture the current content window visibility state.
        """
        return {
            self.window_switcher.datatable_window: (
                self.window_switcher.datatable_window.display
            ),
            self.window_switcher.text_window: self.window_switcher.text_window.display,
            self.window_switcher.vim_scroll: self.window_switcher.vim_scroll.display,
        }

    def _hide_content_windows(self) -> None:
        """
        Hide the content windows while an overlay is active.
        """
        self.window_switcher.datatable_window.display = False
        self.window_switcher.text_window.display = False
        self.window_switcher.vim_scroll.display = False

    def _restore_content_windows(self) -> None:
        """
        Restore the content windows after all overlays are closed.
        """
        for widget, state in self._content_display_state.items():
            widget.display = state
        if self._committed_generation != self._render_generation:
            # A newer file render is still in flight: focusing now would focus
            # the stale window. The matching commit will move focus for us.
            self._pending_focus_generation = self._render_generation
            return
        active_widget = self.window_switcher.get_active_widget()
        if active_widget is not None:
            active_widget.focus()

    def _get_active_overlay(self) -> BaseOverlay | None:
        """
        Get the currently active overlay.
        """
        if not self._overlay_history:
            return None
        return self._overlay_history[-1]

    def _focus_overlay(self, overlay: BaseOverlay) -> None:
        """
        Focus the popup inside the active overlay.
        """
        self._overlay_popup_map[overlay].focus()

    def _show_overlay(self, overlay: BaseOverlay) -> None:
        """
        Display an overlay while preserving the last content window state.
        """
        active_overlay = self._get_active_overlay()
        if active_overlay is None:
            self._content_display_state = self._get_content_window_display_state()
            self._hide_content_windows()
        elif active_overlay is not overlay:
            active_overlay.display = False

        if overlay in self._overlay_history:
            self._overlay_history.remove(overlay)
        self._overlay_history.append(overlay)
        overlay.display = True
        self._focus_overlay(overlay)

    def _close_overlay(self, overlay: BaseOverlay) -> None:
        """
        Close an overlay and restore the previous overlay or content window.
        """
        if overlay in self._overlay_history:
            was_active_overlay = self._overlay_history[-1] is overlay
            self._overlay_history.remove(overlay)
            overlay.display = False
            if was_active_overlay and self._overlay_history:
                previous_overlay = self._overlay_history[-1]
                previous_overlay.display = True
                self._focus_overlay(previous_overlay)
            elif was_active_overlay:
                self._restore_content_windows()
        else:
            overlay.display = False

    @on(DirectoryTree.FileSelected)
    def handle_file_selected(self, message: DirectoryTree.FileSelected) -> None:
        """
        Called when the user click a file in the directory tree.
        """
        self.selected_file_path = message.path  # type: ignore[assignment]
        self.render_selected_file(file_path=message.path)  # type: ignore[arg-type]

    def render_selected_file(
        self,
        file_path: UPath | None = None,
        *,
        scroll_home: bool = True,
        focus_when_ready: bool = False,
    ) -> Worker[FileRenderPayload]:
        """
        Render a file on a worker thread without blocking the UI.

        Each call starts a new *generation* and cancels the previous worker.
        Workers may keep running on blocked remote IO, but only the latest
        generation is allowed to commit results to the widgets.
        """
        if file_path is None:
            file_path = self.selected_file_path
        if file_path is None:
            msg = "No file is selected to render"
            raise ValueError(msg)
        self.selected_file_path = file_path
        self._render_generation += 1
        generation = self._render_generation
        worker = self._render_file_worker(
            file_path=file_path,
            generation=generation,
            scroll_home=scroll_home,
            focus_when_ready=focus_when_ready,
        )
        self._render_worker = worker
        return worker

    def request_content_focus(self) -> None:
        """
        Focus the active content window now, or once the pending render commits.
        """
        if (
            self._committed_generation == self._render_generation
            and self._get_active_overlay() is None
        ):
            active_widget = self.window_switcher.get_active_widget()
            if active_widget is not None:
                active_widget.focus()
                return
        self._pending_focus_generation = self._render_generation

    @work(thread=True, group="file-preview", exclusive=True, exit_on_error=False)
    def _render_file_worker(
        self,
        file_path: UPath,
        generation: int,
        scroll_home: bool,
        focus_when_ready: bool,
    ) -> FileRenderPayload:
        """
        Load a file on a worker thread and commit it from the UI thread.
        """
        payload = self.window_switcher.prepare_file(
            file_path=file_path, scroll_home=scroll_home
        )
        worker = get_current_worker()
        # A newer selection may have started while the (uninterruptible)
        # remote IO was blocked: its generation wins, drop our result.
        if worker.is_cancelled or generation != self._render_generation:
            return payload
        try:
            self.app.call_from_thread(
                self._commit_file_render,
                payload,
                generation,
                focus_when_ready,
            )
        except RuntimeError:
            # The event loop has already stopped (app is shutting down):
            # there is no UI left to write the result to.
            pass
        return payload

    def _commit_file_render(
        self,
        payload: FileRenderPayload,
        generation: int,
        focus_when_ready: bool,
    ) -> None:
        """
        Commit a prepared render from the UI thread.

        Stale generations (older selections) and detached widgets are ignored
        so late worker results can never overwrite a newer selection.
        """
        if not self.is_attached or generation != self._render_generation:
            return
        self.window_switcher.commit_file(payload)
        self._committed_generation = generation
        if payload.is_error and payload.error is not None:
            self.notify(
                title="Unable to Read File",
                message=(
                    f"{payload.file_path}\n"
                    f"{type(payload.error).__name__}: {payload.error}"
                ),
                severity="error",
                timeout=3,
            )
        if self._get_active_overlay() is not None:
            # An overlay is covering the windows. Refresh the captured state
            # so closing it restores the freshly committed window, and keep
            # the content physically hidden behind the overlay.
            self._content_display_state = self._get_content_window_display_state()
            self._hide_content_windows()
        elif focus_when_ready or self._pending_focus_generation == generation:
            self._pending_focus_generation = None
            active_widget = self.window_switcher.get_active_widget()
            if active_widget is not None:
                active_widget.focus()
        self.post_message(CurrentFileInfoBar.FileInfoUpdate(new_file=payload.file_info))

    @on(DoubleClickDirectoryTree.DirectoryDoubleClicked)
    def handle_directory_double_click(
        self, message: DoubleClickDirectoryTree.DirectoryDoubleClicked
    ) -> None:
        """
        Called when the user double clicks a directory in the directory tree.
        """
        self.directory_tree.path = message.path
        self.notify(
            title="Directory Changed",
            message=str(message.path),
            severity="information",
            timeout=1,
        )

    @on(DoubleClickDirectoryTree.FileDoubleClicked)
    def handle_file_double_click(
        self, message: DoubleClickDirectoryTree.FileDoubleClicked
    ) -> None:
        """
        Called when the user double clicks a file in the directory tree.
        """
        if self._copy_supported:
            self._copy_function(str(message.path))
            self.notify(
                message=f"{message.path}",
                title="Copied to Clipboard",
                severity="information",
                timeout=1,
            )

    def download_file_workflow(self) -> None:
        """
        Download the selected file.
        """
        if self.selected_file_path is None:
            return
        elif self.selected_file_path.is_dir():
            return
        elif is_remote_path(self.selected_file_path):
            if self._get_active_overlay() is self.confirmation_window:
                self._close_overlay(self.confirmation_window)
                return
            handled_download_path = self._get_download_file_name()
            self.confirmation.prompt_download(
                file_path=str(self.selected_file_path),
                download_path=str(handled_download_path),
            )
            self._show_overlay(self.confirmation_window)

    def toggle_shortcuts(self) -> None:
        """
        Toggle the shortcuts window.
        """
        if self._get_active_overlay() is self.shortcuts_window:
            self._close_overlay(self.shortcuts_window)
        else:
            self.shortcuts.update_shortcuts()
            self._show_overlay(self.shortcuts_window)

    @work(thread=True)
    def download_selected_file(self) -> None:
        """
        Download the selected file.
        """
        if self.selected_file_path is None:
            return
        elif self.selected_file_path.is_dir():
            return
        elif is_remote_path(self.selected_file_path):
            handled_download_path = self._get_download_file_name()
            with self.selected_file_path.open("rb") as file_handle:
                with handled_download_path.open("wb") as download_handle:
                    shutil.copyfileobj(file_handle, download_handle)
            self.notify(
                message=str(handled_download_path),
                title="Download Complete",
                severity="information",
                timeout=2,
            )

    def _get_download_file_name(self) -> UPath | pathlib.Path:
        """
        Get the download file name.
        """
        download_dir = pathlib.Path.home() / "Downloads"
        if not download_dir.exists():
            msg = f"Download directory {download_dir} not found"
            raise FileNotFoundError(msg)
        download_path = download_dir / self.selected_file_path.name  # type: ignore[union-attr]
        handled_download_path = handle_duplicate_filenames(file_path=download_path)
        return handled_download_path
