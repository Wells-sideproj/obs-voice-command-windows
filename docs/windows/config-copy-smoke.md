# Config-copy smoke evidence

- Date: 2026-09-21
- Scope: project-local scratch directory only; no user `config.toml`, microphone, ASR model, OBS process, or WebSocket was touched.
- Command shape: two fresh `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command` child processes ran the config-copy and `tomllib` parse snippet from [setup.md](setup.md).
- Missing-config case: `config.example.toml` was copied to `config.toml`; SHA-256 matched `config.example.toml`; TOML parse passed.
- Existing-config case: a pre-existing, valid TOML file was left unchanged; the before/after SHA-256 matched; TOML parse passed.
- Scratch directory was removed after both cases.
