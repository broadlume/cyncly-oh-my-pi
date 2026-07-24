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
2. An **ACM certificate** in the same region as the stack (for the ALB).
3. A GHCR-published robomp image built from this fork (pi source baked in).
4. After deploy: fill the Secrets Manager JSON (`REPLACE_ME` keys).

## Build & push the robomp image (GHCR)

From the monorepo root (amd64 for the default `m6i` instance):

```bash
export GHCR_OWNER=epfister-cyncly
export TAG=$(git rev-parse --short HEAD)

# 1) pi runtime base (bakes /pi source)
docker build --platform linux/amd64 -t ghcr.io/$GHCR_OWNER/pi:$TAG .

# 2) robomp on top — override PI_ROOT default at runtime via compose.aws
docker build --platform linux/amd64   -f Dockerfile.robomp   --build-arg PI_BASE=ghcr.io/$GHCR_OWNER/pi:$TAG   -t ghcr.io/$GHCR_OWNER/robomp:$TAG   -t ghcr.io/$GHCR_OWNER/robomp:latest   .

echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_OWNER" --password-stdin
docker push ghcr.io/$GHCR_OWNER/pi:$TAG
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

## Layout

```
infra/cdk/robomp/
  bin/app.ts
  lib/robomp-stack.ts
  assets/                 # baked into instance user-data at synth time
python/robomp/
  docker-compose.yml      # base (gh-proxy isolation unchanged)
  docker-compose.aws.yml  # GHCR image + LiteLLM + PI_ROOT=/pi
```
