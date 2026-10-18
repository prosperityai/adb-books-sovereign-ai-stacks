# Sovereign RAG API image. Book: Sovereign AI Stacks, Chapter 8
# Build on the connected side; the image travels in the Zarf package.
# Pin the base by digest at build time (docker buildx imagetools inspect) and
# record it in VERSIONS.md: "3.13-slim-trixie" moves with each patch release.
#   docker build -t ghcr.io/prosperityai/sovereign-rag-api:1.0.0 apps/rag
FROM docker.io/library/python:3.13-slim-trixie AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM docker.io/library/python:3.13-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin rag
USER 10001
EXPOSE 8000
ENTRYPOINT ["uvicorn", "rag.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
