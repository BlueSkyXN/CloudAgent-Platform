# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PORT=7860 \
    CLOUDAGENT_HFS_MODE=deployment-probe \
    CLOUDAGENT_SDLC_STATUS=review-ready-draft

WORKDIR /app

COPY app.py /app/app.py
COPY healthcheck.sh /app/healthcheck.sh
COPY hfs-dev.toml /app/hfs-dev.toml
COPY README.md /app/HFS_README.md
COPY BUILD_SOURCE.txt /app/BUILD_SOURCE.txt
COPY BUNDLE_MANIFEST.json /app/BUNDLE_MANIFEST.json

RUN chmod +x /app/healthcheck.sh

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD /app/healthcheck.sh || exit 1

CMD ["python", "/app/app.py"]
