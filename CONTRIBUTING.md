# Contributing

This is an experimental source preview. Discuss substantial API changes in an
issue before implementing them. Keep patches focused and explain the behavior,
reason, and checks actually performed. Never describe an unrun check as passing.

Use Docker to build the matched Nginx/module pair. Python and TypeScript clients,
SQL fixtures and development commands are included in the repository. See the
[deployment guide](docs/deployment.md) and [platform runbook](docs/platform-support.md).

For an isolated Python environment:

```sh
python3 -m venv .venv-bench
.venv-bench/bin/pip install -r benchmarks/requirements.txt './sdk/python[django]'
```

When validation is appropriate, the repository provides Rust checks in the image
build, isolated HTTP/WebSocket suites, platform control-plane checks and a full
native compatibility matrix. GitHub Actions workflows are currently manual.
Do not run destructive benchmark fixtures against an application database.

Do not commit `.env`, signing keys, database URLs, raw logs or generated build
artifacts. Use fixture credentials only in tests. Report suspected vulnerabilities
privately as described in [SECURITY.md](SECURITY.md).

The optional patched libpq is maintained in the
[PostgreSQL fork](https://github.com/mishabunte/postgres/tree/libpq-borrowed-params).
Update pgxio's pinned revision explicitly when integrating changes.
