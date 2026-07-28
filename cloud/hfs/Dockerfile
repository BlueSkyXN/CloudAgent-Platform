# syntax=docker/dockerfile:1

FROM python:3.12-slim

ARG CLOUDAGENT_SOURCE_REF=a82b11d9a13b382c0f903eb83314d260a28aee2e

ENV PYTHONUNBUFFERED=1 \
    PORT=7860 \
    CLOUDAGENT_SOURCE_ROOT=/opt/cloudagent \
    CLOUDAGENT_SOURCE_REF=${CLOUDAGENT_SOURCE_REF} \
    CLOUDAGENT_SDLC_STATUS=review-ready-draft

WORKDIR /app

RUN python3 -c 'import re, sys; assert re.fullmatch(r"[0-9a-f]{40}", sys.argv[1]), "CLOUDAGENT_SOURCE_REF must be a full lowercase Git commit SHA"' "$CLOUDAGENT_SOURCE_REF" \
    && apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN git init "${CLOUDAGENT_SOURCE_ROOT}" \
    && git -C "${CLOUDAGENT_SOURCE_ROOT}" remote add origin https://github.com/BlueSkyXN/CloudAgent-Platform.git \
    && git -C "${CLOUDAGENT_SOURCE_ROOT}" fetch --depth=1 origin "${CLOUDAGENT_SOURCE_REF}" \
    && git -C "${CLOUDAGENT_SOURCE_ROOT}" checkout --detach FETCH_HEAD \
    && test "$(git -C "${CLOUDAGENT_SOURCE_ROOT}" rev-parse HEAD)" = "${CLOUDAGENT_SOURCE_REF}" \
    && test -f "${CLOUDAGENT_SOURCE_ROOT}/pyproject.toml" \
    && test -f "${CLOUDAGENT_SOURCE_ROOT}/src/cloudagent_platform/app.py"

COPY start.sh /app/start.sh
COPY healthcheck.sh /app/healthcheck.sh
COPY hfs-dev.toml /app/hfs-dev.toml
COPY validate_persistent_path.py /app/validate_persistent_path.py
COPY README.md /app/HFS_README.md
COPY BUILD_SOURCE.txt /app/BUILD_SOURCE.txt
COPY BUNDLE_MANIFEST.json /app/BUNDLE_MANIFEST.json

RUN python3 -c 'import json, pathlib, sys; expected = sys.argv[1]; build = dict(line.split("=", 1) for line in pathlib.Path("/app/BUILD_SOURCE.txt").read_text(encoding="utf-8").splitlines() if "=" in line); assert build.get("lane") == "source"; assert build.get("version_source") == "commit"; assert build.get("source_commit") == expected; assert json.loads(pathlib.Path("/app/BUNDLE_MANIFEST.json").read_text(encoding="utf-8")).get("source_commit") == expected' "${CLOUDAGENT_SOURCE_REF}" \
    && chmod +x /app/start.sh /app/healthcheck.sh

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/app/healthcheck.sh"]

CMD ["/app/start.sh"]
