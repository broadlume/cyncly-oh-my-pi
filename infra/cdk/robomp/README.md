# robomp CDK (hardened EC2 + ALB)

Deploys **robomp + gh-proxy + LiteLLM** on a single private EC2 instance behind
an internet-facing Application Load Balancer.

## Isolation guarantees

| Control | Behavior |
|---|---|
| Network | Brand-new VPC (`10.77.0.0/16` by default). No peering, TGW, or shared subnets created by this stack. |
| Placement | EC2 in **private** subnets only (NAT egress). **No public IP.** |
| Ingress | ALB SG → instance `:8080` only. No SSH (port 22 closed). |
| ALB paths | HTTPS listener default = `404`. Only `/webhook/github` forwards. |
| Health | ALB probes `/healthz` (not exposed as a public listener rule). |
| IAM | Instance role can **only** `secretsmanager:GetSecretValue` (+ KMS decrypt) on the stack secret. Explicit **Deny** on S3/EC2/SSM/ECR/IAM/… |
| IMDS | IMDSv2 required, hop limit **1** (containers cannot reach the metadata service). Extra iptables DROP to `169.254.169.254` from `DOCKER-USER`. |
| Disk | Root + data EBS encrypted with the stack CMK. |
| WAF | Regional Web ACL (Common + KnownBadInputs + rate limit) associated to the ALB. |
| Images | Pulled from **GHCR** (no ECR / no in-account registry IAM). |

> “No access to other resources in the AWS account” is enforced by **identity**
> (minimal allow + broad deny) and **network** (dedicated VPC, tight SGs/NACLs).
> The instance still uses the public AWS Secrets Manager endpoint over HTTPS via
> NAT to read **its own** secret — that is the only AWS API call it can make.

## Prerequisites

1. AWS credentials with rights to deploy the stack.
2. An **ACM certificate in the same region as the stack** (for the ALB).
   ACM certs are regional — a `us-east-2` cert cannot attach to a `us-east-1` ALB.
   Match `CDK_DEFAULT_REGION` / `aws configure get region` to the cert ARN region.
3. A GHCR-published robomp image built from this repo (omp binary pulled from upstream releases).
4. After deploy: fill the Secrets Manager JSON (`REPLACE_ME` keys).

## Build & push the robomp image (GHCR)

From the repo root (amd64 for the default `m6i` instance):

```bash
export GHCR_OWNER=epfister-cyncly
export TAG=$(git rev-parse --short HEAD)

docker build --platform linux/amd64 -f Dockerfile.robomp \
  --build-arg OMP_VERSION=v17.3.1 \
  -t ghcr.io/$GHCR_OWNER/robomp:$TAG -t ghcr.io/$GHCR_OWNER/robomp:latest .

echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_OWNER" --password-stdin
docker push ghcr.io/$GHCR_OWNER/robomp:$TAG
docker push ghcr.io/$GHCR_OWNER/robomp:latest
```

Or use `.github/workflows/robomp-ghcr.yml`.

## Deploy

```bash
cd infra/cdk/robomp
npm install
npx cdk bootstrap   # once per account/region
npx cdk deploy   -c certificateArn=arn:aws:acm:REGION:ACCOUNT:certificate/ID   -c robompImage=ghcr.io/epfister-cyncly/robomp:TAG   -c envName=prod
```

Optional context:

- `litellmImage` (default `ghcr.io/berriai/litellm:main-stable`)
- `instanceType` (default `m6i.xlarge`)

## Fill the secret

```bash
aws secretsmanager get-secret-value --secret-id robomp/prod/config   --query SecretString --output text > /tmp/robomp-secret.json
$EDITOR /tmp/robomp-secret.json   # replace REPLACE_ME
aws secretsmanager put-secret-value --secret-id robomp/prod/config   --secret-string file:///tmp/robomp-secret.json
rm -f /tmp/robomp-secret.json
```

Required keys: `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `ROBOMP_GH_PROXY_HMAC_KEY`,
`ROBOMP_BOT_LOGIN`, `ROBOMP_GIT_AUTHOR_EMAIL`, `ROBOMP_REPO_ALLOWLIST`,
`LITELLM_MASTER_KEY`, `GHCR_USERNAME`, `GHCR_TOKEN`, plus at least one provider
key (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / …).

Then reboot the instance (or re-run the bootstrap) so compose picks up the
values:

```bash
aws ec2 reboot-instances --instance-ids "$(aws cloudformation describe-stacks   --stack-name Robomp-prod --query "Stacks[0].Outputs[?OutputKey=='InstanceId'].OutputValue" --output text)"
```

## GitHub webhook

1. Put a DNS CNAME for your ACM hostname on the `AlbDnsName` output (recommended).
2. Webhook URL: `https://<your-host>/webhook/github`
3. Content type: `application/json`
4. Secret: same as `GITHUB_WEBHOOK_SECRET`
5. Events: Issues, Issue comments, Pull requests, Pull request reviews, Pull request review comments


## Agent configuration (skills, subagents, MCP servers, omp settings)

The agent runs with `HOME=/srv/agent-home`. omp reads `~/.omp/agent/**` and
`~/.agent/**` from there. Two layers build that tree, in this order, later wins
**per file**:

| Layer | Source | Channel | Carries |
|---|---|---|---|
| 1 | `infra/cdk/robomp/assets/agent-bundle/` | Docker image (`/srv/agent-home-stage`) | skills, subagents, base `config.yml`, base `mcp.json`, `AGENTS.md`, base rules |
| 2 | `/etc/robomp/agent-home/` on the instance | user-data + secret (`/srv/agent-home-override`, read-only mount) | `models.yml`, `00-aws-isolation.md`, optional `AGENTS.md` / `config.yml` / `mcp.json` |

Both the container entrypoint and `worker._stage_agent_home()` copy layer 1 then
layer 2 with `cp -a <src>/. <dst>/`, so an override file replaces only itself and
never hides a sibling in the same directory.

Every layer is a literal image of `$HOME`, so both trees use real dotfile
directories and hold the same relative paths:

```
.omp/agent/config.yml                  omp settings (YAML)
.omp/agent/mcp.json                    { "mcpServers": { "<name>": { … } } }
.omp/agent/agents/<name>.md            subagent definition
.omp/agent/skills/<name>/SKILL.md      skill
.omp/agent/models.yml                  model/gateway routing (layer 2 only)
.agent/AGENTS.md                       always-on instructions
.agent/rules/<NN>-<name>.md            rule files, sorted by name
```

**Frontmatter is load-bearing.** A skill needs `name` and `description`; a
`SKILL.md` with no `description` is dropped silently. A subagent needs `name` and
`description`; `tools` is an optional CSV allowlist. Prefixes keep rules ordered:
`00-` is reserved for the CDK-supplied AWS isolation rule, so bundle rules start
at `10-`.

MCP server entry shape — per-server keys are `command`, `args`, `env`, `cwd`,
`url`, `headers`, `type` (`stdio` | `sse` | `http`), `enabled`, `timeout`,
`requestIdFormat`, `auth`, `oauth`. Values expand `${VAR}` from the omp process
environment:

```json
{
  "mcpServers": {
    "local-tool": {
      "type": "stdio",
      "command": "my-mcp-server",
      "args": ["--stdio"],
      "env": { "XDG_CACHE_HOME": "${XDG_CACHE_HOME}" }
    },
    "remote-tool": {
      "type": "http",
      "url": "https://mcp.example.internal/v1",
      "headers": { "Authorization": "Bearer ${MY_MCP_TOKEN}" }
    }
  }
}
```

A stdio server must not write to `$HOME`: the agent runs as `omp-<n>` with a
root-owned, read-only `HOME`. Point it at the per-slot writable cache via
`XDG_CACHE_HOME` as shown.

### Operator loop

The two halves of `assets/` deploy through different channels:

- Change under `assets/agent-bundle/` → rebuild and push the image, then deploy
  with the new `-c robompImage=` tag. `npx cdk synth` alone changes nothing.
- Change under `assets/` (rules, models.container.yml) → `npx cdk deploy`;
  the instance is replaced and cloud-init re-renders `/etc/robomp/agent-home`.
- Optional secret keys `OMP_CONFIG_YML` and `OMP_MCP_JSON` hold whole-file text.
  A written file **replaces** the baked file of the same name; it is not merged.
  Store them as JSON strings with `\n` escapes, not nested objects. Bump
  `-c provisionNonce=<n>` to force a re-read of the secret.

EC2 caps user-data at 16 KB. The synth budget is 15,872 bytes — the limit minus a
512-byte margin, because `UserData.render()` still holds CFN tokens (secret ARN,
region, image ref) that grow when CloudFormation resolves them. Synth fails with
an explicit message when the budget is exceeded. Multi-file assets (skills,
subagents) must go in the image bundle, never in user-data. `docker-compose.aws.yml`
travels in the image too; the bootstrap extracts it with `docker cp` after the GHCR
login, which keeps the compose file and the image it launches in lockstep.

## Layout

```
infra/cdk/robomp/
  bin/app.ts
  lib/robomp-stack.ts
  assets/                 # baked into instance user-data at synth time
  assets/agent-bundle/    # baked into the robomp image, NOT user-data
python/robomp/
  docker-compose.yml      # base (gh-proxy isolation unchanged)
  docker-compose.aws.yml  # GHCR image + LiteLLM; baked into the image, docker cp'd at boot
```
