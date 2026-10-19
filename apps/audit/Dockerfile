# Audit sink image. Book: Sovereign AI Stacks, Chapter 10
# Build on the connected side; the image travels in the Zarf package and is
# signed at the enclave import station (platform/security/sign_imported_images.sh).
# Pin the base by digest at build time (docker buildx imagetools inspect) and
# record it in VERSIONS.md: "3.13-slim-trixie" moves with each patch release.
#   docker build -t ghcr.io/prosperityai/audit-sink:1.0.0 apps/audit
FROM docker.io/library/python:3.13-slim-trixie AS build
WORKDIR /src
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install .

FROM docker.io/library/python:3.13-slim-trixie
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=build /install /usr/local
# The schema ships with the image so the version that writes is the version
# that was reviewed; audit-sink.yaml loads it into CloudNativePG.
COPY migrations /opt/audit/migrations
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin audit
USER 10001
EXPOSE 8080
ENTRYPOINT ["uvicorn", "audit.sink:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
            "--no-server-header", "--no-access-log"]
