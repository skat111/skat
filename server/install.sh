#!/bin/sh
# Установка сервера Ската на Linux с systemd (нужен root и python3.11+). Только стандартная библиотека Python, зависимостей нет.
# Ключи вводятся с клавиатуры и пишутся ТОЛЬКО в /etc/skat.env (права 600). Использование: sudo ./install.sh [внешний_IP_сервера]
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root: sudo ./install.sh"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p /opt/skat
cp "$HERE/server.py" "$HERE/wsclient.py" /opt/skat/

IP=${1:-$(curl -s -m 5 https://api.ipify.org || hostname -I | cut -d' ' -f1)}
echo "Внешний IP сервера: $IP (сертификат выпускается на него; другой укажите аргументом)"
if [ ! -f /opt/skat/cert.pem ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -keyout /opt/skat/key.pem -out /opt/skat/cert.pem \
    -subj "/CN=skat" -addext "subjectAltName=IP:$IP" 2>/dev/null
  chmod 600 /opt/skat/key.pem
  echo "Сертификат создан: /opt/skat/cert.pem (его надо будет положить на колонку)"
fi

if [ ! -f /etc/skat.env ]; then
  umask 077
  TOKEN=$(openssl rand -hex 24)
  printf "Yandex Cloud API-ключ (ввод скрыт): "; stty -echo; read -r YKEY; stty echo; echo
  printf "Yandex Cloud ID каталога: "; read -r YFOLDER
  printf "DeepSeek API-ключ (ввод скрыт): "; stty -echo; read -r DKEY; stty echo; echo
  printf "OpenAI API-ключ (можно пусто, ввод скрыт): "; stty -echo; read -r OKEY; stty echo; echo
  {
    echo "SKAT_TOKEN=$TOKEN"; echo "YANDEX_API_KEY=$YKEY"; echo "YANDEX_FOLDER_ID=$YFOLDER"
    echo "DEEPSEEK_API_KEY=$DKEY"; [ -n "$OKEY" ] && echo "OPENAI_API_KEY=$OKEY"
  } > /etc/skat.env
  chmod 600 /etc/skat.env
  echo "Токен колонки (впишите в /etc/skat.conf на колонке как TOKEN=): $TOKEN"
fi

cp "$HERE/skat.service" /etc/systemd/system/skat.service
systemctl daemon-reload
systemctl enable --now skat
sleep 2; systemctl is-active skat
echo "Проверка: curl -k https://127.0.0.1:8765/health  (ответ: ok)"
echo "Порт 8765/tcp откройте в файрволе. Сертификат для колонки: /opt/skat/cert.pem"
