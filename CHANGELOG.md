# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Before 1.0, breaking changes ship as minor bumps.

## [Unreleased]

### Documentation

- Sync the README, API reference and contributing guide

### Maintenance

- Ignore the local research notes
- Build the docs in CI and reuse the gate wheel
- Check comments and docstrings in the gate
- Decode frames through one lookup table
- Reuse the protocol checksum in the capture redaction
- Move the transport connect timeout into the constants
- Track the client session with one lifecycle phase

## [0.1.0] - 2026-10-08

### Bug Fixes

- Rework the client session failure path and connect errors

### Documentation

- Document the renamed timing options and new errors
- Drop the pointer to other treadmill libraries

### Features

- Add the EFC treadmill client library

### Maintenance

- Run the hardware scripts as modules and rename the drive script
- Ship the license in the wheel and fix the CI Python matrix
- Stop tracking AGENTS.md
- Remove AGENTS.md from the repository

[Unreleased]: https://github.com/DaanVervacke/citysports-efc-ble/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/DaanVervacke/citysports-efc-ble/releases/tag/v0.1.0


