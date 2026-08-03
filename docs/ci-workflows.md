# CI workflow requirements for `ded`

`ded` polls GitHub for new commits, waits for CI to finish, then waits for
published artifacts before it deploys. **A green workflow run does not mean
`ded` can proceed** unless the workflow also published the artifact your
`ded.json` (`release_assets`, GHCR tag, etc.) expects.

If CI succeeds but no release/image exists, `ded` logs:

```text
waiting for release ci-abc1234 assets=(...)
```

and polls every 30 seconds for up to **2 hours** before failing.

## Contract (all artifact repos)

Every repo that `ded` deploys must satisfy:

| Requirement | Why |
|-------------|-----|
| Workflow file matches `build.workflow` in your `ded.json` (usually `ci.yml`) | `ded` watches that workflow |
| Runs on **push to your configured branch** | Default `trigger_ci: none` waits for push CI |
| **`conclusion: success` only when artifacts are published** | `ded` treats workflow success as “build done” |
| **Publish step is not optional** | No `continue-on-error` on release/GHCR steps |
| **Publish step runs in the same workflow** | Or configure `build.workflow` to the release workflow |
| Tag matches `build.release_tag` | Default: `ci-{sha_short}` → `ci-0cb263b` |
| Assets match `build.release_assets` (when set) | `ded` waits until every listed asset exists |

Recommended: add `workflow_dispatch:` and set `trigger_ci: dispatch` for catch-up.

## Artifact types

| `build.artifact` | What CI must publish | `ded` waits for |
|------------------|----------------------|-----------------|
| `release` | GitHub Release at configured tag | `gh release view` + optional asset names |
| `ghcr` | Image tag `ci-{sha_short}` (typical) | CI success only; pull verified at deploy |
| `none` | Nothing | CI success only |

## Mandatory release pattern (`artifact: release`)

Final job depends on build/test and creates the GitHub Release. Workflow must
**fail** if release creation fails.

```yaml
name: ci

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: ./scripts/build.sh
      - uses: actions/upload-artifact@v4
        with:
          name: dist
          path: dist/

  release:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: dist
          path: dist/
      - name: Publish GitHub Release (required for ded)
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          TAG="ci-${GITHUB_SHA::7}"
          gh release create "$TAG" dist/* --title "$TAG"
```

Rules:

1. Tag must match `release_tag` in your project's `ded.json`.
2. Asset filenames must match `release_assets` in `ded.json` and your deploy role.
3. Publish `.sha256` sidecars when your deploy verifies checksums.

## GHCR pattern (`artifact: ghcr`)

Push an image tag matching your `release_tag` template, e.g.:

```text
ghcr.io/<owner>/<repo>:ci-<7-char-sha>
```

```yaml
      - run: |
          TAG="ci-${GITHUB_SHA::7}"
          docker tag myimage:latest "ghcr.io/${{ github.repository }}:${TAG}"
          docker push "ghcr.io/${{ github.repository }}:${TAG}"
```

`ded` does not poll GHCR; a missing image fails at deploy time.

## Failure modes

| Symptom | Cause | Fix |
|---------|-------|-----|
| `waiting for release ci-…` | Release job missing or slow | Fix CI; `gh release view ci-…` |
| `release … not ready within 7200s` | Release never published | Fix CI; re-run workflow |
| Deploy fails on missing asset | Name mismatch | Align CI uploads with `release_assets` |
| `no workflow run yet` | Path filters / wrong branch | Push change, `workflow_dispatch`, or `trigger_ci` |

## Verify

```bash
ded --config /path/to/ded.json ci-check <repo-key>
gh release view ci-<sha> --repo <owner>/<repo>
```

## Wait timings

| Stage | Timeout | Poll |
|-------|---------|------|
| CI workflow | 7200s | 30s |
| GitHub Release | 7200s | 30s |

Not configurable in `ded.json` today.
