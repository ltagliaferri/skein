# Changelog

## Unreleased

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