# Agent guide — code-vitals

This file holds the **code-vitals-specific** working rules. The machine-global
rules common to every repo — Kanban/bored workflow, iteration & versioning
defaults, branching & git safety, CI-after-push, PR self-review + comment loop,
test coverage, and MCP/secrets discipline — live in the shared **agent-shared
baseline**, imported on this machine via `~/.codex/AGENTS.md` and
`~/.claude/CLAUDE.md` (run `agent-shared/install.sh` once per machine to wire
this up). **Read that baseline first;** this file only records what is specific
to code-vitals or overrides the baseline.

If you change a code-vitals rule, change it here — there is no parallel copy.
Cross-repo rules change in `agent-shared`, not here.

---

## Repository context

| Item | Value |
| --- | --- |
| **Remote / `gh` repo** | `vcheesbrough/code-vitals` |
| **Trunk** | `main` |
| **Kanban board** | `https://bored.desync.link/boards/code-vitals` |
| **Phase** | pre-MVP — iteration semver is `0.N.P` (baseline §2), version in `pyproject.toml` once it exists |

**What it is:** a collector (Python + lizard + tokei, shipped as a container
image in zot) that records measures of each project's code at every trunk
commit into Postgres, explored through Apache Superset. The plan lives on the
bored cards until a `docs/PLAN.md` exists.

Shared skills apply here once the machine is wired up (`start-iteration`,
`ci-watch`, `pr-review-loop`). Their per-repo parameters:

- `OWNER=vcheesbrough`, `REPO=code-vitals`
- CI reproduce commands (from the repo root):
  - lint, types, tests: `uv sync && uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest`
  - script tests: `for t in scripts/test-*.sh; do sh "$t"; done`
  - the image, exactly as CI builds it (runs every test above inside the build):
    `. ./tool-versions.env && docker build --build-arg TOKEI_VERSION="$TOKEI_VERSION" --build-arg RELEASE_TAG=0.1.999 --build-arg GIT_REVISION=local --label org.opencontainers.image.version=0.1.999 --label org.opencontainers.image.revision=local --label org.opencontainers.image.created=now -t code-vitals-collector:local .`
    then `scripts/check-image-metadata.sh code-vitals-collector:local 0.1.999 local` and
    `scripts/check-image-tools.sh code-vitals-collector:local 0.1.999 local`
- Extra review criteria beyond the baseline five (correctness, security/OWASP,
  tests, versioning, scope):
  - **Backfillability** — every measure must be computable from git alone at
    any historical commit, or be flagged non-recomputable.
  - **Measure versioning** — any change that alters a measure's values (its
    logic, its area rules, or a pinned tool such as lizard or tokei) bumps that
    measure's version.

---

## Repo-specific rules

### Bootstrap exception

This repository was initialized directly on `main`. The initial commit that
creates this file and establishes the default branch is allowed on `main`.
After initialization, follow the baseline feature-branch workflow (§2–§3).

### Versioning and releases (overrides baseline §2's patch rule)

The release flow is **bored's**, ported (its `.woodpecker/build.yml` and
`scripts/`, card #461 there); see README § Versioning. In short:

- **Minor `N` = iteration number**; `pyproject.toml` holds `0.N.0` and stays
  there for the whole iteration. **The patch is not bumped by hand** — CI sets
  it to the Woodpecker number of the push pipeline that built the image
  (`scripts/compute-release-tag.sh`), so versions are sparse (`0.1.12`,
  `0.1.15`, …).
- The same string is the image tag, the git tag and the version baked into the
  image (`/etc/code-vitals/build-info.json`).
- Every push, on any branch, publishes `:<version>` and `:commit-<sha>` once
  the in-build tests pass; republishing an existing version is refused. **A
  push never pushes a git tag**: the tag is pushed only after a successful
  deployment, so it means "deployed".
- Replacing these copied scripts with shared tooling is card #503.

### Planning vs implementation (scope gate)

Cards in **TODO / backlog** are **spec only** until moved to **In progress**.
Do **not** treat design discussion as permission to edit the repo.

| User intent | Allowed edits | Not allowed |
| --- | --- | --- |
| Refine a **TODO** card (acceptance, order, scope) | bored MCP `update_card` | Source code, CI, scripts, compose, new files, commits |
| **Start iteration** — *pick up #N*, *implement #N* | Run the **`start-iteration`** skill, then implement per the active card | Scope beyond the card without replanning |
| Ambiguous — shaping a card vs building it | **Ask once:** *"Ticket only, or implement?"* | Guessing and coding "helpfully" |

### Observability

The collector follows the shared **`observability`** skill — load it before any
card that touches the collector, and record each card's telemetry decision on
the card ("no change needed" is a decision). It is built with the collector
core, not saved for the `1.0.0` card, which is only the sign-off.

- **The collector is a scheduled job:** each run is an entry point with its own
  root span. It force-flushes all signals on exit within a bounded timeout
  (~5 s), so telemetry can never noticeably slow a build.
- **Logs go to stdout as well as OTLP**, because the Woodpecker step log is
  where a human looks first.
- **Not opted into dashboards or alerts** (skill §8). Freshness — each repo's
  last recorded commit and its age — is a SQL view and a Superset tile over the
  data itself, not telemetry, because it also catches a collector that never
  ran.
- Superset and Postgres are third-party and outside the product contract.

### Deviations register

- **`service.instance.id` is the measured repo, not the hostname.** Each
  Woodpecker step is a fresh container with a random hostname, which would
  start new metric series every run; omitting it would merge concurrent runs
  for different repos. The repo is stable and bounded, and runs per repo are
  serialized by an advisory lock.

- **Moving collector tag in other repos.** The code-vitals workflow step in
  v-note, bored and sovereign-config uses
  `registry.desync.link/code-vitals-collector:stable` with `pull: true` and
  `failure: ignore`, not a pinned digest. Approved because the step can never
  block a build, writes are idempotent, and catch-up refills any missed run —
  while pinning would need three cross-repo PRs per new measure. The image
  bakes in its version and revision and records them with every run. **Every
  other image stays pinned by digest.** Each such step carries a comment
  pointing here.
