# Contributing

## Setup

```bash
uv sync                              # Python 3.12 + dev tools from uv.lock
pre-commit install --install-hooks   # gitleaks, ruff, commit-message check
```

## Branching model — GitHub Flow

- `main` is always releasable and **protected**: no direct pushes, no force
  pushes, linear history, all CI checks must pass.
- All work happens on a short-lived branch from `main`, merged via a pull
  request with **squash merge**. Branches are deleted after merge.

| Prefix       | Use for                                  | Example                          |
|--------------|------------------------------------------|----------------------------------|
| `feat/`      | new capability                           | `feat/jd-requirement-parser`     |
| `fix/`       | bug fix                                  | `fix/thai-date-parsing`          |
| `security/`  | hardening, threat-model work             | `security/egress-network-policy` |
| `refactor/`  | restructuring without behaviour change   | `refactor/match-engine`          |
| `test/`      | tests / eval only                        | `test/redaction-pii-cases`       |
| `docs/`      | documentation                            | `docs/k8s-setup`                 |
| `ci/`        | workflows, automation                    | `ci/trivy-image-scan`            |
| `build/`     | dependencies, packaging, images          | `build/bump-anthropic-sdk`       |
| `chore/`     | maintenance                              | `chore/repo-foundation`          |

Branch names: lowercase, kebab-case, optionally with an issue number
(`feat/42-jd-parser`).

## Commit messages — Conventional Commits

```
<type>(<optional scope>): <lowercase summary>

<optional body: what and why>
```

Types: `feat`, `fix`, `security`, `refactor`, `perf`, `test`, `docs`, `ci`,
`build`, `chore`, `style`, `revert`. Scopes match components: `core`, `gateway`,
`worker`, `api`, `ui`, `embedder`, `k8s`, `eval`.

The commit-msg hook enforces this locally; the PR title check enforces it on
the squash commit.

## Pull requests

1. Keep PRs small and focused on one change.
2. Fill in the PR template, including the security checklist.
3. CI must be green: lint, type-check, tests (≥ 90 % coverage), secret scan,
   dependency audit.

## Releases

Versions follow [SemVer](https://semver.org). Releases are annotated tags on
`main` (`v0.1.0`) with notes generated from the conventional commit history.
