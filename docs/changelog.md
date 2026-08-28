# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial project setup
- A2A server integration for agent-to-agent communication
- Asyncio lock for concurrent agent connection restriction
- Virtual filesystem with PostgreSQL persistence
- Langfuse observability integration
- Rate limiting middleware (1 request per 20 seconds)
- Retry middleware with exponential backoff
- Store CLI for managing virtual filesystem
- Development tools for testing and debugging
- Acceptance test suite (`tests/acceptance/`) for agent structure, DB initialization, and A2A connectivity
- Mock test suite (`tests/mock/`) for A2A concurrency lock and task cancellation

### Changed
- N/A

### Deprecated
- N/A

### Removed
- N/A

### Fixed
- N/A

### Security
- N/A
