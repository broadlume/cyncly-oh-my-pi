# cyncly-oh-my-pi

Cyncly's self-hosted **robomp** GitHub agent — a triage+fix bot orchestrator deployed on AWS via CDK.

This fork of [can1357/oh-my-pi](https://github.com/can1357/oh-my-pi) keeps only the robomp deployment. The `omp` coding-agent binary is **not** built from this repo; the robomp image downloads a prebuilt release binary from upstream `can1357/oh-my-pi` GitHub releases, pinned by the `OMP_VERSION` build-arg in `Dockerfile.robomp`.

## Layout

| Path | Description |
|---|---|
| `python/robomp/` | Orchestrator + gh-proxy FastAPI apps and the SolidJS dashboard (`web/`) |
| `python/omp-rpc/` | Zero-dependency Python RPC client for driving `omp` as a subprocess |
| `infra/cdk/robomp/` | CDK stack (EC2 + ALB) that deploys the robomp compose project |
| `Dockerfile.robomp` | Self-contained image: web bundle + omp_rpc wheel + upstream omp binary |

## Docs

- Deploy: [`infra/cdk/robomp/README.md`](infra/cdk/robomp/README.md)
- Architecture: [`python/robomp/README.md`](python/robomp/README.md)
