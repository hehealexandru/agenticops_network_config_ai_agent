#!/bin/bash
docker run -it --rm --network host \
  --name AgenticOps \
  -e OPENROUTER_API_KEY="${OPENROUTER_API_KEY}" \
  -e OPENROUTER_MODEL="${OPENROUTER_MODEL:-nvidia/nemotron-3-super-120b-a12b:free}" \
  agenticops
