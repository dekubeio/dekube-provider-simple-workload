# h2c-converter-workload

Workload converter for [helmfile2compose](https://github.com/helmfile2compose/helmfile2compose) — converts Deployment, StatefulSet, DaemonSet, and Job manifests into compose services.

**The Builder** — one of the Seven Bishops, the founding extensions of the helmfile2compose distribution.

## Type

`Provider` (priority 500)

## Kinds

- `Deployment`
- `StatefulSet`
- `DaemonSet`
- `Job`

## Note

This is a **build-time only** extension, designed to be concatenated by `build-distribution.py` into a single-file distribution. It uses internal core imports that are resolved at build time. It is **not** designed for runtime loading via `--extensions-dir`.

## Install

Listed in `distribution.json` — installed automatically when building a distribution via `h2c-manager`.
