# One image, three roles. The role is chosen by the container args:
#   python -m nanoclaw controller | agent | vault
# Pin the base by digest for production with scripts/pin-digests.sh.
ARG PYTHON_VERSION=3.13.18

FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes --target /opt/deps -r requirements.txt
COPY src/ /opt/app/

FROM python:${PYTHON_VERSION}-slim
LABEL org.opencontainers.image.source="https://github.com/djkidnyce/nanoclaw-warden-cinder" \
      org.opencontainers.image.title="nanoclaw-warden-cinder runtime" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/app:/opt/deps
COPY --from=build /opt/deps /opt/deps
COPY --from=build /opt/app /opt/app
# Numeric non root user so Pod Security "restricted" can verify it without a passwd lookup.
USER 65532:65532
ENTRYPOINT ["python", "-m", "nanoclaw"]
CMD ["agent"]
