# syntax=docker/dockerfile:1
#
# Arena-Agents 运行镜像（v0.11）
#
# 基础镜像固定到具体小版本（python:3.12.15-slim-bookworm）。如需按内容寻址再固定 digest：
#   FROM python:3.12.15-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
# （digest 取自 2026-11-14 的 Docker Hub 多架构 manifest，升级基础镜像时请一并更新。）
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 非 root 运行。固定 uid/gid=10001，便于在宿主机上给绑定挂载目录/密钥文件授权：
#   sudo chown -R 10001:10001 /srv/arena-data
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid ${APP_GID} arena \
    && useradd --system --uid ${APP_UID} --gid arena --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin arena

WORKDIR /app

# 依赖：用 requirements.lock（完全固定版本）保证构建可复现。
COPY requirements.lock ./
RUN pip install -r requirements.lock

# 应用文件（代码与只读资产）。属主保持 root、arena 只读：v0.10 起运行时不会在 /app 下写任何东西
# （Provider 密钥、设置、对局存档全部写入外部数据目录 /data）。
# 只复制运行所需目录；.env、saves/、tests/、docs/、本机缓存等由 .dockerignore 排除。
COPY config.yml ./
COPY knowledge/ ./knowledge/
COPY scenarios/ ./scenarios/
COPY prompts/ ./prompts/
COPY static/ ./static/
COPY app/ ./app/

# 外部数据目录 /data（加密的 Provider 库、settings.json、加密对局存档、日志）。
# 必须由部署方在运行时挂载，且必须对 arena(uid 10001) 可写：
#   * 使用 compose 默认的命名卷：首次创建时继承这里的属主(10001)与 0700 权限，无需额外操作；
#   * 使用宿主机目录绑定挂载：Linux 上需先 `sudo chown -R 10001:10001 <目录> && chmod 700 <目录>`；
#     Docker Desktop（Windows/macOS）通常不需要。
# 主密钥不在镜像里、也不放在 /data：通过 Docker Secret（compose 设置 ARENA_MASTER_KEY_FILE=/run/secrets/...）
# 或只读挂载的密钥文件提供。
# 未提供主密钥时应用会 fail closed（拒绝写入加密数据），不会以明文静默回退。
RUN mkdir -p /data && chown arena:arena /data && chmod 700 /data
ENV ARENA_DATA_DIR=/data
# v0.11: all runtime state (providers, templates, bindings, sessions) lives in a workspace.
# Compose normally pins this to /data/workspace/default; it must stay inside the writable /data volume.
ENV ARENA_WORKSPACE_DIR=/data/workspace/default
VOLUME ["/data"]

USER arena

# 容器内固定监听 0.0.0.0:8000；对外暴露的地址/端口由 compose 的端口映射控制
# （默认只绑定宿主机 127.0.0.1）。注意：容器内的 HOST/PORT 环境变量不影响 uvicorn。
EXPOSE 8000

# 轻量健康检查：GET /api/health（免鉴权、不含敏感信息，由后端提供）。
# slim 镜像没有 curl，这里用 Python 标准库。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys,urllib.request as u; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"]

# --timeout-graceful-shutdown：SSE 长连接不会阻塞退出；超时后 lifespan 才会执行
# “暂停对局 → 刷新自动保存 → 关闭 LLM 客户端”。配合 compose 的 stop_grace_period。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
