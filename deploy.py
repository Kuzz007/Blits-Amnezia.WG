import os
import sys
import subprocess
import time
from pathlib import Path

# Шаг 1. Автоматическое развертывание paramiko для скрипта деплоя
try:
    import paramiko
    from paramiko import SFTPClient
except ImportError:
    print("Библиотека paramiko не найдена. Устанавливаем её в окружение...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "paramiko"])
    import paramiko
    from paramiko import SFTPClient

# Параметры подключения к VPS
VPS_IP = "2.26.0.249"
VPS_PORT = 22
VPS_USER = "root"
VPS_PASS = "pashatop15Q"

REMOTE_DIR = "/root/amnezia-panel-mvp"

def upload_directory(sftp, local_dir, remote_dir):
    """ Рекурсивно загружает директорию на сервер """
    local_path = Path(local_dir)
    sftp.mkdir(remote_dir)
    
    for item in local_path.iterdir():
        if item.name in [".venv", "data", "deploy.py", "__pycache__", ".git", "panel.db"]:
            continue
        
        remote_item_path = f"{remote_dir}/{item.name}"
        if item.is_dir():
            upload_directory(sftp, item, remote_item_path)
        else:
            print(f"Загрузка файла: {item.name} -> {remote_item_path}")
            sftp.put(str(item), remote_item_path)

def main():
    print("=== НАЧАЛО ДЕПЛОЯ НА VPS ===")
    
    # 1. Подключение по SSH
    print(f"Подключение к {VPS_IP}:{VPS_PORT} под пользователем {VPS_USER}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(VPS_IP, port=VPS_PORT, username=VPS_USER, password=VPS_PASS, timeout=15)
        print("Успешно подключено к серверу по SSH!")
    except Exception as e:
        print(f"Ошибка подключения по SSH: {e}")
        sys.exit(1)
        
    # 2. Подготовка директорий на сервере
    print(f"Очистка и подготовка удаленной директории {REMOTE_DIR} (сохраняя базу данных и окружение)...")
    # Создаем директорию, если ее нет
    ssh.exec_command(f"mkdir -p {REMOTE_DIR}")
    # Удаляем только старый код, сохраняя data и .venv во избежание потери данных клиентов
    ssh.exec_command(f"find {REMOTE_DIR} -mindepth 1 -maxdepth 1 ! -name 'data' ! -name '.venv' -exec rm -rf {{}} +")
    time.sleep(1)
    
    # 3. Загрузка файлов по SFTP
    print("Запуск SFTP сессии для копирования файлов...")
    sftp = ssh.open_sftp()
    
    local_base = Path(__file__).resolve().parent
    
    # Загружаем отдельные файлы корня
    root_files = ["check_server.sh", "install_amneziawg.sh", "requirements.txt", ".gitignore", ".env"]
    for filename in root_files:
        local_file = local_base / filename
        if local_file.exists():
            remote_path = f"{REMOTE_DIR}/{filename}"
            print(f"Загрузка файла: {filename} -> {remote_path}")
            sftp.put(str(local_file), remote_path)
            
    # Загружаем папки
    folders_to_upload = ["app", "systemd"]
    for folder in folders_to_upload:
        local_folder = local_base / folder
        if local_folder.exists() and local_folder.is_dir():
            upload_directory(sftp, local_folder, f"{REMOTE_DIR}/{folder}")
            
    sftp.close()
    print("Все файлы проекта успешно загружены!")
    
    # 4. Настройка прав запуска скриптов на VPS
    print("Настройка прав на исполнение shell-скриптов...")
    ssh.exec_command(f"chmod +x {REMOTE_DIR}/check_server.sh {REMOTE_DIR}/install_amneziawg.sh")
    
    # 5. Запуск диагностики сервера
    print("\n=== ЗАПУСК ДИАГНОСТИКИ НА VPS ===")
    stdin, stdout, stderr = ssh.exec_command(f"{REMOTE_DIR}/check_server.sh")
    print(stdout.read().decode('utf-8'))
    err = stderr.read().decode('utf-8')
    if err:
        print(f"Диагностические предупреждения:\n{err}")
        
    # 6. Настройка виртуального окружения и установка зависимостей
    print("Создание виртуального окружения python3 на VPS...")
    # Убедимся, что python3-venv установлен
    ssh.exec_command("apt-get update && apt-get install -y python3-pip python3-venv python3-full")
    
    stdin, stdout, stderr = ssh.exec_command(f"python3 -m venv {REMOTE_DIR}/.venv")
    stdout.channel.recv_exit_status() # Ожидаем завершения команды
    
    print("Установка python-зависимостей из requirements.txt на VPS...")
    stdin, stdout, stderr = ssh.exec_command(f"{REMOTE_DIR}/.venv/bin/pip install --upgrade pip && {REMOTE_DIR}/.venv/bin/pip install -r {REMOTE_DIR}/requirements.txt")
    # Выводим лог установки
    print(stdout.read().decode('utf-8'))
    
    # 7. Настройка службы Systemd для веб-панели
    print("Копирование конфигурации systemd для веб-панели...")
    ssh.exec_command(f"cp {REMOTE_DIR}/systemd/amnezia-panel.service /etc/systemd/system/amnezia-panel.service")
    
    print("Перезагрузка демонов systemd и запуск веб-панели...")
    ssh.exec_command("systemctl daemon-reload")
    ssh.exec_command("systemctl enable amnezia-panel")
    
    # Запускаем / Перезапускаем панель
    stdin, stdout, stderr = ssh.exec_command("systemctl restart amnezia-panel")
    stdout.channel.recv_exit_status()
    
    # Даем сервису пару секунд на старт и проверяем статус
    time.sleep(2)
    stdin, stdout, stderr = ssh.exec_command("systemctl is-active amnezia-panel")
    panel_status = stdout.read().decode('utf-8').strip()
    stdin, stdout, stderr = ssh.exec_command(f"sh -c 'if [ -f {REMOTE_DIR}/data/panel.env ]; then . {REMOTE_DIR}/data/panel.env; fi; echo ${{PANEL_PORT:-8080}}'")
    panel_port = stdout.read().decode('utf-8').strip() or "8080"
    
    print(f"\nСтатус службы amnezia-panel на VPS: {panel_status.upper()}")
    
    if panel_status == "active":
        print("\n" + "="*50)
        print("[OK] ДЕПЛОЙ УСПЕШНО ЗАВЕРШЕН!")
        print(f"Адрес панели в браузере: http://{VPS_IP}:{panel_port}")
        print("Стандартные учетные данные администратора:")
        print("  Имя пользователя: admin")
        print("  Пароль: admin")
        print("  (UI принудительно попросит сменить пароль при первом входе!)")
        print("\nИнтеграция с Telegram-ботом:")
        print("  Bearer Token для API хранится в файле .env на сервере.")
        print("  Сгенерированный токен по умолчанию:")
        print("  awg_bot_api_token_71fb589254d3bc7e0da1a2ef490d1bc7")
        print("="*50 + "\n")
    else:
        print("\n" + "!"*50)
        print("ВНИМАНИЕ: Служба веб-панели не смогла запуститься в фоновом режиме.")
        print("Проверьте логи службы с помощью команды на VPS:")
        print("  journalctl -u amnezia-panel -n 50 --no-pager")
        print("!"*50 + "\n")
        
    ssh.close()

if __name__ == "__main__":
    main()
