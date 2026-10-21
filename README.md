# code-vitals

Measures of a project's code — size, complexity, change and functionality —
recorded at every trunk commit, so they can be explored over time.

A collector (Python, lizard, tokei), shipped as a container image, runs from
each project's trunk pipeline and records every trunk commit not yet measured.
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

## Database

`code-vitals migrate` applies the schema (`src/code_vitals/db/migrations/`,
plain SQL, numbered) and then the role grants (`src/code_vitals/db/roles.sql`)
to the database named by `CODE_VITALS_DATABASE_URL`, as its owner. With
`CODE_VITALS_WRITER_PASSWORD` / `CODE_VITALS_READER_PASSWORD` set, those roles
get their login.

| Role | May |
| --- | --- |
| `code_vitals` (owner) | everything; used only by `migrate` |
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

## Deploying

A Woodpecker *Deploy* of a commit, with target `dev` (any branch) or `prod`
(`main` only), promotes the image its push already published and never
rebuilds. Each dev deploy starts from a fresh copy of prod. See AGENTS.md
§ Environments and promotion.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — see [LICENSE-TIER.md](LICENSE-TIER.md).
