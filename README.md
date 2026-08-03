# ded

Local deploy daemon: watch GitHub repos → wait for CI → wait for published
artifacts → run your project's deploy command (e.g. Ansible).

`ded` is **context-agnostic**. It does not ship a project config — you pass
`--config /path/to/ded.json` from **your** deploy project (inventory, app repos,
credentials layout).

## Quick start

```bash
git clone git@github.com:nilpferdschaefer/ded.git
cd ded
chmod +x ded

# Config lives in YOUR project, not in the ded repo:
export DED_CONFIG=/path/to/your-project/config/ded.json
export DED_ENV=/path/to/your-project/config/ded.env   # optional

gh auth login
./ded --config "$DED_CONFIG" validate
./ded --config "$DED_CONFIG" once
./ded --config "$DED_CONFIG" start
```

`DED_CONFIG` (or `--config`) is **required** if no config file exists beside the
ded install.

## CLI

```bash
ded --config /path/to/ded.json validate
ded --config /path/to/ded.json status
ded --config /path/to/ded.json ci-check my-app
ded --config /path/to/ded.json once
ded --config /path/to/ded.json start
ded --config /path/to/ded.json deploy my-app ci-abc1234
```

Options:

- `--config` — path to your project's `ded.json` (or `DED_CONFIG`)
- `--env-file` — deploy credentials dotenv (or `DED_ENV`)
- `--dry-run` — log without calling `gh` / deploy
- `--repo` — limit `once` / `start` to specific repo keys
- `--workers` — process multiple repos in parallel (`once` / `start`; default from config)

## Configuration schema

See **[docs/config.md](docs/config.md)** for field reference and a minimal generic
example. Copy that into **your** project as `config/ded.json` (gitignored there).

Deploy credentials (`DEPLOY_SSH_HOST`, vault password, SSH key) live in your
project's env file — see **[docs/env.example](docs/env.example)**.

## CI workflow requirements

**[docs/ci-workflows.md](docs/ci-workflows.md)** — mandatory release/GHCR publish
rules so `ded` does not block when CI succeeds without artifacts.

## Prerequisites

- `gh` authenticated
- `ansible-playbook` on PATH when using `deploy.mode: ansible`
- SSH access to deploy host
- App repos: CI on push + artifact publish per ci-workflows doc

## What `ded` does not do

- Build application binaries (CI does that)
- Ship project-specific `ded.json` (your project owns that)
- Clone sibling repos (expects `local_path` checkouts when configured)
