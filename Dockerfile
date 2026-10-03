# FailGate 镜像（ADR 0044）：api 和 sandbox worker 共用。
# 沙箱容器由宿主机的 Docker 引擎跑：镜像里只放 docker 命令行，通过挂进来的 docker.sock 调用。
FROM docker:27-cli AS docker-cli

FROM python:3.12-slim
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

WORKDIR /app
COPY pyproject.toml README.md ./
COPY failgate ./failgate
RUN pip install --no-cache-dir . && mkdir -p /data/artifacts

ENV PYTHONUNBUFFERED=1 \
    SANDBOX_ARTIFACTS_DIR=/data/artifacts
EXPOSE 8080
CMD ["failgate", "serve", "--host", "0.0.0.0", "--port", "8080"]
