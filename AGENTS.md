# Development Rules

## Repo Scope

This repo hosts only the self-hosted **robomp** GitHub agent and its AWS deploy:

|Path|Description|
|---|---|
|`python/robomp/`|Orchestrator + gh-proxy + SolidJS dashboard (`web/`)|
|`python/omp-rpc/`|Python RPC client for driving `omp` as a subprocess|
|`infra/cdk/robomp/`|CDK stack (EC2 + ALB)|
|`Dockerfile.robomp`|Self-contained image build|

The `omp` binary is pulled from upstream `can1357/oh-my-pi` releases (pinned by `OMP_VERSION` in `Dockerfile.robomp`) — **never re-add monorepo source builds**.

## Testing

- Python: `cd python/robomp && pytest` (and `cd python/omp-rpc && pytest tests`).
- Web dashboard: `cd python/robomp/web && bun run build`.

## GitHub

Unless the user tells you exactly what to write:
- **Never comment on GitHub** (issues, PRs, discussions).
- **Never create issues on GitHub**.

## Commands

- NEVER commit unless asked.
