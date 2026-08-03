"""Terminal UI for ded — project sidebar with per-repo log panels."""

from __future__ import annotations

import curses
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Literal

REPO_PREFIX = re.compile(r"^([^:\s][^:]*):\s")
MAX_LINES = 1000
SIDEBAR_WIDTH = 28
SCROLLBAR_WIDTH = 1


class RepoStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    OK = "ok"
    ERROR = "error"
    SKIP = "skip"


STATUS_MARK = {
    RepoStatus.IDLE: "○",
    RepoStatus.RUNNING: "●",
    RepoStatus.OK: "✓",
    RepoStatus.ERROR: "✗",
    RepoStatus.SKIP: "–",
}

Focus = Literal["sidebar", "logs"]


@dataclass
class RepoPanel:
    key: str
    status: RepoStatus = RepoStatus.IDLE
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))
    scroll: int = 0
    follow: bool = True


@dataclass
class TuiSnapshot:
    repos: list[str]
    panels: dict[str, RepoPanel]
    selected: int
    sidebar_scroll: int
    focus: Focus
    system_lines: list[str]
    header: str
    footer: str


class DedLogSink:
    """Thread-safe per-repo log buffer for the TUI."""

    def __init__(self, repo_keys: list[str], *, timestamp_fn: Callable[[], str]) -> None:
        self._timestamp_fn = timestamp_fn
        self._lock = threading.Lock()
        self._repos = list(repo_keys)
        self._panels = {key: RepoPanel(key=key) for key in repo_keys}
        self._system_lines: deque[str] = deque(maxlen=MAX_LINES)
        self._selected = 0
        self._sidebar_scroll = 0
        self._focus: Focus = "sidebar"
        self._header = ""
        self._footer = ""
        self._dirty = threading.Event()
        self._dirty.set()

    def _selected_panel(self) -> RepoPanel | None:
        if not self._repos:
            return None
        return self._panels[self._repos[self._selected]]

    def set_header(self, text: str) -> None:
        with self._lock:
            self._header = text
            self._dirty.set()

    def set_footer(self, text: str) -> None:
        with self._lock:
            self._footer = text
            self._dirty.set()

    def set_repo_status(self, repo: str, status: str) -> None:
        with self._lock:
            panel = self._panels.get(repo)
            if panel is None:
                return
            panel.status = RepoStatus(status)
            self._dirty.set()

    def emit(self, msg: str, *, repo: str | None = None) -> None:
        repo_key = repo
        body = msg
        if repo_key is None:
            match = REPO_PREFIX.match(msg)
            if match and match.group(1) in self._panels:
                repo_key = match.group(1)
                body = msg[match.end() :]

        line = f"[ded {self._timestamp_fn()}] {body}"
        with self._lock:
            if repo_key and repo_key in self._panels:
                panel = self._panels[repo_key]
                panel.lines.append(line)
                if panel.follow:
                    panel.scroll = 0
            else:
                self._system_lines.append(line)
            self._dirty.set()

    def snapshot(self) -> TuiSnapshot:
        with self._lock:
            return TuiSnapshot(
                repos=list(self._repos),
                panels={
                    key: RepoPanel(
                        key=panel.key,
                        status=panel.status,
                        lines=deque(panel.lines, maxlen=MAX_LINES),
                        scroll=panel.scroll,
                        follow=panel.follow,
                    )
                    for key, panel in self._panels.items()
                },
                selected=self._selected,
                sidebar_scroll=self._sidebar_scroll,
                focus=self._focus,
                system_lines=list(self._system_lines),
                header=self._header,
                footer=self._footer,
            )

    def toggle_focus(self) -> None:
        with self._lock:
            self._focus = "logs" if self._focus == "sidebar" else "sidebar"
            self._dirty.set()

    def set_focus(self, focus: Focus) -> None:
        with self._lock:
            self._focus = focus
            self._dirty.set()

    def _ensure_sidebar_visible(self, viewport: int) -> None:
        if viewport <= 0 or not self._repos:
            return
        if self._selected < self._sidebar_scroll:
            self._sidebar_scroll = self._selected
        elif self._selected >= self._sidebar_scroll + viewport:
            self._sidebar_scroll = self._selected - viewport + 1
        max_scroll = max(0, len(self._repos) - viewport)
        self._sidebar_scroll = max(0, min(max_scroll, self._sidebar_scroll))

    def move_selection(self, delta: int, *, viewport: int = 1) -> None:
        with self._lock:
            if not self._repos:
                return
            self._selected = max(0, min(len(self._repos) - 1, self._selected + delta))
            self._ensure_sidebar_visible(viewport)
            self._dirty.set()

    def scroll_sidebar(self, delta: int, *, viewport: int) -> None:
        with self._lock:
            max_scroll = max(0, len(self._repos) - viewport)
            self._sidebar_scroll = max(0, min(max_scroll, self._sidebar_scroll + delta))
            self._dirty.set()

    def scroll_logs(self, delta: int, *, viewport: int) -> None:
        with self._lock:
            panel = self._selected_panel()
            if panel is None:
                return
            total = self._log_line_count_locked(panel)
            max_scroll = max(0, total - viewport)
            panel.scroll = max(0, min(max_scroll, panel.scroll + delta))
            panel.follow = panel.scroll == 0
            self._dirty.set()

    def page_scroll_logs(self, direction: int, *, viewport: int) -> None:
        page = max(1, viewport - 1)
        self.scroll_logs(direction * page, viewport=viewport)

    def jump_logs(self, to_top: bool, *, viewport: int) -> None:
        with self._lock:
            panel = self._selected_panel()
            if panel is None:
                return
            total = self._log_line_count_locked(panel)
            max_scroll = max(0, total - viewport)
            panel.scroll = max_scroll if to_top else 0
            panel.follow = not to_top
            self._dirty.set()

    def _log_line_count_locked(self, panel: RepoPanel) -> int:
        total = len(panel.lines)
        if self._system_lines:
            if total:
                total += 1
            total += 1 + len(self._system_lines)
        return total

    def wait_dirty(self, timeout: float = 0.25) -> bool:
        return self._dirty.wait(timeout)

    def clear_dirty(self) -> None:
        self._dirty.clear()


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _log_lines_for_panel(panel: RepoPanel | None, system_lines: list[str]) -> list[str]:
    lines: list[str] = []
    if panel:
        lines.extend(panel.lines)
    if system_lines:
        if lines:
            lines.append("")
        lines.append("— system —")
        lines.extend(system_lines)
    return lines


def _visible_slice(lines: list[str], height: int, scroll_from_bottom: int) -> tuple[list[str], int, int]:
    """Return visible lines, first index, and max scroll offset."""
    if height <= 0:
        return [], 0, 0
    if len(lines) <= height:
        return lines, 0, 0
    max_scroll = len(lines) - height
    scroll = max(0, min(max_scroll, scroll_from_bottom))
    end = len(lines) - scroll
    start = end - height
    return lines[start:end], start, max_scroll


def _draw_scrollbar(
    stdscr: curses.window,
    col: int,
    top: int,
    height: int,
    total: int,
    first_visible: int,
    *,
    active: bool,
) -> None:
    if height <= 0 or total <= height:
        return
    track_attr = curses.A_BOLD if active else curses.A_DIM
    thumb_attr = curses.A_REVERSE | curses.A_BOLD if active else curses.A_DIM

    max_offset = total - height
    thumb_size = max(1, round(height * height / total))
    thumb_size = min(thumb_size, height)
    if max_offset <= 0:
        thumb_row = top
    else:
        thumb_row = top + round((first_visible / max_offset) * (height - thumb_size))

    for row in range(top, top + height):
        if thumb_row <= row < thumb_row + thumb_size:
            stdscr.addch(row, col, "█", thumb_attr)
        else:
            stdscr.addch(row, col, "│", track_attr)


def _draw(stdscr: curses.window, snap: TuiSnapshot) -> None:
    height, width = stdscr.getmaxyx()
    if height < 4 or width < 40:
        return

    stdscr.erase()
    header = _truncate(snap.header, width - 1)
    stdscr.addstr(0, 0, header, curses.A_BOLD)

    body_top = 1
    body_bottom = height - 2
    body_height = max(0, body_bottom - body_top)
    sidebar_outer = min(SIDEBAR_WIDTH, max(14, width // 3))
    sidebar_text_w = max(1, sidebar_outer - SCROLLBAR_WIDTH - 2)
    sidebar_scroll_col = sidebar_outer - 1
    divider_col = sidebar_outer
    log_left = divider_col + 1
    log_text_w = max(1, width - log_left - SCROLLBAR_WIDTH - 1)
    log_scroll_col = width - 1

    if body_height > 0 and sidebar_outer > 0:
        sidebar_attr = curses.A_BOLD if snap.focus == "sidebar" else curses.A_DIM
        stdscr.vline(body_top, divider_col, curses.ACS_VLINE, body_height)
        stdscr.addstr(body_top, 1, _truncate("Projects", sidebar_text_w), curses.A_UNDERLINE | sidebar_attr)

        list_height = max(0, body_height - 1)
        repo_total = len(snap.repos)
        repo_start = max(0, min(snap.sidebar_scroll, max(0, repo_total - list_height)))

        for row in range(list_height):
            idx = repo_start + row
            if idx >= repo_total:
                break
            key = snap.repos[idx]
            panel = snap.panels[key]
            mark = STATUS_MARK.get(panel.status, "?")
            prefix = "▶ " if idx == snap.selected else "  "
            label = _truncate(f"{prefix}{mark} {key}", sidebar_text_w)
            attr = curses.A_REVERSE if idx == snap.selected else curses.A_NORMAL
            if snap.focus == "sidebar" and idx == snap.selected:
                attr |= curses.A_BOLD
            elif panel.status == RepoStatus.RUNNING and idx != snap.selected:
                attr |= curses.A_BOLD
            stdscr.addstr(body_top + 1 + row, 1, label, attr)

        _draw_scrollbar(
            stdscr,
            sidebar_scroll_col,
            body_top + 1,
            list_height,
            repo_total,
            repo_start,
            active=snap.focus == "sidebar",
        )

    if body_height > 0 and log_text_w > 0:
        selected_key = snap.repos[snap.selected] if snap.repos else ""
        panel = snap.panels.get(selected_key)
        log_attr = curses.A_BOLD if snap.focus == "logs" else curses.A_DIM
        follow = "tail" if panel and panel.follow else "scroll"
        title = f"{selected_key}  ({panel.status.value if panel else 'idle'}, {follow})"
        stdscr.addstr(body_top, log_left, _truncate(title, log_text_w), curses.A_UNDERLINE | log_attr)

        log_lines = _log_lines_for_panel(panel, snap.system_lines)
        list_height = max(0, body_height - 1)
        scroll = panel.scroll if panel else 0
        visible, first_idx, _max_scroll = _visible_slice(log_lines, list_height, scroll)
        for row, line in enumerate(visible):
            stdscr.addstr(body_top + 1 + row, log_left, _truncate(line, log_text_w))

        _draw_scrollbar(
            stdscr,
            log_scroll_col,
            body_top + 1,
            list_height,
            len(log_lines),
            first_idx,
            active=snap.focus == "logs",
        )

    footer = snap.footer or (
        "Tab switch pane  ↑↓ scroll  PgUp/PgDn page  Home/End top/bottom  q quit"
    )
    stdscr.addstr(height - 1, 0, _truncate(footer, width - 1), curses.A_DIM)
    stdscr.refresh()


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()


def _pane_at(
    my: int,
    mx: int,
    *,
    body_top: int,
    body_height: int,
    divider_col: int,
) -> Focus | None:
    if my < body_top or my >= body_top + body_height:
        return None
    if mx <= divider_col:
        return "sidebar"
    return "logs"


def _handle_mouse(
    sink: DedLogSink,
    stdscr: curses.window,
    *,
    body_top: int,
    body_height: int,
    divider_col: int,
    list_height: int,
) -> None:
    try:
        _mouse_id, mx, my, _mz, bstate = curses.getmouse()
    except curses.error:
        return

    pane = _pane_at(my, mx, body_top=body_top, body_height=body_height, divider_col=divider_col)
    if pane is None:
        return
    sink.set_focus(pane)

    wheel_up = bool(bstate & curses.BUTTON4_PRESSED)
    wheel_down = bool(bstate & curses.BUTTON2_PRESSED or bstate & curses.BUTTON5_PRESSED)
    if not wheel_up and not wheel_down:
        return

    delta = -3 if wheel_up else 3
    if pane == "sidebar":
        sink.scroll_sidebar(delta, viewport=list_height)
    else:
        sink.scroll_logs(delta, viewport=list_height)


def run_tui(sink: DedLogSink, *, stop: threading.Event) -> None:
    """Run the curses UI until the user quits or stop is set."""

    def _loop(stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        _init_colors()
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
            curses.mouseinterval(0)
        except curses.error:
            pass

        while not stop.is_set():
            height, width = stdscr.getmaxyx()
            snap = sink.snapshot()
            _draw(stdscr, snap)
            sink.clear_dirty()

            body_top = 1
            body_bottom = height - 2
            body_height = max(0, body_bottom - body_top)
            sidebar_outer = min(SIDEBAR_WIDTH, max(14, width // 3))
            divider_col = sidebar_outer
            list_height = max(0, body_height - 1)

            try:
                key = stdscr.getch()
            except curses.error:
                key = -1

            if key == ord("q") or key == ord("Q"):
                stop.set()
                break
            if key == ord("\t"):
                sink.toggle_focus()
                continue
            if key == curses.KEY_MOUSE:
                _handle_mouse(
                    sink,
                    stdscr,
                    body_top=body_top,
                    body_height=body_height,
                    divider_col=divider_col,
                    list_height=list_height,
                )
                continue

            focus = sink.snapshot().focus
            if key == curses.KEY_UP or key == ord("k"):
                if focus == "sidebar":
                    sink.move_selection(-1, viewport=list_height)
                else:
                    sink.scroll_logs(-1, viewport=list_height)
            elif key == curses.KEY_DOWN or key == ord("j"):
                if focus == "sidebar":
                    sink.move_selection(1, viewport=list_height)
                else:
                    sink.scroll_logs(1, viewport=list_height)
            elif key == curses.KEY_PPAGE:
                if focus == "sidebar":
                    sink.scroll_sidebar(-list_height, viewport=list_height)
                else:
                    sink.page_scroll_logs(1, viewport=list_height)
            elif key == curses.KEY_NPAGE:
                if focus == "sidebar":
                    sink.scroll_sidebar(list_height, viewport=list_height)
                else:
                    sink.page_scroll_logs(-1, viewport=list_height)
            elif key == curses.KEY_HOME:
                if focus == "sidebar":
                    sink.scroll_sidebar(-10_000, viewport=list_height)
                else:
                    sink.jump_logs(to_top=True, viewport=list_height)
            elif key == curses.KEY_END:
                if focus == "sidebar":
                    sink.scroll_sidebar(10_000, viewport=list_height)
                else:
                    sink.jump_logs(to_top=False, viewport=list_height)
            elif key == -1:
                sink.wait_dirty(0.2)
            else:
                sink.wait_dirty(0.05)

    try:
        curses.wrapper(_loop)
    except curses.error:
        # Terminal too small or not capable — caller should fall back to plain logs.
        stop.set()
        raise
