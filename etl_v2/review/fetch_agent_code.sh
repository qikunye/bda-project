#!/bin/bash
# Copies /app (src, scripts, sql) out of the agent's ECR image without Docker, into ~/agent.
set -e
R=is459-agent-batch
M=$(aws ecr batch-get-image --repository-name $R --image-ids imageTag=latest --query 'images[0].imageManifest' --output text)
mkdir -p ~/agent /tmp/layers
for D in $(echo "$M" | python3 -c "import json,sys;print(' '.join(l['digest'] for l in json.load(sys.stdin)['layers'][-5:]))"); do
  U=$(aws ecr get-download-url-for-layer --repository-name $R --layer-digest $D --query downloadUrl --output text)
  curl -sL "$U" -o /tmp/layers/l.tgz
  tar -xzf /tmp/layers/l.tgz -C ~/agent --wildcards 'app/*' 2>/dev/null || true
done
ls ~/agent/app; find ~/agent/app -name '*.py' | wc -l
