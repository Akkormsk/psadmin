FROM python:3.14-slim

# ==== build args для совместимости (если вдруг где-то используются) ====
ARG DB_ENGINE
ARG DB_NAME
ARG DB_USER
ARG DB_PASSWORD
ARG DB_HOST
ARG DB_PORT

# ==== базовые настройки Python ====
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# ==== системные зависимости (для PostgreSQL и сборки) ====
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ==== обновляем pip ====
RUN pip install --no-cache-dir --upgrade pip

# ==== создаём пользователя ====
RUN groupadd --gid 2000 app && useradd --uid 2000 --gid 2000 -m -d /app app

# ==== рабочая директория ====
WORKDIR /app

# ==== сначала только requirements, чтобы кешировалась установка пакетов ====
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# ==== теперь копируем весь исходный код ====
COPY --chown=app:app . .

# ==== переходим под непривилегированного пользователя ====
USER app

# ==== по умолчанию запускаем Django dev-сервер ====
CMD ["python3", "manage.py", "runserver", "0.0.0.0:8000"]
