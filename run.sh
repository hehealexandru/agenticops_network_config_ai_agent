#!/bin/bash
docker run -it --rm --network host \
  --name AgenticOps \
  -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" \
  -e OPENROUTER_MODEL="${OPENROUTER_MODEL:-openai/gpt-oss-120b:free}" \
  -e GNS3_HOST="host.docker.internal" \
  -v $(pwd)/docs:/app/docs \
  agenticops
