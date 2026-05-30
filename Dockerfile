FROM ubuntu:24.04

# Отключаем интерактивные диалоги apt
ENV DEBIAN_FRONTEND=noninteractive

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y \
    software-properties-common \
    python3.12 \
    python3.12-dev \
    python3-pip \
    sqlite3 \
    iptables \
    iproute2 \
    curl \
    && add-apt-repository -y ppa:amnezia/ppa \
    && apt-get update \
    && apt-get install -y amneziawg-tools \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Создаем рабочую директорию
WORKDIR /app

# Копируем список зависимостей
COPY requirements.txt .

# Устанавливаем python-библиотеки глобально внутри контейнера
RUN pip3 install --no-cache-dir --ignore-installed --break-system-packages -r requirements.txt

# Копируем исходный код проекта
COPY app/ /app/app/

# Настройки окружения по умолчанию
ENV PANEL_PORT=8080
ENV PYTHONUNBUFFERED=1

# Открываем порт для документации Docker
EXPOSE 8080

# Запускаем FastAPI через Uvicorn с динамическим портом
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PANEL_PORT}"]
