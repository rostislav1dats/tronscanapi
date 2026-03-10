# =============================================================================
# Tron Wallet Service — Dockerfile
#
# Multi-stage сборка:
#   builder  — устанавливает зависимости в venv
#   tester   — запускает тесты (собирается всегда, прерывает сборку при fail)
#   runtime  — финальный образ без тестовых зависимостей
#
# Использование:
#   docker build -t tron-wallet-service .          # сборка (тесты запустятся)
#   docker run -p 8000:8000 tron-wallet-service    # запуск
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: builder — устанавливаем все зависимости в изолированный venv
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /app

# Системные зависимости для сборки пакетов
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Создаём виртуальное окружение
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Копируем и устанавливаем зависимости отдельным слоем (кэшируется)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: tester — запускаем тесты
# Если тесты упали — docker build завершается с ошибкой.
# -----------------------------------------------------------------------------
FROM builder AS tester

WORKDIR /app

# Копируем весь проект (включая tests/)
COPY . .

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"

# Переменные окружения для тестов (нет реальных ключей — тесты мокают всё)
ENV TRONGRID_API_KEY=test-key-for-ci
ENV SERVICE_MASTER_KEY=test-master-key-for-ci-only
ENV LOG_LEVEL=WARNING

# Запускаем тесты — при провале слой не будет закэширован и сборка упадёт
RUN pytest tests/ \
    --tb=short \
    --no-header \
    -q \
    && echo "✅ Все тесты прошли успешно"

# -----------------------------------------------------------------------------
# Stage 3: runtime — финальный минимальный образ
# Копируем только venv и код приложения, без тестов и build-tools
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Создаём непривилегированного пользователя
RUN useradd --no-create-home --shell /bin/false appuser

# Копируем venv из builder (без тестовых инструментов не нужно — они там есть,
# но образ всё равно slim; при необходимости разделите requirements на prod/dev)
COPY --from=builder /opt/venv /opt/venv

# Копируем только код приложения (без tests/)
COPY app/ ./app/

ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONPATH="/app"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Переключаемся на непривилегированного пользователя
USER appuser

EXPOSE 8000

# Healthcheck для Docker / оркестраторов
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" \
    || exit 1

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]