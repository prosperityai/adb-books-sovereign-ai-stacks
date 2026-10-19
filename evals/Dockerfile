# Evaluation harness image. Book: Sovereign AI Stacks, Chapter 11
# Build on the connected side; the image travels in the Zarf package like every
# other image. The golden sets are baked in, so the dataset hashes in every
# run.json identify the image that produced it. Pin the base by digest at build
# time (docker buildx imagetools inspect) and record it in VERSIONS.md.
#   docker build -t ghcr.io/prosperityai/sovereign-evals:1.0.0 evals
FROM docker.io/library/python:3.13-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/evals
COPY pyproject.toml harness.py ./
COPY evalkit ./evalkit
RUN pip install --no-cache-dir . && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin evals
COPY datasets ./datasets
COPY redteam ./redteam
COPY acceptance ./acceptance
USER 10001
ENTRYPOINT ["python", "/opt/evals/harness.py"]
