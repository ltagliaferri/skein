# Changelog

## Unreleased

### Changed
- The CLI's URL resolution now bottoms out on the machine's service address
  (the same `SKEIN_HOST`/`SKEIN_PORT`/`server.json` ladder `skein-server`
  binds), so moving the service moves every `skein` command with it. Before,
  the two resolved independently and `SKEIN_PORT=8123 skein-server` stranded
  the CLI on 8001.
- A project or global `server_url` holding exactly the literal `skein init`
  used to write (`http://localhost:8001` / `http://127.0.0.1:8001`) is read as
  absent; a deliberately different value is honored. Configs are not rewritten.
- `skein init` no longer writes `server_url` into project configs — a project
  config is shared across machines, and one machine's address does not belong
  in it.
- `skein doctor` reports which rung the CLI's URL came from, and warns about
  values resolution had to ignore (an unparseable `SKEIN_PORT`, an unusable
  config file). An unparseable `SKEIN_PORT` never crashes CLI commands;
  `skein-server` itself still refuses to start on one unless `--port` is given.

### Added
- Top-level `--project` flag on every CLI command (overrides cwd `.skein/` discovery)
- `project:site` colon syntax on `skein post` (issue/brief/friction/notion/finding/summary) and `skein playbook create`
- Documented `SKEIN_PROJECT` env var (already worked, was undocumented)
- Cross-project precedence: colon-syntax > `--project` flag > `SKEIN_PROJECT` env > cwd `.skein/`

## [0.2.0] - 2024-11-20

Initial open source release.

### Added
- Multi-project support with `.skein/` directories
- Project-specific storage isolation
- Configurable server (SKEIN_PORT, SKEIN_HOST env vars)
- CLI auto-detection of project config
- Unified search API
- Brief handoff system
- Thread-based status and assignment

### Changed
- Storage now requires project initialization (`skein init`)
- Logs and screenshots use project-specific databases

## [0.1.0] - 2024-11-06

Initial internal release.

### Added
- Core SKEIN server and CLI
- Sites, folios, findings, issues, briefs
- Agent roster management
- Thread connections between folios
- SQLite logs and JSON artifact storage