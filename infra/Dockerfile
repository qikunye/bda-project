# Runs the agent batch on Fargate. Build from bda-project/:  docker build -f infra/Dockerfile .
FROM python:3.12-slim

WORKDIR /app
COPY infra/requirements-batch.txt .
RUN pip install --no-cache-dir -r requirements-batch.txt

COPY src ./src
COPY scripts ./scripts
COPY sql ./sql

ENV PYTHONUNBUFFERED=1 AWS_DEFAULT_REGION=us-east-1
# The state machine overrides the command with the real day. Running the image by hand shows the options.
CMD ["python", "-m", "scripts.run_batch", "--help"]
