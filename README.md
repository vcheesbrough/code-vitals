# code-vitals

Measures of a project's code — size, complexity, change and functionality —
recorded at every trunk commit, so they can be explored over time.

A collector (Python, lizard, tokei), shipped as a container image, runs from
each project's trunk pipeline and records every trunk commit not yet measured.
Measures are versioned plugins: bumping a version recomputes that measure over
the full git history. Results land in a Postgres star schema and are explored
in Apache Superset.

Status: pre-MVP, planning. See [AGENTS.md](AGENTS.md) for working rules.

## License

[PolyForm Noncommercial 1.0.0](LICENSE) — see [LICENSE-TIER.md](LICENSE-TIER.md).
