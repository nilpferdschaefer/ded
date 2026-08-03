# ded.json configuration

`ded.json` belongs to **your deploy project** (the repo that owns Ansible/Terraform,
inventory, and deploy scripts). The `ded` tool does not ship or default to a
project config.

Pass it explicitly:

```bash
ded --config /path/to/your-project/config/ded.json …
# or: export DED_CONFIG=/path/to/your-project/config/ded.json
```

## Top-level fields

| Field | Purpose |
|-------|---------|
| `github_owner` | GitHub org/user for all `repos.*.repo` |
| `poll_interval_sec` | Loop interval for `start` (default 90) |
| `workers` | Parallel repo workers for `once` / `start` (default 1; override with `--workers`) |
| `network` | Opaque string passed to your deploy script (e.g. `testnet`) |
| `stack_root` | Root of your deploy project (relative to config file; default `..`) |
| `state_dir` | Persisted state directory (relative to `stack_root`) |
| `repos` | Map of repo keys → build/deploy definitions |

## Per-repo (`repos.<key>`)

| Field | Purpose |
|-------|---------|
| `repo` | GitHub repository name (under `github_owner`) |
| `branch` | Branch to watch (default `main`) |
| `local_path` | Optional local checkout for `git fetch` / path filters |
| `watch_paths` | Deploy only when these path prefixes change |
| `build.workflow` | Actions workflow file (e.g. `ci.yml`) |
| `build.artifact` | `release`, `ghcr`, or `none` |
| `build.release_tag` | Tag template; `{sha}`, `{sha_short}` |
| `build.release_assets` | Asset filenames required on the release before deploy |
| `build.trigger_ci` | `none`, `dispatch`, or `empty_push` |
| `deploy.mode` | `ansible` or `none` |
| `deploy.scope` | Passed to deploy script when using Ansible mode |
| `deploy.app_tags` | Tag filter list for Ansible |
| `deploy.release_flag` | Maps to `--<flag>=<tag>` on deploy script |
| `trigger_after_build` | Downstream `gh workflow run` after CI |

## Minimal example

Save as **your project's** `config/ded.json` (not inside the ded repo):

```json
{
  "github_owner": "your-org",
  "poll_interval_sec": 90,
  "network": "staging",
  "stack_root": "..",
  "state_dir": ".ded",
  "repos": {
    "my-app": {
      "repo": "my-app",
      "branch": "main",
      "local_path": "../my-app",
      "build": {
        "mode": "github_actions",
        "workflow": "ci.yml",
        "trigger_ci": "none",
        "artifact": "release",
        "release_tag": "ci-{sha_short}",
        "release_assets": ["my-binary", "my-binary.sha256"]
      },
      "deploy": {
        "mode": "ansible",
        "scope": "apps",
        "app_tags": ["my-app"],
        "release_flag": "my_app_release"
      }
    }
  }
}
```

Ansible deploy mode expects `stack_root/scripts/ansible-deploy.sh` and maps
`release_flag` values to CLI flags (see `RELEASE_FLAG_TO_CLI` in `ded.py`).

## Env file

Credentials are **not** in `ded.json`. Use a dotenv file in your project and
`--env-file` or `DED_ENV`. See [env.example](env.example).
