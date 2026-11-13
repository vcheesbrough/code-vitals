# code-vitals

Measures of a project's code — size, complexity, change and functionality —
recorded at every trunk commit, so they can be explored over time.

A collector (Python, lizard, tokei), shipped as a container image, runs from
each project's trunk pipeline — one Woodpecker plugin step the project adds
itself (§ Measuring a repo) — and records every trunk commit not yet measured.
Measures are versioned plugins: bumping a version recomputes that measure over
the full git history. Results land in a Postgres star schema and are explored
in Apache Superset.

Status: pre-MVP. See [AGENTS.md](AGENTS.md) for working rules.

## Development

Needs [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
uv run ruff format --check && uv run ruff check && uv run mypy
./scripts/with-test-postgres.sh uv run pytest   # database tests skip without it
for t in scripts/test-*.sh; do sh "$t"; done
uv run code-vitals --version
```

The image build runs all of the above in its test stage; AGENTS.md lists the
exact command CI uses.

## The collector image

`registry.desync.link/code-vitals-collector` carries:

| Tool | Pinned in |
| --- | --- |
| Python 3.13 (`python:3.13.16-slim`, by digest) | `Dockerfile` |
| lizard | `pyproject.toml` (`lizard==…`) and `uv.lock` |
| tokei (built from source) | `tool-versions.env` |
| git | Debian package of the base image |

Changing a pinned measuring tool changes measured values, so it is a measure
version bump, never a silent update.

`registry.desync.link/code-vitals-plugin` (`plugin/Dockerfile`) is that image
plus the docker CLI, with `code-vitals plugin` as its entrypoint: the
Woodpecker plugin of § Measuring a repo. It is built, checked and published with
the collector under the same version, and starts the collector of that version.

## Measuring a repo

A public repo is measured by adding one step to it, in
`.woodpecker/code-vitals.yml` — no change to code-vitals and no secrets change
(the writer password is in the broker's shared layer):

```yaml
# code-vitals: record this repo's trunk commits (vcheesbrough/code-vitals).
when:
  - event: [push, manual]
    branch: main
skip_clone: true  # the collector clones the repo itself, with full history
steps:
  code-vitals:
    # Moving tag: code-vitals AGENTS.md § Deviations register.
    image: registry.desync.link/code-vitals-plugin:stable
    failure: ignore
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    settings:
      trunk: main
      version_sources:            # optional; tried in order, the minor is the iteration
        - {path: Cargo.toml, kind: cargo-workspace}
      areas:                      # optional; without it: tests vs everything else
        version: 1                # bump whenever the rules change
        rules:
          - {glob: "crates/**", area: server}
          - {glob: "frontend/**", area: web}
      writer_password:
        from_secret: code_vitals_prod_writer_password
```

- `trunk` (required): the branch whose first-parent history is measured.
- `version_sources`: each `{path, kind}`, `kind` one of `cargo-workspace`,
  `cargo-package`, `pyproject`, `plain`.
- `areas`: `version` and `rules` (`{glob, area}`, first match wins, area names
  lower-case words). Shared excludes, the test rules and the `tooling`
  catch-all are added around them (`src/code_vitals/areas.py`). Changing the
  rules without a new `version` is refused; a new `version` re-measures the
  repo's history at the next deploy, and its fixed values in
  `expected_values.toml`, if any, are skipped until repinned.

The step must be in `.woodpecker/code-vitals.yml` (deployments read its
settings there; the plugin refuses to run from any other workflow). The plugin
validates the settings (`src/code_vitals/settings.py`; an unknown or invalid
setting fails the step with a message naming it), then starts the
collector on code-vitals' prod network through the socket. The first run
registers the repo under its name and URL (a second repo of the same name
from another URL is refused) and measures its whole history. Every
deployment then re-measures it from the same settings, read from that file at
its trunk head; a repo whose file has no plugin step is skipped. The
operations and e2e-suite rules of the `functionality` measures are still per
repo name in `src/code_vitals/measures/functionality.py`, so a new repo has
none until one is added there (a measure version bump).

## Versioning

Ported from bored (its `.woodpecker/build.yml`, card #461):

- **Version** = `MAJOR.MINOR` from `pyproject.toml` + the push pipeline's
  Woodpecker number as the patch. `MINOR` is the iteration number; pre-MVP
  versions are `0.N.<pipeline>`.
- **Every push** (any branch) builds the image, runs every test inside the
  build, then publishes `:<version>` and `:commit-<sha>`. An existing version
  is never overwritten.
- **Deployments** promote an already-published image and never rebuild; the git
  tag (same string as the version) is pushed only after a successful
  deployment.

## Collecting

```sh
CODE_VITALS_DATABASE_URL=postgresql://code_vitals_writer:…@code-vitals-db:5432/code_vitals \
CODE_VITALS_SETTINGS='{"trunk": "main", "version_sources": [{"path": "Cargo.toml", "kind": "cargo-workspace"}]}' \
  code-vitals collect --repo bored --url https://github.com/vcheesbrough/bored.git \
    --clone /path/to/full/clone --trigger push
```

`CODE_VITALS_SETTINGS` holds the repo's plugin settings as JSON (§ Measuring a
repo); the plugin passes them so. `--fetch` in place of `--clone` clones
`--url` into a temporary directory first (a failed clone is recorded as a
failed run). `--url` is recorded on the repo for deployments to clone.

Records every trunk commit (first-parent history of the repo's trunk) not yet
measured, for every registered measure version (`src/code_vitals/measures/`),
into the database as the writer role. Safe to rerun, to run concurrently (a
per-repo advisory lock), and to interrupt (each commit is one transaction; the
next run finishes the rest). The measured repos are the rows of the `repo`
table; each registers itself on its plugin step's first run.

A run with no failed commit records that the repo is caught up on every
version it ran (`measure_backfill`). A version turns `complete` once every
repo has caught up on it, and a repo's area ruleset once a run under it
succeeds; that is when the `current_*` views switch to them. A
non-recomputable measure version is recorded only at commits from its
release on (`measure_recomputable` and `measure_released_at` in
`current_measurement` mark the break).

Every deployment runs this over every registered repo with the deployed
image, in parallel, then checks (`scripts/run-collector.sh`):

- `code-vitals list-repos` — the registered repos and their URLs.
- `code-vitals backfill-repo --repo R --url U --trunk T` — clones the repo,
  reads its plugin settings from `.woodpecker/code-vitals.yml` at the trunk
  head (no plugin step: skipped, exit 3), collects with trigger `backfill`,
  then checks: a second catch-up finds nothing to do, the repo's area ruleset
  is `complete`, and the values at its fixed commits in
  `src/code_vitals/expected_values.toml` (a repo without an entry has none)
  are exactly those recorded. A change that alters values bumps the measure's
  version and updates the table in the same PR; on a mismatch the command
  prints the recorded entries to paste.
- `code-vitals check-status --repo R …` — every version this build registers
  is `complete`, and the last run of every repo collected (not skipped)
  succeeded.

`code-vitals retire <measure> <version> [--delete-rows]` retires a version this
build no longer registers: it leaves the views; `--delete-rows` also deletes
its rows, as the owner.

Telemetry is configured by the standard `OTEL_*` variables only — none set
means off. In a deployment: `OTEL_EXPORTER_OTLP_ENDPOINT=http://monitor-alloy:4318`,
`OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`,
`OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=<env>`; the container must
be on the stack network and `proxy-backend`.

## Measures

Snapshot measures, recorded at every trunk commit and split by area
(each repo's versioned area rules are in its plugin settings; shared
excludes and test rules in `src/code_vitals/areas.py`):

| Measure | Tool | Facet | What |
| --- | --- | --- | --- |
| `files` | git | — | regular files in the tree, unsplit |
| `lines_of_code` | tokei | language | code lines (not comments or blanks) |
| `source_files` | tokei | language | files tokei recognises |
| `nloc` | lizard | — | non-comment lines of the files lizard analyses |
| `functions` | lizard | — | functions and methods |
| `complexity_total` | lizard | — | sum of cyclomatic complexity |
| `functions_over_10` | lizard | — | functions with complexity over 10 |
| `max_function_complexity` | lizard | — | the most complex function's complexity |
| `function_complexity` | lizard | — | one row per function (hotspots), in `function_complexity` |
| `operations` | regex | kind | HTTP routes, gRPC methods (distinct `Service.Method`), MCP tools, outside the tests area |
| `e2e_scenarios` | regex | suite | Playwright scenarios, Kotlin `@Test`, Rust integration tests, per suite |

Flow measures (what each commit changed against its first parent, summed per
period): `lines_added`, `lines_deleted`, `files_changed`, by area, from
`git diff --numstat --no-renames`.

The operation and suite patterns are per repo, in
`src/code_vitals/measures/functionality.py`; their digest is part of each
measure's definition, so changing a pattern needs a version bump.

Ratios are not stored: complexity per 1,000 lines is `complexity_total / nloc`
(lizard's lines, as the prototype used), not over tokei's `lines_of_code`.

## Database

`code-vitals migrate` applies the schema (`src/code_vitals/db/migrations/`,
plain SQL, numbered) and then the role grants (`src/code_vitals/db/roles.sql`)
to the database named by `CODE_VITALS_DATABASE_URL`, as its owner. With
`CODE_VITALS_WRITER_PASSWORD` / `CODE_VITALS_READER_PASSWORD` set, those roles
get their login.

| Role | May |
| --- | --- |
| `code_vitals` (owner) | everything; used only by `migrate` and `retire --delete-rows` |
| `code_vitals_writer` (the collector) | read, insert and update the tables; never alter, drop, delete or create |
| `code_vitals_reader` (Superset) | `SELECT` on `current_measurement`, `current_function_complexity` and `freshness` only |

The `current_*` views show only measure versions and area rulesets whose
backfill is `complete`, so a chart never mixes two definitions of a measure.

## Superset

https://code-vitals.desync.link (prod) and https://code-vitals-dev.desync.link
(dev). Sign in through Authentik; access needs membership of
`code-vitals[-dev]-users` (explore and build charts, Superset's Alpha role) or
`code-vitals[-dev]-admins` (Admin), granted in Authentik. Superset reads
code-vitals only as `code_vitals_reader`. The image (`superset/Dockerfile`) is
Apache Superset 6.1.0 plus the Postgres driver, authlib and
`superset/superset_config.py`.

The **code-vitals dashboard** (`/superset/dashboard/code-vitals/`) is code:
`superset/dashboard/` is Superset's import/export bundle (an export zip,
unzipped — `metadata.yaml`, `databases/`, `datasets/`, `charts/`,
`dashboards/`), and every deploy imports it over whatever is there
(`superset/code_vitals_dashboard.py`), so a deleted or edited dashboard comes
back on the next deploy, and a dataset, chart or dashboard dropped from the
bundle is deleted from Superset (one still used by a chart made in Superset is
kept and reported). To change it, edit the YAML (or build a copy in
Superset, export it and bring the YAML back) — never the `sqlalchemy_uri` in
`databases/code-vitals.yaml`, which stays masked: the deploy never imports the
database file, and the connection's URI is set by `set-database-uri`. The
datasets read only the reader's views; `tests/test_superset_dashboard.py`
checks that, the references, the filters and the colours, and
`tests/test_superset_datasets.py` runs the datasets' SQL as the reader.
A chart with an x-axis queries a row per x-value and group, counted over
every measure's groups, and Superset cuts a result off at its row limit
without an error: each deploy's smoke (`code_vitals_dashboard --check`)
counts those rows for every such chart and fails once one passes 80% of its
limit (card #552), reporting the closest chart. It runs only when code-vitals
deploys, while the rows grow with every measured repo's commits, so the 20%
headroom must cover the growth between two deploys.
The import itself is tested against the real Superset in the image before the push
pipeline publishes it (`superset/dashboard_import_test.py`, run by
`scripts/check-superset-import.sh`): the connection keeps its URI and gets the
bundle's UUID, a re-run changes nothing, the dashboard's charts are replaced
rather than merged, a failing import changes nothing, and what the bundle
dropped is deleted unless something made in Superset uses it. Locally, after
building both images:
`./scripts/with-test-postgres.sh ./scripts/check-superset-import.sh <superset-image> <collector-image>`.

| Dataset | What |
| --- | --- |
| `commit_snapshot` | snapshot measures at every trunk commit time (any repo's): each repo's last measured commit at or before it, carried forward, so a repo's series steps only at its own commits and totals never dip. Plot it with **no time grain** — a grain would sum several commits' states |
| `iteration_snapshot` | snapshot measures at each iteration's last commit |
| `current_measurement` | the view, for flow measures (churn): a bar per commit, or summed per iteration |
| `hotspots` | `current_function_complexity` plus the week and a commit label |
| `freshness` | the view: each repo's newest recorded commit, its age and the last run |

Ratios are Superset metrics (complexity per 1,000 lines, average complexity
per function, test share of lines). Every over-time chart has a point per
commit at its commit time (no time grain); snapshots are drawn as steps.
Filters: Repo, Area, Commit time (a time range on `committed_at`) and Iteration
apply to every chart whose dataset has the column, except Area on the test
share (a ratio across areas); Commit picks the moment of the Hotspots table
(newest first), which the Commit time range leaves alone. The **Freshness** table, one
row per repo, is the pipeline's health check (no alerts, by decision). Colours
come from the dataviz reference palette (`codeVitals` in
`superset_config.py`), one fixed slot per repo and per area.

## Deploying

A Woodpecker *Deploy* of a commit, with target `dev` (any branch) or `prod`
(`main` only), promotes the image its push already published and never
rebuilds. Each dev deploy starts from a fresh copy of prod. After migrating,
it collects and checks every repo (§ Collecting), so a bumped measure version
backfills in dev from the branch and in prod on release. A prod deploy moves
`code-vitals-plugin:stable` — what the measured repos' plugin steps run — and
`code-vitals-collector:stable` last, once its checks passed. See AGENTS.md
§ Environments and promotion.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — see [LICENSE-TIER.md](LICENSE-TIER.md).
