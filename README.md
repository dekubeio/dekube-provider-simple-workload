# h2c-provider-simple-workload

Simple workload provider for [helmfile2compose](https://github.com/helmfile2compose/helmfile2compose) — converts Deployment, StatefulSet, DaemonSet, and Job manifests into compose services. Naively maps each workload to a compose service without attempting to emulate scheduling, scaling, or lifecycle management.

**The Builder** — one of the Eight Monks, the founding extensions of the helmfile2compose distribution.

> Heresy level: 7/10 — flattens an entire orchestration plane into flat services. The most dangerous rite in the canon.

## Type

`Provider` (priority 500)

## Kinds

- `Deployment`
- `StatefulSet`
- `DaemonSet`
- `Job`

## Install

Via [h2c-manager](https://github.com/helmfile2compose/h2c-manager):

```sh
python3 h2c-manager.py workload
```

Or listed in `distribution.json` — installed automatically when building a distribution.
