FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client telnet && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agenticops/ ./agenticops/

RUN mkdir -p /app/logs /app/backups

ENV OPENROUTER_MODEL="nvidia/nemotron-3-ultra-550b-a55b:free" \
    LLM_REDACT_SECRETS="true" \
    LOG_LEVEL="WARNING" \
    PYTHONUNBUFFERED="1"

ENTRYPOINT ["python3", "agenticops/agent.py"]
