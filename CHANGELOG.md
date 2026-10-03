# Changelog

All notable changes to harvest-ai are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - unreleased

First public release.

### Changed
- Distribution name `harvest-ai` (the PyPI name `harvest` belongs to an unrelated project) and import package
  `harvest_ai`, so both can be installed side by side. The `harvest` command stays; `harvest-ai` is an alias;
  `harvest-mcp` starts the MCP server. Scraper modules import `harvest_ai.extract` / `harvest_ai.mcp_client`; the review
  gate refuses the old `harvest` import name with a clear finding.
- Licence: Apache-2.0 (was all rights reserved).
- systemd examples no longer assume an install path.

### Added
- Project files: LICENSE and NOTICE, contributing guide with DCO sign-off, code of conduct, security policy, issue and
  pull request templates, release workflow (PyPI trusted publishing), CodeQL, secret scanning, Dependabot.
