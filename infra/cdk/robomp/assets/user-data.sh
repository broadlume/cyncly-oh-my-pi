#!/bin/bash
set -euo pipefail
exec > >(tee -a /var/log/robomp-bootstrap.log) 2>&1

echo "[robomp] bootstrap starting $(date -u +%FT%TZ)"

ROBOMP_SECRET_ARN="__ROBOMP_SECRET_ARN__"
AWS_REGION="__AWS_REGION__"
ROBOMP_IMAGE="__ROBOMP_IMAGE__"
LITELLM_IMAGE="__LITELLM_IMAGE__"
DATA_DEVICE="__DATA_DEVICE__"
COMPOSE_DIR=/opt/robomp
CONFIG_DIR=/etc/robomp
DATA_MOUNT=/var/lib/docker/volumes

export AWS_DEFAULT_REGION="$AWS_REGION"

# --- base packages (Amazon Linux 2023) --------------------------------------
dnf -y update
dnf -y install docker jq unzip curl-minimal
if ! command -v aws >/dev/null 2>&1; then
  dnf -y install awscli || true
fi
if ! docker compose version >/dev/null 2>&1; then
  mkdir -p /usr/local/lib/docker/cli-plugins /usr/libexec/docker/cli-plugins
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose
  chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  ln -sfn /usr/local/lib/docker/cli-plugins/docker-compose /usr/libexec/docker/cli-plugins/docker-compose
fi

systemctl enable --now docker
usermod -aG docker ec2-user || true

# IMDSv2 only is enforced by the Launch Template; block containers from IMDS.
# Hop limit is 1 on the instance, so docker bridge cannot reach 169.254.169.254.
iptables -C DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null \
  || iptables -I DOCKER-USER -d 169.254.169.254/32 -j DROP

# --- data volume -------------------------------------------------------------
# Nitro remaps /dev/xvdf -> /dev/nvme1n1 (or similar). Prefer an explicit
# device if present, otherwise the first unmounted NVMe disk that is not root.
resolve_data_device() {
  if [ -n "$DATA_DEVICE" ] && [ -b "$DATA_DEVICE" ]; then
    printf '%s' "$DATA_DEVICE"
    return
  fi
  for cand in /dev/nvme1n1 /dev/xvdf /dev/sdf; do
    if [ -b "$cand" ]; then
      printf '%s' "$cand"
      return
    fi
  done
  # Last resort: first loop of lsblk disks excluding the root disk.
  root_src=$(findmnt -n -o SOURCE / | sed 's/[0-9]*$//')
  lsblk -dn -o NAME,TYPE | awk '$2=="disk"{print "/dev/"$1}' | while read -r d; do
    if [ "$d" != "$root_src" ] && [ -b "$d" ]; then
      printf '%s' "$d"
      break
    fi
  done
}

DATA_DEVICE="$(resolve_data_device || true)"
if [ -n "$DATA_DEVICE" ] && [ -b "$DATA_DEVICE" ]; then
  if ! blkid "$DATA_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$DATA_DEVICE"
  fi
  mkdir -p /var/lib/robomp-data
  grep -q "/var/lib/robomp-data" /etc/fstab || echo "$DATA_DEVICE /var/lib/robomp-data ext4 defaults,nofail,x-systemd.device-timeout=30 0 2" >> /etc/fstab
  mount -a || mount /var/lib/robomp-data
fi
mkdir -p /var/lib/robomp-data/robomp_data
mkdir -p "$COMPOSE_DIR" "$CONFIG_DIR/rules"

# --- fetch secrets (ONLY AWS API this host is allowed to call) --------------
for attempt in $(seq 1 30); do
  if SECRET_JSON=$(aws secretsmanager get-secret-value \
      --secret-id "$ROBOMP_SECRET_ARN" \
      --query SecretString \
      --output text 2>/tmp/robomp-secret.err); then
    break
  fi
  echo "[robomp] waiting for secret ($attempt/30)"
  sleep 5
done
if [ -z "${SECRET_JSON:-}" ]; then
  echo "[robomp] failed to read secret" >&2
  cat /tmp/robomp-secret.err >&2 || true
  exit 1
fi

json_get() {
  jq -r --arg k "$1" 'if has($k) and .[$k] != null and .[$k] != "" then .[$k] else empty end' <<<"$SECRET_JSON"
}
require_json() {
  local v
  v=$(json_get "$1" || true)
  if [ -z "$v" ]; then
    echo "[robomp] secret missing required key: $1" >&2
    exit 1
  fi
  printf '%s' "$v"
}

GITHUB_TOKEN=$(require_json GITHUB_TOKEN)
GITHUB_WEBHOOK_SECRET=$(require_json GITHUB_WEBHOOK_SECRET)
ROBOMP_GH_PROXY_HMAC_KEY=$(require_json ROBOMP_GH_PROXY_HMAC_KEY)
ROBOMP_BOT_LOGIN=$(require_json ROBOMP_BOT_LOGIN)
ROBOMP_GIT_AUTHOR_EMAIL=$(require_json ROBOMP_GIT_AUTHOR_EMAIL)
ROBOMP_REPO_ALLOWLIST=$(require_json ROBOMP_REPO_ALLOWLIST)
LITELLM_MASTER_KEY=$(require_json LITELLM_MASTER_KEY)
GHCR_USERNAME=$(require_json GHCR_USERNAME)
GHCR_TOKEN=$(require_json GHCR_TOKEN)

ROBOMP_GIT_AUTHOR_NAME=$(json_get ROBOMP_GIT_AUTHOR_NAME || true)
ROBOMP_MAINTAINER_LOGINS=$(json_get ROBOMP_MAINTAINER_LOGINS || true)
ROBOMP_REVIEWER_BOTS=$(json_get ROBOMP_REVIEWER_BOTS || true)
ROBOMP_REPLAY_TOKEN=$(json_get ROBOMP_REPLAY_TOKEN || true)
ROBOMP_MODEL=$(json_get ROBOMP_MODEL || true)
ROBOMP_THINKING=$(json_get ROBOMP_THINKING || true)
ANTHROPIC_API_KEY=$(json_get ANTHROPIC_API_KEY || true)
OPENAI_API_KEY=$(json_get OPENAI_API_KEY || true)
AZURE_API_KEY=$(json_get AZURE_API_KEY || true)
GROQ_API_KEY=$(json_get GROQ_API_KEY || true)

: "${ROBOMP_GIT_AUTHOR_NAME:=robomp}"
: "${ROBOMP_MODEL:=anthropic/claude-sonnet-4-6}"
: "${ROBOMP_THINKING:=high}"

# --- materialize config ------------------------------------------------------
umask 077
cat > /etc/robomp/.env <<EOF
GITHUB_WEBHOOK_SECRET=${GITHUB_WEBHOOK_SECRET}
ROBOMP_BOT_LOGIN=${ROBOMP_BOT_LOGIN}
ROBOMP_GIT_AUTHOR_NAME=${ROBOMP_GIT_AUTHOR_NAME}
ROBOMP_GIT_AUTHOR_EMAIL=${ROBOMP_GIT_AUTHOR_EMAIL}
ROBOMP_REPO_ALLOWLIST=${ROBOMP_REPO_ALLOWLIST}
ROBOMP_MAINTAINER_LOGINS=${ROBOMP_MAINTAINER_LOGINS}
ROBOMP_REVIEWER_BOTS=${ROBOMP_REVIEWER_BOTS}
ROBOMP_GH_PROXY_HMAC_KEY=${ROBOMP_GH_PROXY_HMAC_KEY}
ROBOMP_GH_PROXY_URL=http://gh-proxy:8081
GITHUB_TOKEN=${GITHUB_TOKEN}
ROBOMP_MODEL=${ROBOMP_MODEL}
ROBOMP_THINKING=${ROBOMP_THINKING}
ROBOMP_PR_REVIEW_ENABLED=true
ROBOMP_REPLAY_TOKEN=${ROBOMP_REPLAY_TOKEN}
ROBOMP_IMAGE=${ROBOMP_IMAGE}
LITELLM_IMAGE=${LITELLM_IMAGE}
LITELLM_MASTER_KEY=${LITELLM_MASTER_KEY}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
OPENAI_API_KEY=${OPENAI_API_KEY}
AZURE_API_KEY=${AZURE_API_KEY}
GROQ_API_KEY=${GROQ_API_KEY}
EOF
chmod 0600 /etc/robomp/.env

# Compose files + static assets are written by the CDK user-data preamble
# into $COMPOSE_DIR and $CONFIG_DIR before this script runs.

if [ -f "$CONFIG_DIR/models.container.yml.tmpl" ]; then
  sed "s|__LITELLM_MASTER_KEY__|${LITELLM_MASTER_KEY}|g" \
    "$CONFIG_DIR/models.container.yml.tmpl" > "$CONFIG_DIR/models.container.yml"
  chmod 0644 "$CONFIG_DIR/models.container.yml"
fi

# Ensure external volume exists as bind to EBS.
if ! docker volume inspect robomp_robomp_data >/dev/null 2>&1; then
  docker volume create \
    --driver local \
    --opt type=none \
    --opt device=/var/lib/robomp-data/robomp_data \
    --opt o=bind \
    robomp_robomp_data
fi

echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin

cd "$COMPOSE_DIR"
export COMPOSE_PROJECT_NAME=robomp
docker compose \
  --env-file /etc/robomp/.env \
  -f docker-compose.aws.yml \
  pull
docker compose \
  --env-file /etc/robomp/.env \
  -f docker-compose.aws.yml \
  up -d

# systemd unit for restart resilience
cat > /etc/systemd/system/robomp.service <<EOF
[Unit]
Description=robomp docker compose stack
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${COMPOSE_DIR}
Environment=COMPOSE_PROJECT_NAME=robomp
ExecStart=/usr/bin/docker compose --env-file /etc/robomp/.env -f docker-compose.aws.yml up -d
ExecStop=/usr/bin/docker compose --env-file /etc/robomp/.env -f docker-compose.aws.yml stop
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable robomp.service

# Scrub secret material from shell history / temp
unset SECRET_JSON GITHUB_TOKEN GHCR_TOKEN LITELLM_MASTER_KEY ANTHROPIC_API_KEY OPENAI_API_KEY
rm -f /tmp/robomp-secret.err

echo "[robomp] bootstrap complete $(date -u +%FT%TZ)"
