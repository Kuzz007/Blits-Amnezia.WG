#!/bin/bash
# Скрипт установки AmneziaWG для AmneziaWG Web Panel MVP
set -e

echo "=== УСТАНОВКА AMNEZIAWG ==="

# Проверка на root
if [ "$EUID" -ne 0 ]; then
  echo "Ошибка: Этот скрипт должен быть запущен от имени root." >&2
  exit 1
fi

# Проверка подтверждения
if [ "$1" != "--confirm" ]; then
  echo "ВНИМАНИЕ! Этот скрипт внесет следующие изменения в вашу систему:"
  echo "1. Добавит PPA-репозиторий ppa:amnezia/ppa"
  echo "2. Установит пакеты amneziawg и amneziawg-tools"
  echo "3. Включит форвардинг IPv4/IPv6 пакетов (sysctl net.ipv4.ip_forward=1)"
  echo "4. Автоматически определит публичный сетевой интерфейс"
  echo "5. Создаст базовый конфигурационный файл /etc/amnezia/amneziawg/awg0.conf"
  echo ""
  echo "Если вы согласны, запустите скрипт с флагом: $0 --confirm"
  exit 0
fi

# Проверяем, установлен ли уже awg
if command -v awg >/dev/null 2>&1; then
  echo "AmneziaWG (awg) уже установлен в системе. Пропускаем шаг установки пакетов."
else
  echo "Обновление пакетного менеджера и установка базовых утилит..."
  apt-get update
  apt-get install -y software-properties-common iptables ufw curl

  echo "Добавление PPA репозитория AmneziaWG..."
  add-apt-repository -y ppa:amnezia/ppa
  apt-get update

  echo "Установка amneziawg..."
  apt-get install -y amneziawg
fi

# Включение форвардинга пакетов
echo "Включение форвардинга IPv4 и IPv6 пакетов..."
sysctl -w net.ipv4.ip_forward=1
sysctl -w net.ipv6.conf.all.forwarding=1

# Сохранение в sysctl.conf
if ! grep -q "net.ipv4.ip_forward=1" /etc/sysctl.conf; then
  echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
fi
if ! grep -q "net.ipv6.conf.all.forwarding=1" /etc/sysctl.conf; then
  echo "net.ipv6.conf.all.forwarding=1" >> /etc/sysctl.conf
fi

# Определение основного сетевого интерфейса
DEFAULT_INTERFACE=$(ip route show default | awk '/default/ {print $5}' | head -n1)
if [ -z "$DEFAULT_INTERFACE" ]; then
  DEFAULT_INTERFACE="eth0"
  echo "Предупреждение: Не удалось автоматически определить сетевой интерфейс. Используется по умолчанию: eth0"
else
  echo "Определен основной сетевой интерфейс: $DEFAULT_INTERFACE"
fi

# Создание директории для конфигураций AmneziaWG
mkdir -p /etc/amnezia/amneziawg

# Генерация ключей сервера, если они еще не существуют
PRIVATE_KEY_FILE="/etc/amnezia/amneziawg/server.key"
PUBLIC_KEY_FILE="/etc/amnezia/amneziawg/server.pub"

if [ ! -f "$PRIVATE_KEY_FILE" ]; then
  echo "Генерация ключей сервера AmneziaWG..."
  awg genkey | tee "$PRIVATE_KEY_FILE" | awg pubkey > "$PUBLIC_KEY_FILE"
  chmod 600 "$PRIVATE_KEY_FILE"
  echo "Ключи успешно сгенерированы."
else
  echo "Ключи сервера уже существуют. Используем существующие."
fi

SERVER_PRIVATE_KEY=$(cat "$PRIVATE_KEY_FILE")
SERVER_PUBLIC_KEY=$(cat "$PUBLIC_KEY_FILE")

# Создание базового конфига awg0.conf
CONFIG_FILE="/etc/amnezia/amneziawg/awg0.conf"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "Создание конфигурационного файла $CONFIG_FILE..."
  cat > "$CONFIG_FILE" <<EOF
[Interface]
Address = 10.66.66.1/24
ListenPort = 51820
PrivateKey = $SERVER_PRIVATE_KEY

# Параметры маскировки AmneziaWG (Junk Packet Parameters)
Jc = 4
Jmin = 40
Jmax = 70
S1 = 15
S2 = 97
H1 = 394850
H2 = 983475
H3 = 129485
H4 = 847592

PostUp = iptables -t nat -A POSTROUTING -o $DEFAULT_INTERFACE -j MASQUERADE; ip6tables -t nat -A POSTROUTING -o $DEFAULT_INTERFACE -j MASQUERADE; iptables -A FORWARD -i awg0 -j ACCEPT; ip6tables -A FORWARD -i awg0 -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o $DEFAULT_INTERFACE -j MASQUERADE; ip6tables -t nat -D POSTROUTING -o $DEFAULT_INTERFACE -j MASQUERADE; iptables -D FORWARD -i awg0 -j ACCEPT; ip6tables -D FORWARD -i awg0 -j ACCEPT
EOF
  chmod 600 "$CONFIG_FILE"
  echo "Базовый конфиг awg0.conf успешно создан."
else
  echo "Конфигурационный файл $CONFIG_FILE уже существует. Пропускаем создание."
fi

# Настройка правил брандмауэра UFW, если он включен
if ufw status | grep -q "Status: active"; then
  echo "UFW активен. Добавляем разрешение для UDP порта 51820..."
  ufw allow 51820/udp
  ufw route allow in on awg0
  ufw reload
fi

# Включение и запуск сервиса AmneziaWG
echo "Включение и запуск сервиса awg-quick@awg0..."
systemctl enable awg-quick@awg0 || echo "Предупреждение: Не удалось включить автозапуск awg-quick@awg0."
systemctl restart awg-quick@awg0 || echo "Предупреждение: Не удалось перезапустить awg-quick@awg0 (может потребоваться перезагрузка)."

echo "=== УСТАНОВКА И НАСТРОЙКА AMNEZIAWG ЗАВЕРШЕНА ==="
echo "Публичный ключ сервера: $SERVER_PUBLIC_KEY"
