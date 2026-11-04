# HomenS.AI · © 2026 Serhii Khomenko — сборщик комплектов с веб-страницей (docs/WEB.md).
# Контекст сборки — корень модуля установки (см. compose.yml рядом и .dockerignore в корне).
FROM docker.io/library/python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /opt/homensai

# cryptography для подписи — только закреплённые двоичные колёса (docs/CRYPTO-LOCK.md).
COPY requirements-crypto.txt ./
RUN python -m pip install --no-cache-dir --root-user-action=ignore --require-hashes --only-binary=:all: -r requirements-crypto.txt

COPY shared ./shared
COPY homensai_installer_client ./homensai_installer_client

RUN useradd --uid 1000 --create-home builder \
    && mkdir -p /work/projects /work/keys /work/output \
    && chown builder:builder /work/projects /work/keys /work/output \
    && chmod 0700 /work/keys
USER builder

EXPOSE 8790
ENTRYPOINT ["python", "-m", "shared.kit_builder_web", "--host", "0.0.0.0", "--port", "8790", \
            "--projects", "/work/projects", "--keys", "/work/keys", "--output", "/work/output"]
