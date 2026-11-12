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
  - lint, types, tests (the database tests need a throwaway Postgres, which the
    wrapper starts and removes):
    `uv sync && uv run ruff format --check && uv run ruff check && uv run mypy && ./scripts/with-test-postgres.sh env CODE_VITALS_REQUIRE_DB=1 uv run pytest`
  - script tests: `for t in scripts/test-*.sh; do sh "$t"; done`
  - the image, exactly as CI builds it (runs every test above inside the build):
    `. ./tool-versions.env && ./scripts/with-test-postgres.sh docker build --secret id=database_url,env=CODE_VITALS_TEST_DATABASE_URL --build-arg TOKEI_VERSION="$TOKEI_VERSION" --build-arg RELEASE_TAG=0.1.999 --build-arg GIT_REVISION=local --label org.opencontainers.image.version=0.1.999 --label org.opencontainers.image.revision=local --label org.opencontainers.image.created=now -t code-vitals-collector:local .`
    then `scripts/check-image-metadata.sh code-vitals-collector:local 0.1.999 local` and
    `scripts/check-image-tools.sh code-vitals-collector:local 0.1.999 local`
- Extra review criteria beyond the baseline five (correctness, security/OWASP,
  tests, versioning, scope):
  - **Backfillability** — every measure must be computable from git alone at
    any historical commit, or be flagged non-recomputable.
  - **Measure versioning** — any change that alters a measure's values (its
    logic, its area rules, or a pinned tool such as lizard or tokei) bumps that
    measure's version; any change to a repo's area rules bumps that repo's
    ruleset version. The collector enforces both: it refuses a known measure
    version whose unit, kind, recomputability or definition changed, and a
    known ruleset version whose rules changed. Tool-based definitions name the
    tool version, so a tool bump trips the same check.

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

### Environments and promotion

Two environments on mini, each its own compose project of `deploy/compose.yaml`:

| | dev | prod |
| --- | --- | --- |
| Compose project / Postgres container | `code-vitals-dev` / `code-vitals-dev-postgres-1` | `code-vitals` / `code-vitals-postgres-1` |
| Data volume | `code-vitals-dev-pg` | `code-vitals-prod-pg` |
| Superset | https://code-vitals-dev.desync.link (`code-vitals-dev-superset-1`) | https://code-vitals.desync.link (`code-vitals-superset-1`) |
| Databases on that Postgres | `code_vitals` (reset from prod each deploy), `superset` (kept) | `code_vitals`, `superset` (both backed up nightly: mini-config #504) |
| Authentik application / groups | `code-vitals-dev` / `code-vitals-dev-users`, `-admins` | `code-vitals` / `code-vitals-users`, `-admins` |
| Deployed by | Woodpecker *Deploy*, target `dev`, **any branch** (`.woodpecker/deploy-dev.yml`) | target `prod`, **`main` only**, gated in `when:` (`.woodpecker/deploy-prod.yml`) |

- **Every deployment collects and checks** (card #497): after migrate, one
  step per measured repo runs the deployed image by digest
  (`scripts/run-collector.sh`: clone, `collect --trigger backfill`,
  `check-repo`), then `check` runs `check-status`; `tag-release` waits on it.
  A measure or ruleset bump therefore backfills in dev from the branch and in
  prod on release, and must update `src/code_vitals/expected_values.toml` in
  the same PR. Only prod moves `:stable`, in `promote-stable`, after
  `tag-release`.
- **A push never deploys.** A deployment promotes the image the push already
  published (resolved through the commit, then used by digest) and never
  rebuilds. The git tag is pushed only after it succeeds.
- **Prove a branch in dev before merging it.** Every dev deploy recreates the
  dev database from a fresh `pg_dump` of prod (empty while prod isn't running),
  then migrates it: the migration is tested on prod-shaped data and no row a
  previous dev deploy wrote survives. Dev is disposable; never keep anything
  there.
- **Postgres publishes no port.** Migrations, the collector and Superset run
  as containers on the project's network through the agent's docker socket;
  only Superset is also on Traefik's `proxy-backend`.
- **Superset** (`superset/`) is its own image, `code-vitals-superset`, built
  and published by the push pipeline beside the collector under the same
  version. Sign-in is OIDC through Authentik (`authentik/` blueprints, applied
  on deploy); group membership is managed by hand in Authentik. Each deploy
  runs `superset db upgrade`, `init` and `set-database-uri` in a one-off
  container (`superset/bootstrap.sh`), then smoke-checks health, the sign-in
  redirect and the reader role (`scripts/smoke-superset.sh`) before tagging.
- `DEPLOY_PULL=missing ./scripts/deploy-stack.sh <env> <collector> <superset>`
  (with the six secrets and a `proxy-backend` network) runs a stack on a local
  docker for trying changes; never point it at mini by hand.
- `tests/test_pipelines.py` pins the gates: prod only from `main`, dev only to
  dev, a push never deploys, every step image pinned by digest, every repo
  collected and checked before the tag, `:stable` moved by prod only and last. **The gates
  guard against accidents, not against a deliberate change** — see the trust
  assumption in § Deviations register.

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
- **Logs: OTLP is the record; the CI step log gets plain text.** Structured
  log records go over OTLP (trace-correlated in Loki). stdout carries a few
  human lines (run start, summary) and stderr every WARNING/ERROR with its
  traceback, because the Woodpecker step log is where a human looks first —
  with or without telemetry.
- **Not opted into dashboards or alerts** (skill §8). Freshness — each repo's
  last recorded commit and its age — is a SQL view and a Superset tile over the
  data itself, not telemetry, because it also catches a collector that never
  ran.
- Superset and Postgres are third-party and outside the product contract.
- **How it is built** (card #494): `src/code_vitals/telemetry.py` is the only
  module that imports the OpenTelemetry SDK or exporters
  (`tests/test_boundaries.py`); product code emits through
  `src/code_vitals/obs.py` (API only: instruments, attribute keys, span
  helpers) and logs under the `code_vitals` logger — the OTLP log handler goes
  there, never on the root logger (it deadlocks the flush when the collector is
  down). OTLP over http/protobuf to `monitor-alloy:4318` on `proxy-backend` is
  deliberate; a dedicated telemetry network was considered and rejected. The
  Python binding is the observability skill's `references/python.md`.

### Deviations register

- **Trust assumption: only the owner pushes branches or starts deployments.**
  Woodpecker runs the pipeline files of the commit it builds or deploys, so a
  branch can rewrite the prod gate in `deploy-prod.yml` (and the test that pins
  it). The sovereign-config broker hands this repo's whole per-repo layer —
  dev **and** prod database passwords — plus the shared layer
  (`github_token`, zot credentials) to every pipeline of the repo, whatever its
  event; it has no per-event or per-branch scoping. And a dev deploy runs
  `pg_dump` on the prod container by design, so dev has prod's data. bored
  records the same limit for its blueprint steps. Making it a real boundary
  needs the broker to withhold prod secrets from non-deployment, non-`main`
  pipelines; until then the gates stop mistakes only.

- **Deploy secrets via `from_secret`, not `sovereign-config render`.** bored's
  deploy reads its compose layer through a read-only sovereign-config
  connection. That connection can only be created through the sovereign-config
  MCP, which returns its URL into the agent transcript (baseline §7), so the
  deploy's secrets (`code_vitals_{dev,prod}_{postgres,writer,reader}_password`,
  `code_vitals_{dev,prod}_{superset_secret_key,superset_db_password,oidc_client_secret}`)
  live in the broker's per-repo layer
  `/woodpecker/repos/vcheesbrough/code-vitals/` and reach the steps through
  `from_secret`, as sovereign-config's own deploy does. They were generated with
  `openssl rand -hex 32 | sovereign-config set … --secret`, never displayed. To
  move to `render`: create a read-only `code-vitals-devops` connection rooted at
  `/code-vitals/devops` by hand, store its URL in the per-repo layer, and move
  the values under `/code-vitals/devops/<env>/compose`.

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
