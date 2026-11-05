#!/bin/bash
args=(-it --rm --network host --name AgenticOps)

if [ -f .env ]; then
  args+=(--env-file .env)
fi

for var in LLM_PROVIDERS OPENROUTER_API_KEY OPENROUTER_MODEL \
           NVIDIA_API_KEY NVIDIA_MODEL MISTRAL_API_KEY MISTRAL_MODEL \
           LLM_REDACT_SECRETS LOG_LEVEL; do
  if [ -n "${!var}" ]; then
    args+=(-e "$var")
  fi
done

if [ -d credentials.json ]; then
  echo "credentials.json is a directory (left by an earlier docker run)." >&2
  echo "Remove it with: rmdir credentials.json" >&2
  exit 1
fi
if [ ! -f credentials.json ]; then
  (umask 077 && echo '{}' > credentials.json)
fi

docker run "${args[@]}" \
  -e GNS3_HOST="host.docker.internal" \
  -v "$(pwd)/docs:/app/docs" \
  -v "$(pwd)/credentials.json:/app/credentials.json" \
  agenticops
