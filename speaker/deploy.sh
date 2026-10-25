#!/bin/sh
# Установка клиента на колонку (Alpine Linux, root по SSH). Запускать с компьютера: ./deploy.sh root@IP_колонки [путь/к/cert.pem]
set -e
H=${1:?"Использование: ./deploy.sh root@IP_колонки [cert.pem]"}; CERT=${2:-}
HERE=$(cd "$(dirname "$0")" && pwd)
ssh "$H" 'mkdir -p /opt/skat/ww /opt/skat/led /opt/skat/snd && apk add --no-cache python3 py3-numpy onnxruntime sox alsa-utils speexdsp chrony openssl >/dev/null'
scp "$HERE"/skat-client.py "$HERE"/aec.py "$HERE"/wake.py "$HERE"/ring.py "$HERE"/clock.py "$HERE"/music.py "$H":/opt/skat/
scp "$HERE"/../models/skat_model.npz "$H":/opt/skat/ww/
# звук: dmix (музыка, ответ и сигналы одновременно) и программная громкость; прежний файл сохраняем
ssh "$H" '[ -f /etc/asound.conf ] && [ ! -f /etc/asound.conf.skat-bak ] && cp /etc/asound.conf /etc/asound.conf.skat-bak || true'
scp "$HERE"/asound.conf "$H":/etc/asound.conf
scp "$HERE"/skat.initd "$H":/etc/init.d/skat && ssh "$H" 'chmod +x /etc/init.d/skat'
# признаки для детектора слова (openWakeWord, Apache-2.0): скачиваем на колонке
ssh "$H" 'cd /opt/skat/ww && for f in melspectrogram.onnx embedding_model.onnx; do [ -f $f ] || wget -q https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/$f; done; ls -la'
[ -n "$CERT" ] && scp "$CERT" "$H":/opt/skat/cert.pem
ssh "$H" '[ -f /etc/skat.conf ] || echo "ВНИМАНИЕ: создайте /etc/skat.conf по образцу speaker/skat.conf.example"'
echo "Готово. На колонке: rc-update add skat default && rc-service skat start; лог: /tmp/skat.log"
