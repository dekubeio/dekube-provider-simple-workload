# dekube-provider-simple-workload

Simple workload provider for [dekube](https://dekube.io) — converts Deployment, StatefulSet, DaemonSet, and Job manifests into compose services. Naively maps each workload to a compose service without attempting to emulate scheduling, scaling, or lifecycle management.

**The Builder** — one of the Eight Monks, the founding extensions of the helmfile2compose distribution.

> Heresy level: 7/10 — flattens an entire orchestration plane into flat services. The most dangerous rite in the canon.

## Type

`Provider` (priority 500)

## Requires

dekube-engine >= v1.6.0 — passes `sts_name=` to `convert_volume_mounts`, which older engines reject with `TypeError`.

## Kinds

- `Deployment`
- `StatefulSet`
- `DaemonSet`
- `Job`
- `Pod`

## Install

Via [dekube-manager](https://github.com/dekubeio/dekube-manager):

```sh
python3 dekube-manager.py workload
```

Or listed in `distribution.json` — installed automatically when building a distribution.
