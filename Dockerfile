# deco-exporter — Prometheus exporter for per-client traffic on a TP-Link Deco
# mesh, read from the main Deco's local admin API (homelab#1931).
#
# Dependencies install from a fully hashed lock (requirements.txt), wheels
# only. `--only-binary :all:` matters: the lock also carries the sdist hash
# uv emits for tplinkrouterc6u, and refusing sdists means the only artifact
# pip can accept is the wheel vetted in AC1 (sha256 cf50956d…).
FROM python:3.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python3 -m pip install --no-cache-dir --require-hashes --only-binary :all: \
        -r /app/requirements.txt && \
    # pip is build-time only; its vendored packages carry CVEs (homelab#1697).
    python3 -m pip uninstall -y pip

COPY src/ /app/src/

# Same uid the Deployment runs as; nothing is written at runtime, so the pod
# can use readOnlyRootFilesystem without even a /tmp mount.
USER 1000:1000

EXPOSE 9850

ENTRYPOINT ["python3", "-m", "deco_exporter.main"]
