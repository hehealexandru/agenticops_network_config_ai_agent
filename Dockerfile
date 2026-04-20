FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends openssh-client telnet && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agenticops/ ./agenticops/

RUN mkdir -p /app/logs /app/backups

ENV OPENROUTER_API_KEY=""
ENV OPENROUTER_MODEL="nvidia/nemotron-3-super-120b-a12b:free"

ENTRYPOINT ["python3", "agenticops/agent.py"]
