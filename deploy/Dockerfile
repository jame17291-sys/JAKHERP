# syntax=docker/dockerfile:1.7
FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/jakh-runtime/vendor \
    PATH="/opt/jakh-rtlcss/node_modules/.bin:${PATH}"
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 libldap-2.5-0 libsasl2-2 libssl3 libmagic1 \
    fonts-dejavu-core fonts-noto-core ca-certificates nodejs chromium chromium-sandbox \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 101 odoo && useradd --uid 101 --gid 101 --home-dir /var/lib/jakh-odoo --no-create-home odoo
WORKDIR /opt/jakh

# Full vendor source and compilers exist only in this throw-away build stage.
FROM runtime-base AS source-builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev libldap2-dev libsasl2-dev libssl-dev npm \
    && rm -rf /var/lib/apt/lists/*
COPY vendor.lock.json ./vendor.lock.json
COPY LICENSE COPYRIGHT ./
COPY scripts ./scripts
COPY addons ./addons
COPY deploy/runtime-modules.json ./deploy/runtime-modules.json
COPY deploy/requirements-jakh.lock.txt ./deploy/requirements-jakh.lock.txt
COPY deploy/rtlcss/package.json deploy/rtlcss/package-lock.json /opt/jakh-rtlcss/
COPY deploy/rtlcss/vendor /opt/jakh-rtlcss/vendor
COPY deploy/rtlcss/provenance /opt/jakh-rtlcss/provenance
# Reviewed official tagged sources, packaged locally with a real npm integrity lock.
# Compiler installation needs no registry; the whole Docker build remains untested here.
RUN python scripts/install_rtlcss.py --bundle /opt/jakh-rtlcss --verify-only \
    && npm ci --prefix /opt/jakh-rtlcss --offline --ignore-scripts --omit=dev --no-audit --no-fund \
    && /opt/jakh-rtlcss/node_modules/.bin/rtlcss --version
ADD --checksum=sha256:53b2b33a58b9e0f743d2076b01b5a955a867c044de232aed109d838e45018961 \
    https://nightly.odoo.com/20.0/nightly/src/odoo_20.0.20261012.tar.gz /tmp/odoo.tar.gz
RUN python scripts/prepare_source.py --archive /tmp/odoo.tar.gz --destination /opt/odoo-source \
    && chmod 755 /opt/odoo-source \
    && pip install --no-cache-dir -r /opt/odoo-source/odoo-20.0.post20261012/requirements.txt \
    && pip install --no-cache-dir --no-deps --only-binary=:all: --require-hashes -r /opt/jakh/deploy/requirements-jakh.lock.txt \
    && python scripts/build_lean_runtime.py \
        --source /opt/odoo-source/odoo-20.0.post20261012 --addons /opt/jakh/addons \
        --profile /opt/jakh/deploy/runtime-modules.json --destination /opt/jakh-runtime \
    && rm /tmp/odoo.tar.gz

FROM runtime-base AS application
COPY --from=source-builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=source-builder /usr/local/bin /usr/local/bin
COPY --from=source-builder /opt/jakh-runtime /opt/jakh-runtime
COPY --from=source-builder /opt/jakh-rtlcss /opt/jakh-rtlcss
COPY LICENSE COPYRIGHT vendor.lock.json ./
COPY scripts ./scripts
COPY deploy/container_entrypoint.py ./deploy/container_entrypoint.py
EXPOSE 8069
# The small entrypoint reads root-only secrets and initializes volume ownership, then
# irreversibly drops to UID/GID 101 before importing or executing Odoo.
ENTRYPOINT ["python", "/opt/jakh/deploy/container_entrypoint.py"]
CMD ["serve"]
