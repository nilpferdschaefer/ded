#!/usr/bin/env python3
"""
ded — local deploy daemon.

Watches configured GitHub repos, waits for CI to publish artifacts,
then runs your project's deploy script (e.g. ansible-deploy.sh).

Requires: gh (authenticated), --config pointing to your project's ded.json.
"""

from __future__ import annotations

import argparse
import contextvars
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ded_tui import DedLogSink


RELEASE_FLAG_TO_CLI = {
    "datastore_release": "datastore-release",
    "analytics_release": "analytics-release",
    "ram_release": "ram-release",
    "executor_release": "executor-release",
}

DED_VERSION = "2.0.0"

_current_repo: contextvars.ContextVar[str | None] = contextvars.ContextVar("ded_repo", default=None)
_log_sink: DedLogSink | None = None

DEPLOY_ENV_KEYS = (
    "BOT_HOST",
    "DEPLOY_SSH_HOST",
    "ANSIBLE_VAULT_PASSWORD",
    "SSH_PRIVATE_KEY",
    "SSH_PRIVATE_KEY_FILE",
    "DEPLOY_SSH_KEY",
    "DEPLOY_SSH_KEY_FILE",
    "DEPLOY_SSH_USER",
    "DEPLOY_SSH_PORT",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_duration(sec: float) -> str:
    if sec < 60:
        return f"{sec:.1f}s"
    minutes, remainder = divmod(sec, 60)
    if minutes < 60:
        return f"{int(minutes)}m{remainder:.0f}s"
    hours, remainder = divmod(minutes, 60)
    return f"{int(hours)}h{int(remainder)}m"


def ansible_profile_callbacks_env() -> dict[str, str]:
    """Enable Ansible profile_tasks so per-task durations appear in deploy output."""
    existing = os.environ.get("ANSIBLE_CALLBACKS_ENABLED", "")
    callbacks = [part.strip() for part in existing.split(",") if part.strip()]
    if "profile_tasks" not in callbacks:
        callbacks.append("profile_tasks")
    return {"ANSIBLE_CALLBACKS_ENABLED": ",".join(callbacks)}


class AnsibleTimingCollector:
    """Parse profile_tasks summary lines from ansible-playbook output."""

    _TASK_SUMMARY_RE = re.compile(r"^(.+?)\s+(\d+(?:\.\d+)?)s\s*$")
    _PLAYBOOK_TOTAL_RE = re.compile(
        r"^Playbook run took .*? (\d+):(\d{2}):(\d+(?:\.\d+)?)\s*$"
    )

    def __init__(self) -> None:
        self.tasks: list[dict[str, Any]] = []
        self.playbook_duration_sec: float | None = None

    def feed(self, line: str) -> None:
        stripped = line.strip()
        playbook_match = self._PLAYBOOK_TOTAL_RE.match(stripped)
        if playbook_match:
            hours, minutes, seconds = playbook_match.groups()
            self.playbook_duration_sec = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
            return

        task_match = self._TASK_SUMMARY_RE.match(stripped)
        if not task_match:
            return
        task_name = task_match.group(1).strip()
        if not task_name or task_name.startswith("Playbook run took"):
            return
        duration = float(task_match.group(2))
        self.tasks.append({"task": task_name, "duration_sec": round(duration, 4)})

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.playbook_duration_sec is not None:
            out["ansible_playbook_sec"] = round(self.playbook_duration_sec, 2)
        if self.tasks:
            out["ansible_tasks"] = sorted(
                self.tasks,
                key=lambda item: item["duration_sec"],
                reverse=True,
            )
        return out


def set_log_sink(sink: DedLogSink | None) -> None:
    global _log_sink
    _log_sink = sink


def log(msg: str, *, repo: str | None = None) -> None:
    repo_key = repo or _current_repo.get()
    if _log_sink is not None:
        _log_sink.emit(msg, repo=repo_key)
        return
    print(f"[ded {utc_now()}] {msg}", flush=True)


def parse_dotenv_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from a dotenv file (does not mutate os.environ)."""
    if not path.is_file():
        return {}
    parsed: dict[str, str] = {}
    text = path.read_text(encoding="utf-8-sig")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value.startswith("~/") or value == "~":
            value = str(Path(value).expanduser())
        parsed[key] = value
    return parsed


def load_dotenv(path: Path, *, override: bool = False) -> bool:
    """Load KEY=VALUE lines into os.environ (stdlib-only dotenv)."""
    if not path.is_file():
        return False
    loaded_any = False
    for key, value in parse_dotenv_file(path).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        loaded_any = True
    return loaded_any


def ded_root_dir() -> Path:
    return Path(__file__).resolve().parent


def deploy_env_candidates(
    stack_root: Path,
    *,
    env_file: Path | None = None,
    config_path: Path | None = None,
) -> list[Path]:
    if env_file is not None:
        return [env_file.expanduser()]
    candidates: list[Path] = []
    for env_key in ("DED_ENV", "DEPLOYD_ENV"):
        val = os.environ.get(env_key, "").strip()
        if val:
            candidates.append(Path(val).expanduser())
    if config_path is not None:
        candidates.append(config_path.parent / "ded.env")
        candidates.append(config_path.parent / "deployd.env")
    candidates.extend(
        [
            stack_root / "config" / "ded.env",
            stack_root / "config" / "deployd.env",
            stack_root / ".env",
        ]
    )
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def load_deploy_env(
    stack_root: Path,
    *,
    env_file: Path | None = None,
    config_path: Path | None = None,
) -> tuple[list[Path], list[Path], list[Path]]:
    """Load deploy credentials from .env files. Shell exports win over file values."""
    candidates = deploy_env_candidates(stack_root, env_file=env_file, config_path=config_path)
    loaded: list[Path] = []
    empty_files: list[Path] = []
    for path in candidates:
        if not path.is_file():
            continue
        if load_dotenv(path):
            loaded.append(path)
        else:
            empty_files.append(path)
    missing = [path for path in candidates if not path.is_file()]
    return loaded, missing, empty_files


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def normalize_deploy_env() -> None:
    """Map DEPLOY_SSH_* names (mainnet.env convention) to ansible-deploy vars."""
    if not env_value("BOT_HOST") and env_value("DEPLOY_SSH_HOST"):
        os.environ["BOT_HOST"] = env_value("DEPLOY_SSH_HOST")
    if not env_value("ANSIBLE_USER") and env_value("DEPLOY_SSH_USER"):
        os.environ["ANSIBLE_USER"] = env_value("DEPLOY_SSH_USER")
    if not env_value("ANSIBLE_SSH_PORT") and env_value("DEPLOY_SSH_PORT"):
        os.environ["ANSIBLE_SSH_PORT"] = env_value("DEPLOY_SSH_PORT")
    if not env_value("SSH_PRIVATE_KEY_FILE") and env_value("DEPLOY_SSH_KEY_FILE"):
        os.environ["SSH_PRIVATE_KEY_FILE"] = env_value("DEPLOY_SSH_KEY_FILE")
    if not env_value("SSH_PRIVATE_KEY") and env_value("DEPLOY_SSH_KEY"):
        os.environ["SSH_PRIVATE_KEY"] = env_value("DEPLOY_SSH_KEY")


def env_debug_lines(
    *,
    env_loaded: list[Path] | None = None,
    env_missing: list[Path] | None = None,
    env_empty: list[Path] | None = None,
) -> list[str]:
    normalize_deploy_env()
    lines = [f"ded version {DED_VERSION} ({Path(__file__).resolve()})"]
    if env_loaded:
        lines.append(f"loaded env file(s): {', '.join(str(p) for p in env_loaded)}")
    elif env_missing:
        lines.append("no env file loaded; looked for:")
        lines.extend(f"  - {p}" for p in env_missing)
    if env_empty:
        lines.append("env file(s) with no KEY=VALUE assignments:")
        lines.extend(f"  - {p}" for p in env_empty)
    for key in DEPLOY_ENV_KEYS:
        val = env_value(key)
        if not val:
            lines.append(f"  {key}: (unset)")
        elif "PASSWORD" in key or "KEY" in key:
            lines.append(f"  {key}: (set, len={len(val)})")
        else:
            lines.append(f"  {key}: {val}")
    return lines


def shell_export(path: Path) -> None:
    """Print shell-safe export lines for bash/zsh eval (used by ded wrapper)."""
    import shlex

    for key, value in parse_dotenv_file(path).items():
        print(f"export {key}={shlex.quote(value)}")


def die(msg: str, code: int = 1) -> None:
    log(f"ERROR: {msg}")
    sys.exit(code)


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    capture: bool = False,
    stream: bool | None = None,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    repo_key = _current_repo.get()
    if stream is None:
        stream = _log_sink is not None and repo_key is not None and not capture
    if stream:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=merged,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip("\n")
            if text:
                log(text, repo=repo_key)
        proc.wait()
        if check and proc.returncode != 0:
            raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, proc.returncode or 0, "", "")

    result = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged,
        text=True,
        capture_output=capture,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip() or (result.stdout or "").strip()
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    if capture and _log_sink is not None and repo_key is not None:
        for line in (result.stdout or "").splitlines():
            if line.strip():
                log(line, repo=repo_key)
        for line in (result.stderr or "").splitlines():
            if line.strip():
                log(line, repo=repo_key)
    return result


def run_streaming(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_line: Callable[[str], None] | None = None,
    check: bool = True,
) -> float:
    """Run a command, stream each output line, and return wall-clock duration in seconds."""
    merged = os.environ.copy()
    if env:
        merged.update(env)
    repo_key = _current_repo.get()
    start = time.perf_counter()
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        text = line.rstrip("\n")
        if not text:
            continue
        if on_line is not None:
            on_line(text)
        elif _log_sink is not None:
            log(text, repo=repo_key)
        else:
            print(text, flush=True)
    proc.wait()
    duration = time.perf_counter() - start
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return duration


def require_tool(name: str) -> None:
    run(["bash", "-lc", f"command -v {name}"], capture=True)


def load_config_data(path: Path) -> dict[str, Any]:
    """Parse ded JSON config with actionable errors."""
    try:
        raw = path.read_text()
    except OSError as exc:
        die(f"cannot read config {path}: {exc}")

    stripped = raw.strip()
    if not stripped:
        die(
            f"config is empty: {path}\n"
            f"  Create ded.json in your deploy project — see docs/config.md"
        )

    head = stripped.splitlines()[0].strip()
    if head.startswith("#") or "=" in head and not head.startswith("{"):
        die(
            f"config does not look like JSON: {path}\n"
            f"  deploy credentials: your project's ded.env (see docs/env.example)\n"
            f"  repo/watch settings: your project's ded.json (see docs/config.md)"
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        die(
            f"invalid JSON in config {path} (line {exc.lineno}, column {exc.colno}): {exc.msg}\n"
            f"  Validate with: python3 -m json.tool {path}\n"
            f"  See docs/config.md in the ded install"
        )

    if not isinstance(data, dict):
        die(f"config root must be a JSON object in {path}, got {type(data).__name__}")

    missing = [key for key in ("github_owner", "repos") if key not in data]
    if missing:
        die(
            f"config {path} is missing required field(s): {', '.join(missing)}\n"
            f"  Compare with docs/config.md"
        )
    if not isinstance(data["repos"], dict) or not data["repos"]:
        die(f"config {path}: \"repos\" must be a non-empty object")

    return data


@dataclass
class DeployConfig:
    raw: dict[str, Any]
    github_owner: str
    poll_interval_sec: int
    workers: int
    network: str
    stack_root: Path
    state_dir: Path
    repos: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path) -> "DeployConfig":
        data = load_config_data(path)
        stack_root_raw = Path(data.get("stack_root", ".."))
        stack_root = stack_root_raw.resolve() if stack_root_raw.is_absolute() else (path.parent / stack_root_raw).resolve()
        state_dir = Path(data.get("state_dir", ".ded"))
        if not state_dir.is_absolute():
            state_dir = (stack_root / state_dir).resolve()
        workers = int(data.get("workers", 1))
        if workers < 1:
            die(f"config {path}: workers must be >= 1, got {workers}")
        return cls(
            raw=data,
            github_owner=data["github_owner"],
            poll_interval_sec=int(data.get("poll_interval_sec", 90)),
            workers=workers,
            network=data.get("network", "testnet"),
            stack_root=stack_root,
            state_dir=state_dir,
            repos=data["repos"],
        )


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {"repos": {}}
        if self.path.exists():
            self.data = json.loads(self.path.read_text())

    def repo(self, name: str) -> dict[str, Any]:
        repos = self.data.setdefault("repos", {})
        return repos.setdefault(name, {})

    def save(self) -> None:
        with self._lock:
            self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")


@dataclass
class RepoContext:
    key: str
    cfg: dict[str, Any]
    owner: str
    repo: str
    branch: str
    local_path: Path | None


class DeployDaemon:
    def __init__(self, config: DeployConfig, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.state = StateStore(config.state_dir / "state.json")

    def full_repo(self, ctx: RepoContext) -> str:
        return f"{ctx.owner}/{ctx.repo}"

    def ctx_for(self, key: str) -> RepoContext:
        cfg = self.config.repos[key]
        local = cfg.get("local_path")
        return RepoContext(
            key=key,
            cfg=cfg,
            owner=self.config.github_owner,
            repo=cfg.get("repo", key),
            branch=cfg.get("branch", "main"),
            local_path=Path(local).resolve() if local else None,
        )

    def gh(self, *args: str, capture: bool = True) -> str:
        cmd = ["gh", *args]
        if self.dry_run and args and args[0] not in {"auth", "version"}:
            log(f"DRY-RUN gh {' '.join(args)}")
            return ""
        result = run(cmd, capture=capture)
        return (result.stdout or "").strip()

    def latest_sha(self, ctx: RepoContext) -> str:
        if ctx.local_path and (ctx.local_path / ".git").exists():
            run(["git", "-C", str(ctx.local_path), "fetch", "origin", ctx.branch], capture=True)
            return run(
                ["git", "-C", str(ctx.local_path), "rev-parse", f"origin/{ctx.branch}"],
                capture=True,
            ).stdout.strip()
        out = self.gh(
            "api",
            f"repos/{self.full_repo(ctx)}/commits/{ctx.branch}",
            "--jq",
            ".sha",
        )
        return out

    def parent_sha(self, ctx: RepoContext, sha: str) -> str | None:
        if ctx.local_path and (ctx.local_path / ".git").exists():
            parent = run(
                ["git", "-C", str(ctx.local_path), "rev-parse", f"{sha}^"],
                capture=True,
                check=False,
            ).stdout.strip()
            return parent or None
        parent = self.gh(
            "api",
            f"repos/{self.full_repo(ctx)}/commits/{sha}",
            "--jq",
            ".parents[0].sha",
        )
        return parent or None

    def changed_files(self, ctx: RepoContext, sha: str) -> list[str]:
        if ctx.local_path and (ctx.local_path / ".git").exists():
            parent = self.parent_sha(ctx, sha)
            if not parent:
                return ["(root)"]
            return [
                line
                for line in run(
                    ["git", "-C", str(ctx.local_path), "diff", "--name-only", parent, sha],
                    capture=True,
                ).stdout.splitlines()
                if line
            ]
        compare = self.gh(
            "api",
            f"repos/{self.full_repo(ctx)}/commits/{sha}",
            "--jq",
            ".files[].filename",
        )
        return [line for line in compare.splitlines() if line]

    def _run_matches_workflow(self, run_info: dict[str, Any], workflow_file: str) -> bool:
        stem = Path(workflow_file).stem.lower()
        name = (run_info.get("workflowName") or "").lower()
        return stem == name or name.startswith(stem)

    def is_ancestor(self, ctx: RepoContext, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        if ctx.local_path and (ctx.local_path / ".git").exists():
            run(["git", "-C", str(ctx.local_path), "fetch", "origin", ctx.branch], capture=True)
            result = run(
                ["git", "-C", str(ctx.local_path), "merge-base", "--is-ancestor", ancestor, descendant],
                capture=True,
                check=False,
            )
            return result.returncode == 0
        status = self.gh(
            "api",
            f"repos/{self.full_repo(ctx)}/compare/{ancestor}...{descendant}",
            "--jq",
            ".status",
        )
        return status in {"ahead", "identical"}

    def has_workflow_run(self, ctx: RepoContext, sha: str, workflow: str) -> bool:
        return bool(self.workflow_runs_for_commit(ctx, sha, workflow) or self.workflow_runs_for_commit(ctx, sha, None))

    def latest_successful_ci_sha(self, ctx: RepoContext, head_sha: str, workflow: str) -> str | None:
        for run_info in self.recent_branch_runs(ctx, limit=30):
            if run_info.get("conclusion") != "success":
                continue
            run_sha = run_info.get("headSha")
            if not run_sha or not self.is_ancestor(ctx, run_sha, head_sha):
                continue
            if workflow and not self._run_matches_workflow(run_info, workflow):
                continue
            return run_sha
        return None

    def resolve_ci_sha(self, ctx: RepoContext, head_sha: str, workflow: str) -> str:
        """Resolve which commit's CI artifacts to use for this HEAD."""
        if self.has_workflow_run(ctx, head_sha, workflow):
            return head_sha

        current = head_sha
        visited: set[str] = set()
        while current not in visited:
            visited.add(current)
            if not self.changed_files(ctx, current):
                parent = self.parent_sha(ctx, current)
                if not parent:
                    break
                log(f"{ctx.key}: {current[:7]} has no file changes — checking parent {parent[:7]}")
                current = parent
                if self.has_workflow_run(ctx, current, workflow):
                    log(
                        f"{ctx.key}: {head_sha[:7]} has no CI — "
                        f"using build from {current[:7]}"
                    )
                    return current
                continue
            break

        ancestor = self.latest_successful_ci_sha(ctx, head_sha, workflow)
        if ancestor:
            log(f"{ctx.key}: {head_sha[:7]} has no CI — using build from {ancestor[:7]}")
            return ancestor

        return head_sha

    def paths_changed(self, ctx: RepoContext, sha: str, prefixes: list[str]) -> bool:
        if not prefixes:
            return True
        if ctx.local_path and (ctx.local_path / ".git").exists():
            parent = run(
                ["git", "-C", str(ctx.local_path), "rev-parse", f"{sha}^"],
                capture=True,
                check=False,
            ).stdout.strip()
            if not parent:
                return True
            diff = run(
                ["git", "-C", str(ctx.local_path), "diff", "--name-only", parent, sha],
                capture=True,
            ).stdout.splitlines()
            return any(any(p.startswith(prefix) for prefix in prefixes) for p in diff)
        compare = self.gh(
            "api",
            f"repos/{self.full_repo(ctx)}/commits/{sha}",
            "--jq",
            ".files[].filename",
        )
        files = [line for line in compare.splitlines() if line]
        return any(any(f.startswith(prefix) for prefix in prefixes) for f in files)

    def release_tag_for(self, build: dict[str, Any], sha: str) -> str:
        template = build.get("release_tag", "ci-{sha_short}")
        return template.format(sha=sha, sha_short=sha[:7])

    def recent_branch_runs(self, ctx: RepoContext, *, limit: int = 5) -> list[dict[str, Any]]:
        out = self.gh(
            "run",
            "list",
            "--repo",
            self.full_repo(ctx),
            "--branch",
            ctx.branch,
            "--limit",
            str(limit),
            "--json",
            "databaseId,displayTitle,workflowName,headSha,status,conclusion,event,createdAt,url",
        )
        if not out:
            return []
        return json.loads(out)

    def list_workflows(self, ctx: RepoContext) -> list[dict[str, Any]]:
        out = self.gh(
            "workflow",
            "list",
            "--repo",
            self.full_repo(ctx),
            "--json",
            "name,path,state",
        )
        if not out:
            return []
        return json.loads(out)

    def ci_check(self, key: str) -> None:
        ctx = self.ctx_for(key)
        build = ctx.cfg.get("build") or {}
        if not build:
            die(f"{key} has no build config")
        workflow = build.get("workflow", "ci.yml")
        sha = self.latest_sha(ctx)
        assets = build.get("release_assets") or []
        ci_sha = self.resolve_ci_sha(ctx, sha, workflow)

        print(f"{key} ({self.full_repo(ctx)} @ {ctx.branch})")
        print(f"  head: {sha} ({sha[:7]})")
        if ci_sha != sha:
            print(f"  ci sha (resolved): {ci_sha} ({ci_sha[:7]})")
        print(f"  configured workflow: {workflow}")
        print(f"  trigger_ci: {self.build_trigger_mode(build)}")
        print(f"  expected release tag: {self.release_tag_for(build, ci_sha)}")

        try:
            workflows = self.list_workflows(ctx)
            print(f"  workflows on default branch ({len(workflows)}):")
            for wf in workflows:
                print(f"    - {wf.get('path')} ({wf.get('name')}, {wf.get('state')})")
        except RuntimeError as exc:
            print(f"  workflows: error ({exc})")

        for label, wf in ((workflow, workflow), ("any", None)):
            try:
                runs = self.workflow_runs_for_commit(ctx, ci_sha, wf)
                print(f"  runs for {ci_sha[:7]} ({label}): {len(runs)}")
                for run_info in runs:
                    print(
                        f"    - {run_info.get('databaseId')} "
                        f"{run_info.get('status')}/{run_info.get('conclusion')} "
                        f"{run_info.get('url', '')}"
                    )
            except RuntimeError as exc:
                print(f"  runs for {ci_sha[:7]} ({label}): error ({exc})")

        if sha != ci_sha:
            print(f"  (HEAD {sha[:7]} has no direct CI run — deploy uses {ci_sha[:7]})")

        try:
            recent = self.recent_branch_runs(ctx)
            print(f"  recent runs on {ctx.branch} ({len(recent)}):")
            for run_info in recent:
                head = str(run_info.get("headSha", ""))[:7]
                print(
                    f"    - {head} {run_info.get('workflowName')} "
                    f"({run_info.get('event')}) {run_info.get('status')}/{run_info.get('conclusion')}"
                )
        except RuntimeError as exc:
            print(f"  recent runs: error ({exc})")

        release_tag = self.release_tag_for(build, ci_sha)
        release_ready = False
        if build.get("artifact") == "release":
            release_ready = self.release_ready(ctx, release_tag, assets)
            print(f"  release {release_tag}: {'ready' if release_ready else 'missing'}")
            if assets:
                print(f"    required assets: {assets}")

        ci_has_run = self.has_workflow_run(ctx, ci_sha, workflow)
        print()
        if ci_has_run and (build.get("artifact") != "release" or release_ready):
            print(f"  OK — ded can deploy {release_tag or ci_sha[:7]} at HEAD {sha[:7]}")
        elif not ci_has_run:
            print("  No Actions run for the resolved CI commit. Common causes:")
            print("    - ci.yml uses push.paths filters (empty commits do not match)")
            print("    - build.workflow filename does not match .github/workflows/*")
            print("    - push went to a fork or branch other than main")
            print("  Fixes: add workflow_dispatch + trigger_ci=dispatch, or push a real code change.")
        elif build.get("artifact") == "release" and not release_ready:
            print(f"  CI OK but release {release_tag} is not ready yet — ded will wait.")
        print()

    def build_trigger_mode(self, build: dict[str, Any]) -> str:
        mode = build.get("trigger_ci", "none")
        if build.get("auto_trigger") is True:
            mode = "dispatch"
        if mode not in {"none", "dispatch", "empty_push"}:
            raise RuntimeError(f"unsupported build.trigger_ci: {mode!r}")
        return mode

    def workflow_runs_for_commit(self, ctx: RepoContext, sha: str, workflow: str | None = None) -> list[dict[str, Any]]:
        cmd = [
            "run",
            "list",
            "--repo",
            self.full_repo(ctx),
            "--commit",
            sha,
            "--limit",
            "5",
            "--json",
            "databaseId,status,conclusion,url",
        ]
        if workflow:
            cmd[4:4] = ["--workflow", workflow]
        try:
            out = self.gh(*cmd)
            if out:
                return json.loads(out)
        except RuntimeError as exc:
            msg = str(exc)
            if workflow and ("not found" in msg.lower() or "could not resolve" in msg.lower()):
                return self.workflow_runs_for_commit(ctx, sha, workflow=None)
            if "not found" not in msg.lower():
                raise

        runs: list[dict[str, Any]] = []
        for run_info in self.recent_branch_runs(ctx, limit=50):
            if run_info.get("headSha") != sha:
                continue
            if workflow and not self._run_matches_workflow(run_info, workflow):
                continue
            runs.append(
                {
                    "databaseId": run_info["databaseId"],
                    "status": run_info.get("status"),
                    "conclusion": run_info.get("conclusion"),
                    "url": run_info.get("url"),
                }
            )
        return runs

    def trigger_workflow(self, ctx: RepoContext, workflow: str, ref: str) -> None:
        log(f"{ctx.key}: triggering {workflow} on {ref}")
        if self.dry_run:
            return
        self.gh(
            "workflow",
            "run",
            workflow,
            "--repo",
            self.full_repo(ctx),
            "--ref",
            ref,
            capture=False,
        )
        time.sleep(5)

    def trigger_ci_empty_push(self, ctx: RepoContext) -> str:
        if not ctx.local_path or not (ctx.local_path / ".git").exists():
            raise RuntimeError(f"{ctx.key}: trigger_ci=empty_push requires local_path git checkout")
        path = str(ctx.local_path)
        run(["git", "-C", path, "fetch", "origin", ctx.branch], capture=True)
        run(["git", "-C", path, "checkout", ctx.branch], capture=True)
        run(["git", "-C", path, "reset", "--hard", f"origin/{ctx.branch}"], capture=True)
        run(
            [
                "git",
                "-C",
                path,
                "-c",
                "user.email=ded@local",
                "-c",
                "user.name=ded",
                "commit",
                "--allow-empty",
                "-m",
                "ded: trigger CI",
            ],
            capture=True,
        )
        run(["git", "-C", path, "push", "origin", ctx.branch], capture=False)
        return run(["git", "-C", path, "rev-parse", "HEAD"], capture=True).stdout.strip()

    def _dispatch_unavailable(self, exc: RuntimeError) -> bool:
        msg = str(exc)
        return "workflow_dispatch" in msg or "Workflow does not have" in msg

    def wait_for_workflow(self, ctx: RepoContext, sha: str, workflow: str, timeout_sec: int = 7200) -> tuple[str, float]:
        build = ctx.cfg.get("build") or {}
        trigger_mode = self.build_trigger_mode(build)
        deadline = time.time() + timeout_sec
        trigger_attempted = False
        logged_waiting = False
        poll_sec = 30
        start = time.perf_counter()

        while time.time() < deadline:
            runs = self.workflow_runs_for_commit(ctx, sha, workflow)
            if not runs:
                runs = self.workflow_runs_for_commit(ctx, sha, workflow=None)

            if not runs:
                if not trigger_attempted and trigger_mode != "none":
                    trigger_attempted = True
                    try:
                        if trigger_mode == "dispatch":
                            self.trigger_workflow(ctx, workflow, ctx.branch)
                        elif trigger_mode == "empty_push":
                            new_sha = self.trigger_ci_empty_push(ctx)
                            if new_sha != sha:
                                log(f"{ctx.key}: empty push advanced main to {new_sha[:7]}")
                                sha = new_sha
                    except RuntimeError as exc:
                        if trigger_mode == "dispatch" and self._dispatch_unavailable(exc):
                            log(
                                f"{ctx.key}: {workflow} has no workflow_dispatch — "
                                "waiting for push-triggered CI "
                                "(add workflow_dispatch to the workflow, or set trigger_ci=empty_push)"
                            )
                        else:
                            raise
                elif not logged_waiting:
                    hint = (
                        "set build.trigger_ci=dispatch (needs workflow_dispatch) or empty_push"
                        if trigger_mode == "none"
                        else "CI may still be starting"
                    )
                    log(f"{ctx.key}: no workflow run yet for {sha[:7]} — waiting ({hint})")
                    try:
                        recent = self.recent_branch_runs(ctx, limit=3)
                        if recent:
                            latest = recent[0]
                            head = str(latest.get("headSha", ""))[:7]
                            log(
                                f"{ctx.key}: latest {ctx.branch} run is {head} "
                                f"({latest.get('workflowName')}, {latest.get('event')}) — "
                                f"run ./scripts/ded ci-check {ctx.key} for details"
                            )
                        else:
                            log(
                                f"{ctx.key}: no recent Actions runs on {ctx.branch} — "
                                f"CI may not be firing (path filters? wrong workflow name?). "
                                f"Run: ./scripts/ded ci-check {ctx.key}"
                            )
                    except RuntimeError:
                        pass
                    logged_waiting = True
                time.sleep(poll_sec)
                continue

            logged_waiting = False
            wf_run = runs[0]
            status = wf_run.get("status")
            conclusion = wf_run.get("conclusion")
            run_id = str(wf_run["databaseId"])
            log(f"{ctx.key}: workflow {run_id} status={status} conclusion={conclusion}")
            if status == "completed":
                if conclusion == "success":
                    duration = time.perf_counter() - start
                    log(f"{ctx.key}: CI workflow finished in {format_duration(duration)}", repo=ctx.key)
                    return sha, duration
                if conclusion in {"failure", "cancelled", "timed_out", "action_required"}:
                    raise RuntimeError(f"workflow {run_id} ended with {conclusion}: {wf_run.get('url')}")
                duration = time.perf_counter() - start
                log(f"{ctx.key}: CI workflow finished in {format_duration(duration)}", repo=ctx.key)
                return sha, duration
            if self.dry_run:
                duration = time.perf_counter() - start
                return sha, duration
            self.gh("run", "watch", run_id, "--repo", self.full_repo(ctx), "--exit-status", capture=False)
            duration = time.perf_counter() - start
            log(f"{ctx.key}: CI workflow finished in {format_duration(duration)}", repo=ctx.key)
            return sha, duration

        raise TimeoutError(
            f"workflow did not finish within {timeout_sec}s for {ctx.key} "
            f"(commit {sha[:7]}). Push to {ctx.branch}, run CI manually, "
            "add workflow_dispatch to the workflow, or set build.trigger_ci=empty_push."
        )

    def release_ready(self, ctx: RepoContext, tag: str, assets: list[str]) -> bool:
        try:
            out = self.gh(
                "release",
                "view",
                tag,
                "--repo",
                self.full_repo(ctx),
                "--json",
                "tagName,assets",
            )
        except RuntimeError:
            return False
        if not out:
            return False
        data = json.loads(out)
        names = {a["name"] for a in data.get("assets", [])}
        if not assets:
            return True
        return all(name in names for name in assets)

    def wait_for_release(self, ctx: RepoContext, tag: str, assets: list[str], timeout_sec: int = 7200) -> float:
        deadline = time.time() + timeout_sec
        start = time.perf_counter()
        while time.time() < deadline:
            if self.release_ready(ctx, tag, assets):
                duration = time.perf_counter() - start
                log(
                    f"{ctx.key}: release {tag} ready after {format_duration(duration)}",
                    repo=ctx.key,
                )
                return duration
            log(f"{ctx.key}: waiting for release {tag} assets={assets or '(any)'}")
            time.sleep(30)
        raise TimeoutError(f"release {tag} not ready within {timeout_sec}s")

    def trigger_downstream(self, ctx: RepoContext) -> None:
        for spec in ctx.cfg.get("trigger_after_build") or []:
            target_key = spec["repo_key"]
            target = self.ctx_for(target_key)
            workflow = spec.get("workflow", "ci.yml")
            ref = spec.get("branch", target.branch)
            log(f"{ctx.key}: triggering downstream {target_key} {workflow} on {ref}")
            if self.dry_run:
                continue
            try:
                self.gh(
                    "workflow",
                    "run",
                    workflow,
                    "--repo",
                    self.full_repo(target),
                    "--ref",
                    ref,
                    capture=False,
                )
            except RuntimeError as exc:
                if self._dispatch_unavailable(exc):
                    log(
                        f"{ctx.key}: downstream {target_key} has no workflow_dispatch — "
                        "push to main or add workflow_dispatch to that workflow"
                    )
                else:
                    raise

    def ansible_deploy(
        self,
        deploy_cfg: dict[str, Any],
        *,
        release_tag: str | None = None,
    ) -> dict[str, Any]:
        script = self.config.stack_root / "scripts" / "ansible-deploy.sh"
        if not script.exists():
            raise FileNotFoundError(script)
        scope = deploy_cfg.get("scope", "apps")
        cmd = [str(script), self.config.network, scope]
        if release_tag and deploy_cfg.get("release_flag"):
            flag = RELEASE_FLAG_TO_CLI[deploy_cfg["release_flag"]]
            cmd.append(f"--{flag}={release_tag}")
        app_tags = deploy_cfg.get("app_tags") or []
        if app_tags:
            cmd.append(f"--app-tags={','.join(app_tags)}")
        log(f"deploy: {' '.join(cmd)}")
        if self.dry_run:
            return {}
        collector = AnsibleTimingCollector()

        def on_ansible_line(line: str) -> None:
            collector.feed(line)
            log(line, repo=_current_repo.get())

        duration = run_streaming(
            cmd,
            cwd=self.config.stack_root,
            env=ansible_profile_callbacks_env(),
            on_line=on_ansible_line,
        )
        timings: dict[str, Any] = {
            "ansible_deploy_sec": round(duration, 2),
            **collector.as_dict(),
        }
        log(f"deploy finished in {format_duration(duration)}", repo=_current_repo.get())
        slowest = timings.get("ansible_tasks", [])
        if slowest:
            top = slowest[0]
            log(
                f"slowest ansible task: {top['task']} ({top['duration_sec']:.1f}s)",
                repo=_current_repo.get(),
            )
        return timings

    def process_repo(self, key: str) -> str | None:
        ctx = self.ctx_for(key)
        st = self.state.repo(key)
        head_sha = self.latest_sha(ctx)
        if not head_sha:
            raise RuntimeError(f"could not resolve HEAD for {key}")

        last_deployed = st.get("last_deployed_sha")
        if last_deployed == head_sha:
            return None

        watch_paths = ctx.cfg.get("watch_paths") or []
        build = ctx.cfg.get("build")
        deploy_cfg = ctx.cfg.get("deploy") or {}

        if watch_paths and not self.paths_changed(ctx, head_sha, watch_paths):
            log(f"{head_sha[:7]} has no changes under {watch_paths} — skip", repo=ctx.key)
            st["last_seen_sha"] = head_sha
            st["last_skipped_sha"] = head_sha
            st["last_skipped_at"] = utc_now()
            self.state.save()
            return "skip"

        log(
            f"new work at {head_sha[:7]} "
            f"(last deployed {str(last_deployed)[:7] if last_deployed else 'none'})",
            repo=ctx.key,
        )
        release_tag: str | None = None
        ci_sha = head_sha
        timings: dict[str, Any] = {"stages": {}}
        run_start = time.perf_counter()

        if build:
            mode = build.get("mode", "github_actions")
            if mode != "github_actions":
                raise RuntimeError(f"unsupported build mode: {mode}")
            workflow = build["workflow"]
            assets = build.get("release_assets") or []
            artifact = build.get("artifact", "release")
            ci_sha = self.resolve_ci_sha(ctx, head_sha, workflow)
            ci_sha, ci_wait_sec = self.wait_for_workflow(ctx, ci_sha, workflow)
            timings["stages"]["ci_wait_sec"] = round(ci_wait_sec, 2)
            release_tag = self.release_tag_for(build, ci_sha)
            if artifact == "release":
                release_wait_sec = self.wait_for_release(ctx, release_tag, assets)
                timings["stages"]["release_wait_sec"] = round(release_wait_sec, 2)
            elif artifact == "ghcr":
                log(f"CI OK — RAM image expected at GHCR tag {release_tag}", repo=ctx.key)
            elif artifact == "none":
                log(f"CI OK — no release artifact to wait for", repo=ctx.key)
            else:
                raise RuntimeError(f"unsupported build artifact type: {artifact}")
            self.trigger_downstream(ctx)

        deploy_mode = deploy_cfg.get("mode", "ansible")
        if deploy_mode == "none":
            log("build-only — skipping Ansible deploy", repo=ctx.key)
        elif deploy_mode == "ansible":
            ansible_timings = self.ansible_deploy(deploy_cfg, release_tag=release_tag)
            timings["stages"].update(ansible_timings)
        else:
            raise RuntimeError(f"unsupported deploy mode: {deploy_mode}")

        total_sec = time.perf_counter() - run_start
        timings["total_sec"] = round(total_sec, 2)
        stage_parts = [
            f"{name}={format_duration(value)}"
            for name, value in timings["stages"].items()
        ]
        log(
            f"timings: total {format_duration(total_sec)}"
            + (f" ({', '.join(stage_parts)})" if stage_parts else ""),
            repo=ctx.key,
        )

        st["last_seen_sha"] = head_sha
        st["last_deployed_sha"] = head_sha
        st["last_deployed_tag"] = release_tag
        if build and release_tag:
            st["last_ci_sha"] = ci_sha
        st["last_deploy_at"] = utc_now()
        st["last_timings"] = timings
        st["last_deploy_duration_sec"] = timings["total_sec"]
        for field in ("ci_wait_sec", "release_wait_sec", "ansible_deploy_sec"):
            if field in timings["stages"]:
                st[f"last_{field}"] = timings["stages"][field]
        st.pop("last_error", None)
        self.state.save()
        log(f"complete at {head_sha[:7]}", repo=ctx.key)
        return "ok"

    def _process_repo_safe(self, key: str) -> None:
        token = _current_repo.set(key)
        if _log_sink is not None:
            _log_sink.set_repo_status(key, "running")
        try:
            outcome = self.process_repo(key)
            if _log_sink is not None:
                if outcome == "skip":
                    _log_sink.set_repo_status(key, "skip")
                elif outcome == "ok":
                    _log_sink.set_repo_status(key, "ok")
                else:
                    _log_sink.set_repo_status(key, "idle")
        except Exception as exc:  # noqa: BLE001 — daemon should continue
            log(f"failed: {exc}", repo=key)
            if _log_sink is not None:
                _log_sink.set_repo_status(key, "error")
            st = self.state.repo(key)
            st["last_error"] = str(exc)
            st["last_error_at"] = utc_now()
            self.state.save()
        finally:
            _current_repo.reset(token)

    def process_all(self, only: list[str] | None = None, *, workers: int = 1) -> None:
        keys = only or list(self.config.repos.keys())
        for key in keys:
            if key not in self.config.repos:
                die(f"unknown repo key: {key}")

        if workers <= 1 or len(keys) <= 1:
            for key in keys:
                self._process_repo_safe(key)
            return

        effective_workers = min(workers, len(keys))
        log(f"processing {len(keys)} repo(s) with {effective_workers} worker(s)")
        with ThreadPoolExecutor(max_workers=effective_workers) as pool:
            futures = [pool.submit(self._process_repo_safe, key) for key in keys]
            for future in as_completed(futures):
                future.result()

    def status(self) -> None:
        for key in self.config.repos:
            st = self.state.repo(key)
            print(f"{key}:")
            for field in (
                "last_seen_sha",
                "last_deployed_sha",
                "last_deployed_tag",
                "last_deploy_at",
                "last_deploy_duration_sec",
                "last_ci_wait_sec",
                "last_release_wait_sec",
                "last_ansible_deploy_sec",
                "last_error",
                "last_error_at",
            ):
                if field in st:
                    val = st[field]
                    if field.endswith("_sha") and isinstance(val, str):
                        val = val[:7]
                    if field.endswith("_sec") and isinstance(val, (int, float)):
                        val = format_duration(float(val))
                    print(f"  {field}: {val}")
            timings = st.get("last_timings")
            if isinstance(timings, dict):
                tasks = timings.get("ansible_tasks") or timings.get("stages", {}).get("ansible_tasks")
                if tasks:
                    print("  slowest ansible tasks:")
                    for task in tasks[:5]:
                        print(f"    - {task['task']}: {task['duration_sec']:.1f}s")
            try:
                sha = self.latest_sha(self.ctx_for(key))
                print(f"  remote_head: {sha[:7]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  remote_head: error ({exc})")
            print()

    def validate_env(
        self,
        *,
        env_loaded: list[Path] | None = None,
        env_missing: list[Path] | None = None,
        env_empty: list[Path] | None = None,
    ) -> None:
        normalize_deploy_env()
        missing = []
        if not env_value("BOT_HOST"):
            missing.append("DEPLOY_SSH_HOST or BOT_HOST")
        if not env_value("ANSIBLE_VAULT_PASSWORD"):
            missing.append("ANSIBLE_VAULT_PASSWORD")
        if not env_value("SSH_PRIVATE_KEY") and not env_value("SSH_PRIVATE_KEY_FILE"):
            missing.append("SSH_PRIVATE_KEY_FILE, DEPLOY_SSH_KEY_FILE, or DEPLOY_SSH_KEY")
        if missing:
            lines = [f"missing deploy env: {', '.join(missing)}"]
            if env_loaded:
                lines.append(f"  loaded env file(s): {', '.join(str(p) for p in env_loaded)}")
            else:
                lines.append("  no env file loaded")
            if env_empty:
                lines.append("  env file(s) exist but contain no assignments:")
                lines.extend(f"    - {p}" for p in env_empty)
            if env_missing:
                lines.append("  looked for:")
                lines.extend(f"    - {p}" for p in env_missing)
                lines.append("  set DED_ENV or pass --env-file (see docs/env.example)")
            elif not env_loaded:
                lines.append(f"  expected: {ded_root_dir() / 'config' / 'ded.env'}")
            lines.append("  run: ./scripts/ded validate --debug")
            die("\n".join(lines))
        require_tool("gh")
        require_tool("ansible-playbook")
        try:
            self.gh("auth", "status")
        except RuntimeError:
            die("gh is not authenticated — run: gh auth login")


def default_config_path(ded_root: Path | None = None) -> Path | None:
    for env_key in ("DED_CONFIG", "DEPLOYD_CONFIG"):
        env = os.environ.get(env_key, "").strip()
        if env:
            return Path(env).resolve()
    here = ded_root or ded_root_dir()
    for candidate in (
        here / "config" / "ded.json",
        here / "config" / "ded.local.json",
        here / "config" / "deployd.json",
        here / "config" / "deployd.local.json",
    ):
        if candidate.exists():
            return candidate.resolve()
    return None


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=None, help="path to ded.json")
    common.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="dotenv file (default: config/ded.env, then .env; also auto-loaded by ded wrapper)",
    )
    common.add_argument("--dry-run", action="store_true", help="log actions without gh/ansible side effects")

    p = argparse.ArgumentParser(description="ded local deploy daemon", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", parents=[common], help="check config, gh auth, and deploy env")
    validate_parser = sub.choices["validate"]
    validate_parser.add_argument("--debug", action="store_true", help="print env resolution details")

    sp = sub.add_parser("status", parents=[common], help="show last deploy state per repo")
    sp.add_argument("--repo", action="append", help="limit to repo key(s)")

    sp = sub.add_parser("once", parents=[common], help="poll all repos once and exit")
    sp.add_argument("--repo", action="append", help="limit to repo key(s)")
    sp.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parallel repo workers (default: workers from config, or 1)",
    )
    sp.add_argument("--ui", action="store_true", help="terminal UI with per-repo log panes")
    sp.add_argument("--plain", action="store_true", help="plain interleaved logs (disable UI)")

    sp = sub.add_parser("deploy", parents=[common], help="deploy a specific release tag (skip CI)")
    sp.add_argument("repo_key", help="configured repo key from ded.json")
    sp.add_argument("tag", help="release tag, e.g. ci-abc1234")

    sp = sub.add_parser("ci-check", parents=[common], help="diagnose CI / workflow detection for a repo")
    sp.add_argument("repo_key", help="configured repo key from ded.json")

    sp = sub.add_parser("start", parents=[common], help="run continuous poll loop")
    sp.add_argument("--repo", action="append", help="limit to repo key(s)")
    sp.add_argument("--interval", type=int, default=None, help="override poll interval seconds")
    sp.add_argument(
        "--workers",
        type=int,
        default=None,
        help="parallel repo workers (default: workers from config, or 1)",
    )
    sp.add_argument("--ui", action="store_true", help="terminal UI with per-repo log panes")
    sp.add_argument("--plain", action="store_true", help="plain interleaved logs (disable UI)")

    return p


def should_use_ui(args: argparse.Namespace) -> bool:
    if getattr(args, "plain", False):
        return False
    if getattr(args, "ui", False):
        return True
    if args.command not in {"once", "start"}:
        return False
    return sys.stdout.isatty() and sys.stdin.isatty()


def run_poll_with_ui(
    daemon: DeployDaemon,
    *,
    repo_keys: list[str],
    workers: int,
    interval: int,
    network: str,
    continuous: bool,
) -> None:
    from ded_cursor import CursorAgentSettings
    from ded_tui import DedLogSink, run_tui

    sink = DedLogSink(repo_keys, timestamp_fn=utc_now)
    set_log_sink(sink)
    stop = threading.Event()
    agent_settings = CursorAgentSettings.from_config(daemon.config.raw)
    launching_agents: set[str] = set()
    launch_lock = threading.Lock()

    sink.set_header(
        f"ded {DED_VERSION}  network={network}  workers={workers}"
        + (f"  interval={interval}s" if continuous else "")
    )
    if os.environ.get(agent_settings.api_key_env, "").strip():
        sink.set_footer("Press a on a project to launch a Cursor agent with its logs")
    else:
        sink.set_footer(f"Set {agent_settings.api_key_env} to enable Cursor agent launch (a)")

    def launch_cursor_agent(repo_key: str) -> None:
        with launch_lock:
            if repo_key in launching_agents:
                sink.set_footer(f"Cursor agent for {repo_key} is already launching…")
                return
            launching_agents.add(repo_key)

        def work() -> None:
            try:
                _launch_cursor_agent(sink, daemon, repo_key, agent_settings)
            finally:
                with launch_lock:
                    launching_agents.discard(repo_key)

        threading.Thread(
            target=work,
            daemon=True,
            name=f"cursor-agent-{repo_key}",
        ).start()

    def worker() -> None:
        try:
            while not stop.is_set():
                daemon.process_all(only=repo_keys, workers=workers)
                if not continuous:
                    sink.set_footer("Done — press q to quit")
                    break
                if stop.wait(interval):
                    break
        except Exception as exc:  # noqa: BLE001 — surface in UI instead of breaking curses
            sink.emit(f"ERROR: poll loop failed: {exc}")
            sink.set_footer("Error — press q to quit")
            stop.set()

    thread = threading.Thread(target=worker, name="ded-poll", daemon=True)
    thread.start()
    try:
        run_tui(sink, stop=stop, on_launch_agent=launch_cursor_agent)
    finally:
        stop.set()
        thread.join(timeout=5)
        set_log_sink(None)


def _launch_cursor_agent(
    sink: DedLogSink,
    daemon: DeployDaemon,
    repo_key: str,
    settings: "CursorAgentSettings",
) -> None:
    from ded_cursor import (
        agent_url_from_response,
        build_agent_prompt,
        create_cloud_agent,
        open_agent_url,
        repo_agent_target,
    )

    api_key = os.environ.get(settings.api_key_env, "").strip()
    if not api_key:
        sink.emit(
            f"Cursor agent: set {settings.api_key_env} in ded.env "
            "(Cursor Dashboard → API Keys)",
        )
        sink.set_footer(f"Missing {settings.api_key_env}")
        return

    repo_cfg = daemon.config.repos.get(repo_key)
    if repo_cfg is None:
        sink.emit(f"Cursor agent: unknown repo key {repo_key}")
        return

    target = repo_agent_target(
        repo_key,
        github_owner=daemon.config.github_owner,
        repo_cfg=repo_cfg,
    )
    prompt = build_agent_prompt(
        target=target,
        network=daemon.config.network,
        logs=sink.log_text_for_repo(repo_key),
        state=dict(daemon.state.repo(repo_key)),
        max_chars=settings.max_log_chars,
    )

    sink.set_footer(f"Creating Cursor agent for {repo_key}…")
    try:
        result = create_cloud_agent(
            api_key,
            prompt_text=prompt,
            target=target,
            model=settings.model,
        )
        url = agent_url_from_response(result)
        opened = open_agent_url(url)
        sink.emit(f"Cursor agent created: {url}", repo=repo_key)
        sink.set_footer("Agent opened in browser" if opened else f"Agent ready: {url}")
    except Exception as exc:  # noqa: BLE001 — show in UI
        sink.emit(f"Cursor agent failed: {exc}", repo=repo_key)
        sink.set_footer(f"Agent failed: {exc}")


def main() -> None:
    # Internal: bash wrapper loads env via python (safe quoting for # and spaces in values).
    if len(sys.argv) >= 2 and sys.argv[1] == "shell-export":
        path = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else None
        if path is None or not path.is_file():
            die("shell-export requires path to env file")
        shell_export(path.resolve())
        return

    parser = build_parser()
    args = parser.parse_args()
    root = ded_root_dir()
    if args.config is not None:
        config_path = args.config.resolve()
    else:
        resolved = default_config_path(root)
        if resolved is None:
            die(
                "no config file: pass --config /path/to/your-project/ded.json\n"
                "  or set DED_CONFIG\n"
                "  Schema: docs/config.md (in the ded install)"
            )
        config_path = resolved
    if not config_path.exists():
        die(f"config not found: {config_path}")
    try:
        config = DeployConfig.load(config_path)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — surface config bugs without traceback
        die(f"failed to load config {config_path}: {exc}")

    loaded_env: list[Path] = []
    missing_env: list[Path] = []
    empty_env: list[Path] = []
    for env_root in (root, config.stack_root):
        part_loaded, part_missing, part_empty = load_deploy_env(
            env_root, env_file=args.env_file, config_path=config_path
        )
        for path in part_loaded:
            if path not in loaded_env:
                loaded_env.append(path)
        missing_env = part_missing
        empty_env = part_empty
    if loaded_env:
        log(f"loaded env: {', '.join(str(p) for p in loaded_env)}")
    elif (missing_env or empty_env) and args.command == "validate":
        if missing_env:
            log(f"no env file found; looked for: {', '.join(str(p) for p in missing_env)}")
        if empty_env:
            log(f"env file(s) with no assignments: {', '.join(str(p) for p in empty_env)}")
    normalize_deploy_env()

    daemon = DeployDaemon(config, dry_run=args.dry_run)

    if args.command == "validate":
        if getattr(args, "debug", False):
            for line in env_debug_lines(env_loaded=loaded_env, env_missing=missing_env, env_empty=empty_env):
                log(line)
        daemon.validate_env(env_loaded=loaded_env, env_missing=missing_env, env_empty=empty_env)
        log(f"config OK: {config_path}")
        log(f"repos: {', '.join(config.repos.keys())}")
        return

    if args.command == "status":
        daemon.status()
        return

    if args.command == "ci-check":
        daemon.validate_env(env_loaded=loaded_env, env_missing=missing_env, env_empty=empty_env)
        daemon.ci_check(args.repo_key)
        return

    daemon.validate_env(env_loaded=loaded_env, env_missing=missing_env, env_empty=empty_env)

    if args.command == "deploy":
        ctx = daemon.ctx_for(args.repo_key)
        deploy_cfg = ctx.cfg.get("deploy") or {}
        if deploy_cfg.get("mode") == "none":
            die(f"{args.repo_key} is build-only (deploy.mode=none)")
        ansible_timings = daemon.ansible_deploy(deploy_cfg, release_tag=args.tag)
        st = daemon.state.repo(args.repo_key)
        st["last_deployed_tag"] = args.tag
        st["last_deploy_at"] = utc_now()
        if ansible_timings:
            st["last_timings"] = {"stages": ansible_timings, "total_sec": ansible_timings.get("ansible_deploy_sec")}
            st["last_deploy_duration_sec"] = ansible_timings.get("ansible_deploy_sec")
            if "ansible_deploy_sec" in ansible_timings:
                st["last_ansible_deploy_sec"] = ansible_timings["ansible_deploy_sec"]
        daemon.state.save()
        return

    if args.command == "once":
        workers = args.workers if args.workers is not None else config.workers
        if workers < 1:
            die(f"--workers must be >= 1, got {workers}")
        repo_keys = args.repo or list(config.repos.keys())
        if should_use_ui(args):
            run_poll_with_ui(
                daemon,
                repo_keys=repo_keys,
                workers=workers,
                interval=0,
                network=config.network,
                continuous=False,
            )
            return
        daemon.process_all(only=args.repo, workers=workers)
        return

    if args.command == "start":
        interval = args.interval or config.poll_interval_sec
        workers = args.workers if args.workers is not None else config.workers
        if workers < 1:
            die(f"--workers must be >= 1, got {workers}")
        repo_keys = args.repo or list(config.repos.keys())
        if should_use_ui(args):
            run_poll_with_ui(
                daemon,
                repo_keys=repo_keys,
                workers=workers,
                interval=interval,
                network=config.network,
                continuous=True,
            )
            return
        log(f"starting ded (interval={interval}s, network={config.network}, workers={workers})")
        while True:
            daemon.process_all(only=args.repo, workers=workers)
            time.sleep(interval)
        return

    die(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
