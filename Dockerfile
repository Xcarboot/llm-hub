FROM public.ecr.aws/docker/library/python:3.12-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip wheel --no-cache-dir --wheel-dir /wheels .

FROM public.ecr.aws/docker/library/python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN useradd --create-home --uid 10001 gateway
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY alembic.ini ./
COPY alembic ./alembic
COPY litellm_config.yaml ./
USER gateway
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
