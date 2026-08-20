#!/bin/bash
# Deploy the Robomp-prod stack with the context the live stack uses.
#
# Usage:
#   ./deploy.sh                        # deploy :latest-known-good tag below
#   ./deploy.sh 5093653                # deploy ghcr.io/broadlume/robomp:<tag>
#   ./deploy.sh 5093653 --nonce        # also force an instance replacement
#   ./deploy.sh --diff [tag]           # cdk diff instead of deploy
#
# Secret-only changes do NOT need this script: robomp-config.service re-reads
# the secret on every boot, so `aws ec2 reboot-instances` is enough.
set -euo pipefail
cd "$(dirname "$0")"

PROFILE=BroadlumeRoot
ACCOUNT=499231742659
REGION=us-west-2
CERT_ARN=arn:aws:acm:us-west-2:499231742659:certificate/34b49648-40bb-45fe-b897-a1073e1097cf
DASHBOARD_CIDRS=184.72.133.8/32,38.81.235.16/32
DEFAULT_TAG=5093653

CMD=deploy
TAG=$DEFAULT_TAG
NONCE_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --diff) CMD=diff ;;
    --nonce) NONCE_ARGS=(-c "provisionNonce=$(date -u +%Y%m%dT%H%M%SZ)") ;;
    *) TAG=$arg ;;
  esac
done

EXTRA_ARGS=()
if [ "$CMD" = deploy ]; then
  EXTRA_ARGS=(--require-approval never --progress events)
fi

echo "[deploy] $CMD Robomp-prod with ghcr.io/broadlume/robomp:$TAG"
AWS_PROFILE=$PROFILE CDK_DEFAULT_ACCOUNT=$ACCOUNT CDK_DEFAULT_REGION=$REGION \
  ./node_modules/.bin/cdk "$CMD" \
  --profile "$PROFILE" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"} \
  -c "certificateArn=$CERT_ARN" \
  -c "robompImage=ghcr.io/broadlume/robomp:$TAG" \
  -c envName=prod \
  -c "dashboardAllowCidrs=$DASHBOARD_CIDRS" \
  ${NONCE_ARGS[@]+"${NONCE_ARGS[@]}"}
