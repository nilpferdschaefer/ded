"""Terminal UI for ded — project sidebar with per-repo log panels."""

from __future__ import annotations

import curses
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

REPO_PREFIX = re.compile(r"^([^:\s][^:]*):\s")
MAX_LINES = 1000
SIDEBAR_WIDTH = 26


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


@dataclass
class RepoPanel:
    key: str
    status: RepoStatus = RepoStatus.IDLE
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_LINES))


@dataclass
class TuiSnapshot:
    repos: list[str]
    panels: dict[str, RepoPanel]
    selected: int
    scroll: int
    system_lines: list[str]
    header: str
    footer: str
    follow: bool


class DedLogSink:
    """Thread-safe per-repo log buffer for the TUI."""

    def __init__(self, repo_keys: list[str], *, timestamp_fn: Callable[[], str]) -> None:
        self._timestamp_fn = timestamp_fn
        self._lock = threading.Lock()
        self._repos = list(repo_keys)
        self._panels = {key: RepoPanel(key=key) for key in repo_keys}
        self._system_lines: deque[str] = deque(maxlen=MAX_LINES)
        self._selected = 0
        self._scroll = 0
        self._follow = True
        self._header = ""
        self._footer = ""
        self._dirty = threading.Event()
        self._dirty.set()

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
                self._panels[repo_key].lines.append(line)
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
                    )
                    for key, panel in self._panels.items()
                },
                selected=self._selected,
                scroll=self._scroll,
                system_lines=list(self._system_lines),
                header=self._header,
                footer=self._footer,
                follow=self._follow,
            )

    def move_selection(self, delta: int) -> None:
        with self._lock:
            if not self._repos:
                return
            self._selected = max(0, min(len(self._repos) - 1, self._selected + delta))
            self._scroll = 0
            self._follow = True
            self._dirty.set()

    def scroll_logs(self, delta: int) -> None:
        with self._lock:
            self._scroll = max(0, self._scroll + delta)
            self._follow = self._scroll == 0
            self._dirty.set()

    def reset_log_scroll(self) -> None:
        with self._lock:
            self._scroll = 0
            self._follow = True
            self._dirty.set()

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


def _visible_lines(lines: list[str], height: int, scroll_from_bottom: int) -> list[str]:
    if height <= 0:
        return []
    if len(lines) <= height:
        return lines
    end = len(lines) - scroll_from_bottom
    end = max(height, min(len(lines), end))
    start = end - height
    return lines[start:end]


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
    sidebar_w = min(SIDEBAR_WIDTH, max(12, width // 3))
    log_left = sidebar_w + 1
    log_width = max(0, width - log_left)

    if body_height > 0 and sidebar_w > 0:
        stdscr.vline(body_top, sidebar_w, curses.ACS_VLINE, body_height)
        stdscr.addstr(body_top, 1, "Projects", curses.A_UNDERLINE)

        visible_repo_count = max(0, body_height - 1)
        repo_start = 0
        if snap.selected >= visible_repo_count:
            repo_start = snap.selected - visible_repo_count + 1

        for row in range(visible_repo_count):
            idx = repo_start + row
            if idx >= len(snap.repos):
                break
            key = snap.repos[idx]
            panel = snap.panels[key]
            mark = STATUS_MARK.get(panel.status, "?")
            prefix = "▶ " if idx == snap.selected else "  "
            label = _truncate(f"{prefix}{mark} {key}", sidebar_w - 2)
            attr = curses.A_REVERSE if idx == snap.selected else curses.A_NORMAL
            if panel.status == RepoStatus.RUNNING and idx != snap.selected:
                attr |= curses.A_BOLD
            stdscr.addstr(body_top + 1 + row, 1, label, attr)

    if body_height > 0 and log_width > 0:
        selected_key = snap.repos[snap.selected] if snap.repos else ""
        panel = snap.panels.get(selected_key)
        title = f"{selected_key}  ({panel.status.value if panel else 'idle'})"
        stdscr.addstr(body_top, log_left + 1, _truncate(title, log_width - 2), curses.A_BOLD)

        log_lines: list[str] = []
        if panel:
            log_lines.extend(panel.lines)
        if snap.system_lines:
            if log_lines:
                log_lines.append("")
            log_lines.append("— system —")
            log_lines.extend(snap.system_lines)

        visible = _visible_lines(log_lines, max(0, body_height - 1), snap.scroll)
        for row, line in enumerate(visible):
            stdscr.addstr(body_top + 1 + row, log_left + 1, _truncate(line, log_width - 2))

    footer = snap.footer or "↑↓ select project  PgUp/PgDn scroll logs  q quit"
    stdscr.addstr(height - 1, 0, _truncate(footer, width - 1), curses.A_DIM)
    stdscr.refresh()


def _init_colors() -> None:
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()


def run_tui(sink: DedLogSink, *, stop: threading.Event) -> None:
    """Run the curses UI until the user quits or stop is set."""

    def _loop(stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        _init_colors()

        while not stop.is_set():
            snap = sink.snapshot()
            _draw(stdscr, snap)
            sink.clear_dirty()

            try:
                key = stdscr.getch()
            except curses.error:
                key = -1

            if key == ord("q") or key == ord("Q"):
                stop.set()
                break
            if key == curses.KEY_UP or key == ord("k"):
                sink.move_selection(-1)
            elif key == curses.KEY_DOWN or key == ord("j"):
                sink.move_selection(1)
            elif key == curses.KEY_PPAGE:
                sink.scroll_logs(10)
            elif key == curses.KEY_NPAGE:
                sink.scroll_logs(-10)
            elif key == curses.KEY_HOME:
                height, _ = stdscr.getmaxyx()
                sink.scroll_logs(10_000)
            elif key == curses.KEY_END:
                sink.reset_log_scroll()
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
