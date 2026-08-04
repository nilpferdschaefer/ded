"""Create Cursor cloud agents from ded TUI logs."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any

CURSOR_API_URL = "https://api.cursor.com/v1/agents"
DEFAULT_API_KEY_ENV = "CURSOR_API_KEY"
DEFAULT_MAX_LOG_CHARS = 48_000


@dataclass
class CursorAgentSettings:
    api_key_env: str = DEFAULT_API_KEY_ENV
    model: str | None = None
    max_log_chars: int = DEFAULT_MAX_LOG_CHARS

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "CursorAgentSettings":
        cursor = raw.get("cursor") or {}
        if not isinstance(cursor, dict):
            return cls()
        model = cursor.get("model")
        return cls(
            api_key_env=str(cursor.get("api_key_env") or DEFAULT_API_KEY_ENV),
            model=str(model) if model else None,
            max_log_chars=int(cursor.get("max_log_chars", DEFAULT_MAX_LOG_CHARS)),
        )


@dataclass
class RepoAgentTarget:
    key: str
    repo_url: str
    branch: str
    full_name: str


def github_repo_url(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}"


def repo_agent_target(
    key: str,
    *,
    github_owner: str,
    repo_cfg: dict[str, Any],
) -> RepoAgentTarget:
    repo_name = str(repo_cfg.get("repo", key))
    branch = str(repo_cfg.get("branch", "main"))
    override = repo_cfg.get("cursor_repo_url") or repo_cfg.get("repo_url")
    repo_url = str(override) if override else github_repo_url(github_owner, repo_name)
    return RepoAgentTarget(
        key=key,
        repo_url=repo_url,
        branch=branch,
        full_name=f"{github_owner}/{repo_name}",
    )


def build_agent_prompt(
    *,
    target: RepoAgentTarget,
    network: str,
    logs: str,
    state: dict[str, Any],
    max_chars: int,
) -> str:
    lines = [
        f"Investigate a deploy issue for `{target.key}` ({target.full_name}, branch `{target.branch}`).",
        f"Network/environment: {network}",
        "",
    ]
    state_lines = []
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
        if field in state:
            state_lines.append(f"- {field}: {state[field]}")
    if state_lines:
        lines.append("Latest ded state:")
        lines.extend(state_lines)
        lines.append("")

    timings = state.get("last_timings")
    if isinstance(timings, dict):
        stages = timings.get("stages") or {}
        timing_bits = []
        if "total_sec" in timings:
            timing_bits.append(f"total={timings['total_sec']:.1f}s")
        for key in ("ci_wait_sec", "release_wait_sec", "ansible_deploy_sec"):
            if key in stages:
                timing_bits.append(f"{key}={stages[key]:.1f}s")
        if timing_bits:
            lines.append("Last deploy timings: " + ", ".join(timing_bits))
        tasks = stages.get("ansible_tasks") or []
        if tasks:
            lines.append("Slowest ansible tasks:")
            for task in tasks[:5]:
                lines.append(f"- {task['task']}: {task['duration_sec']:.1f}s")
        if timing_bits or tasks:
            lines.append("")

    lines.extend(
        [
            "Below are logs captured by the ded deploy daemon for this service.",
            "Diagnose the failure, identify root cause, and propose concrete fixes.",
            "",
            "--- ded logs ---",
        ]
    )

    header = "\n".join(lines)
    budget = max(1000, max_chars - len(header) - 20)
    body = logs.strip() or "(no log lines captured yet)"
    if len(body) > budget:
        body = f"...[truncated {len(body) - budget} chars]\n" + body[-budget:]
    prompt = header + body
    return prompt[:max_chars]


def _auth_header(api_key: str) -> str:
    token = api_key.strip()
    if token.lower().startswith("bearer "):
        return token
    encoded = base64.b64encode(f"{token}:".encode()).decode("ascii")
    return f"Basic {encoded}"


def create_cloud_agent(
    api_key: str,
    *,
    prompt_text: str,
    target: RepoAgentTarget,
    model: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "repos": [{"url": target.repo_url, "startingRef": target.branch}],
        "name": (name or f"ded: {target.key}")[:100],
    }
    if model:
        payload["model"] = {"id": model}

    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        CURSOR_API_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": _auth_header(api_key),
            "User-Agent": "ded-deploy-daemon",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cursor API HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cursor API request failed: {exc.reason}") from exc


def agent_url_from_response(data: dict[str, Any]) -> str:
    agent = data.get("agent") or {}
    url = agent.get("url")
    if url:
        return str(url)
    agent_id = agent.get("id")
    if agent_id:
        return f"https://cursor.com/agents/{agent_id}"
    raise RuntimeError(f"Cursor API response missing agent url: {data!r}")


def open_agent_url(url: str) -> bool:
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        pass
    for cmd in (["xdg-open", url], ["open", url]):
        try:
            subprocess.run(cmd, check=False, capture=True)
            return True
        except OSError:
            continue
    return False
