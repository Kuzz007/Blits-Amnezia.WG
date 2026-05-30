#!/bin/bash
# -------------------------------------------------------------------------
# 🛡️ Blitz Panel (AmneziaWG Web Panel) - Интерактивный установщик в 1 клик
# -------------------------------------------------------------------------
set -e

# Цвета для вывода в терминал
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

clear 2>/dev/null || true
echo -e "${PURPLE}"
echo "========================================================================="
echo "   🛡️   ДОБРО ПОЖАЛОВАТЬ В УСТАНОВЩИК BLITZ PANEL & AMNEZIAWG   🛡️"
echo "========================================================================="
echo -e "${NC}"
echo -e "Этот скрипт полностью настроит AmneziaWG на вашем сервере, докеризует"
echo -e "веб-панель администратора и настроит безопасный веб-доступ (IP или SSL)."
echo ""

# Шаг 1. Проверка прав root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Ошибка: Этот скрипт должен быть запущен от имени root (через sudo).${NC}" >&2
    exit 1
fi

# Шаг 2. Проверка операционной системы
if [ -f /etc/os-release ]; then
    . /etc/os-release
    if [[ "$ID" != "ubuntu" && "$ID" != "debian" ]]; then
        echo -e "${YELLOW}Предупреждение: Установка рекомендуется на Ubuntu 22.04 / 24.04 LTS.${NC}"
        echo -e "Ваша система: $NAME ($VERSION)"
        read -p "Продолжить установку на свой страх и риск? (y/n): " confirm_os
        if [[ "$confirm_os" != "y" && "$confirm_os" != "Y" ]]; then
            exit 1
        fi
    fi
else
    echo -e "${YELLOW}Предупреждение: Не удалось определить ОС.${NC}"
    read -p "Продолжить установку? (y/n): " confirm_os
    if [[ "$confirm_os" != "y" && "$confirm_os" != "Y" ]]; then
        exit 1
    fi
fi

# Шаг 3. Установка системных зависимостей хоста (Docker и AmneziaWG)
echo -e "\n${BLUE}[1/5] Проверка и установка Docker & Docker Compose...${NC}"
if ! command -v docker &> /dev/null; then
    echo "Установка Docker в систему..."
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    
    # Репозиторий Docker
    if [[ "$ID" == "ubuntu" ]]; then
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    else
        curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $VERSION_CODENAME stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null
    fi
    
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    echo -e "${GREEN}Docker успешно установлен!${NC}"
else
    echo -e "${GREEN}Docker уже установлен в системе.${NC}"
fi

echo -e "\n${BLUE}[2/5] Сборка и настройка AmneziaWG в ядре сервера...${NC}"
if [ -f "./install_amneziawg.sh" ]; then
    chmod +x install_amneziawg.sh
    # Запускаем официальную установку AmneziaWG с автоподтверждением
    bash install_amneziawg.sh --confirm
else
    echo -e "${RED}Ошибка: Не найден скрипт install_amneziawg.sh в корневой папке!${NC}"
    exit 1
fi

# Подготовка директории для БД и данных
mkdir -p data/clients data/qr

# Шаг 4. Интерактивный опрос: Домен или IP
echo -e "\n${BLUE}[3/5] Настройка режима работы веб-панели:${NC}"
echo -e "  1) Доступ по ${CYAN}IP-адресу${NC} сервера (без шифрования SSL, порт 80)"
echo -e "  2) Доступ по ${CYAN}доменному имени${NC} (с автоматическим выпуском SSL-сертификата Let's Encrypt)"
echo ""
read -p "Выберите вариант (1 или 2, по умолчанию 1): " net_choice
net_choice=${net_choice:-1}

DOMAIN_MODE=false
PANEL_PORT=8080

if [[ "$net_choice" == "2" ]]; then
    DOMAIN_MODE=true
    echo -e "\n--- Настройка домена и SSL ---"
    read -p "Введите ваш домен (например, vpn.mysite.com): " domain_name
    read -p "Введите ваш Email (для Let's Encrypt оповещений): " email_address
    
    if [[ -z "$domain_name" || -z "$email_address" ]]; then
        echo -e "${RED}Ошибка: Имя домена и email не могут быть пустыми! Возврат к режиму IP.${NC}"
        DOMAIN_MODE=false
    fi
fi

if [ "$DOMAIN_MODE" = true ]; then
    PANEL_PORT=1010 # В режиме домена запускаем панель на внутреннем порту 1010
    
    echo -e "\nУстановка Certbot для выпуска SSL-сертификата..."
    apt-get install -y certbot
    
    # Временно освобождаем 80 порт
    systemctl stop nginx 2>/dev/null || true
    docker stop nginx-proxy 2>/dev/null || true
    docker stop amnezia-panel 2>/dev/null || true
    
    echo "Запрос бесплатного SSL-сертификата Let's Encrypt..."
    if certbot certonly --standalone -d "$domain_name" --non-interactive --agree-tos --email "$email_address"; then
        echo -e "${GREEN}SSL-сертификат успешно получен!${NC}"
        
        # Заполняем шаблон Nginx
        if [ -f "nginx.conf.template" ]; then
            sed -e "s/__DOMAIN__/$domain_name/g" -e "s/__PORT__/$PANEL_PORT/g" nginx.conf.template > nginx.conf
            echo -e "${GREEN}Файл конфигурации nginx.conf успешно создан.${NC}"
        else
            echo -e "${RED}Ошибка: Не найден шаблон nginx.conf.template!${NC}"
            exit 1
        fi
    else
        echo -e "${RED}Ошибка выпуска SSL-сертификата! Проверьте, что домен привязан к IP этого сервера.${NC}"
        echo -e "${YELLOW}Откат на установку по IP-адресу.${NC}"
        DOMAIN_MODE=false
    fi
fi

if [ "$DOMAIN_MODE" = false ]; then
    PANEL_PORT=80 # В режиме IP вешаем панель напрямую на 80 веб-порт
fi

# Записываем порт в файл окружения
echo "PANEL_PORT=$PANEL_PORT" > data/panel.env

# Шаг 5. Интерактивный опрос: Настройка пароля администратора
echo -e "\n${BLUE}[4/5] Настройка пароля администратора:${NC}"
echo -e "  1) Оставить пароль по умолчанию (${CYAN}admin / admin${NC})"
echo -e "     (При первом входе в панель система принудительно попросит сменить пароль)"
echo -e "  2) Задать надежный пароль прямо сейчас"
echo ""
read -p "Выберите вариант (1 или 2, по умолчанию 1): " pass_choice
pass_choice=${pass_choice:-1}

CUSTOM_PASS=""
if [[ "$pass_choice" == "2" ]]; then
    echo -e "\n--- Настройка пароля ---"
    while true; do
        read -s -p "Введите новый пароль администратора: " custom_pass
        echo ""
        read -s -p "Повторите новый пароль: " custom_pass_confirm
        echo ""
        
        if [ "$custom_pass" = "$custom_pass_confirm" ]; then
            if [ ${#custom_pass} -lt 5 ]; then
                echo -e "${RED}Ошибка: Пароль должен содержать минимум 5 символов! Попробуйте снова.${NC}"
            else
                CUSTOM_PASS="$custom_pass"
                break
            fi
        else
            echo -e "${RED}Ошибка: Пароли не совпадают! Попробуйте снова.${NC}"
        fi
    done
fi

# Шаг 6. Сборка и запуск Docker контейнеров
echo -e "\n${BLUE}[5/5] Запуск контейнеров в Docker...${NC}"
# Остановим старые контейнеры, если были
docker compose down || true

if [ "$DOMAIN_MODE" = true ]; then
    echo "Запуск панели с SSL-проксированием Nginx..."
    docker compose --profile ssl up -d --build
else
    echo "Запуск панели в режиме прямого IP-доступа..."
    docker compose up -d --build
fi

# Шаг 7. Применение кастомного пароля в БД (если был выбран)
if [[ -n "$CUSTOM_PASS" ]]; then
    echo "Применение настроенного вами пароля администратора..."
    # Даем базе данных и контейнеру 3 секунды на инициализацию
    sleep 3
    
    docker exec amnezia-panel python3 -c "
import sqlite3, bcrypt
conn = sqlite3.connect('/app/data/panel.db')
h = bcrypt.hashpw(b'$custom_pass', bcrypt.gensalt()).decode('utf-8')
conn.execute('UPDATE users SET password_hash = ?, must_change_password = 0 WHERE username = \"admin\"', (h,))
conn.commit()
conn.close()
"
    echo -e "${GREEN}Новый пароль успешно применен в базу данных!${NC}"
fi

# Вывод красивого финального баннера
PUBLIC_IP=$(curl -s https://ifconfig.me || curl -s https://api.ipify.org)

echo -e "\n${GREEN}========================================================================="
echo "   🎉   УСТАНОВКА И НАСТРОЙКА BLITZ PANEL УСПЕШНО ЗАВЕРШЕНА!   🎉"
echo "=========================================================================${NC}"
echo ""
echo -e "Адрес панели в вашем браузере:"
if [ "$DOMAIN_MODE" = true ]; then
    echo -e "  🔗  ${CYAN}https://$domain_name${NC}"
else
    echo -e "  🔗  ${CYAN}http://$PUBLIC_IP${NC}"
fi
echo ""
echo -e "Данные для входа в панель:"
echo -e "  👤  Имя пользователя: ${CYAN}admin${NC}"
if [[ -n "$CUSTOM_PASS" ]]; then
    echo -e "  🔑  Пароль:           ${CYAN}[Установлен ваш личный надежный пароль]${NC}"
else
    echo -e "  🔑  Пароль:           ${CYAN}admin${NC} (система сразу потребует его смену)"
fi
echo ""
echo -e "Для API-интеграции с Telegram-ботом используйте Bearer Token:"
# Сгенерируем случайный токен для бота и выведем его пользователю
BOT_TOKEN="awg_bot_api_token_$(openssl rand -hex 16)"
# Сохраняем токен в файле настроек .env
echo "API_TOKEN=$BOT_TOKEN" >> data/panel.env
echo -e "  🤖  ${YELLOW}$BOT_TOKEN${NC}"
echo ""
echo -e "Служба панели работает стабильно в Docker-контейнере."
echo -e "Увидеть логи панели можно командой: ${PURPLE}docker logs -f amnezia-panel${NC}"
echo -e "${GREEN}=========================================================================${NC}\n"
