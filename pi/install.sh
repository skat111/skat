#!/bin/sh
# Установка музыки и лент на Raspberry Pi OS / Debian (root). Использование: sudo ./install.sh
set -e
[ "$(id -u)" = 0 ] || { echo "Запустите от root: sudo ./install.sh"; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
apt-get update -qq && apt-get install -y -qq python3-venv ffmpeg bluez >/dev/null
mkdir -p /opt/skat-music && python3 -m venv /opt/skat-music/venv
/opt/skat-music/venv/bin/pip install -q yandex-music bleak pyserial requests
cp "$HERE/musicd.py" "$HERE/ledd.py" /opt/skat-music/
cp "$HERE/skat-music.service" "$HERE/skat-led.service" /etc/systemd/system/
if [ ! -f /etc/skat-music.env ]; then
  umask 077; TOKEN=$(openssl rand -hex 24)
  printf "Токен Яндекс Музыки (можно пусто, ввод скрыт): "; stty -echo; read -r YM; stty echo; echo
  { echo "MUSICD_TOKEN=$TOKEN"; echo "YANDEX_MUSIC_TOKEN=$YM"; } > /etc/skat-music.env; chmod 600 /etc/skat-music.env
  echo "MUSIC_TOKEN для колонки: $TOKEN"
fi
systemctl daemon-reload && systemctl enable --now skat-music
echo "Музыка запущена. IllumiHome DMRRBA-007 включайте после того, как найдёте её: добавьте LED_ADDR в /etc/skat-music.env и: systemctl enable --now skat-led"
echo "Поиск ленты по Bluetooth: /opt/skat-music/venv/bin/python -c \"import asyncio;from bleak import BleakScanner;[print(d.address,d.name) for d in asyncio.run(BleakScanner.discover(10))]\""
