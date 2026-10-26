# -----------------------------------------------------------
# 阶段 1：构建环境 (Builder)
# 使用基于 Debian 的 slim 镜像，完美兼容 Python 的 glibc 预编译包
# -----------------------------------------------------------
FROM python:3.11-slim AS builder

# 安装构建依赖 (C 编译器和 PostgreSQL 开发库)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

# 创建并激活虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 极速安装依赖 (Slim 镜像会直接下载 .whl 预编译包，不会再报错了！)
RUN pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------
# 阶段 2：运行环境 (Production)
# -----------------------------------------------------------
FROM python:3.11-slim

# 安装纯运行时的依赖库 (只装 libpq5 运行库，不要 gcc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 🚨 Debian 系统的安全加固：创建非 root 用户
RUN groupadd -r appgroup && useradd -r -g appgroup appuser

WORKDIR /app

# 从 Builder 阶段把装好依赖的虚拟环境直接复制过来
COPY --from=builder /opt/venv /opt/venv

# 拷贝项目代码，并赋予非特权用户权限
COPY --chown=appuser:appgroup ./app ./app
COPY --chown=appuser:appgroup ./company_handbook.txt ./company_handbook.txt
COPY --chown=appuser:appgroup ./company_knowledge ./company_knowledge

ENV PATH="/opt/venv/bin:$PATH"

EXPOSE 8000

# 切换到非特权用户运行
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
