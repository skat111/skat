"""Командные слова «дальше», «назад», «пауза» (и синонимы) разными голосами Yandex TTS -> cmd_tts.npz.
Запуск: set -a; . /etc/skat.env; set +a; python gen_cmd.py"""
import numpy as np
from gen_tts import tts, trim, VOICES, EMO

CLASSES = {
    "dalshe": ["Дальше", "Дальше!", "Дальше.", "Следующий", "Следующий!", "Следующая"],
    "nazad": ["Назад", "Назад!", "Назад.", "Предыдущий", "Предыдущий!", "Предыдущая"],
    "pauza": ["Пауза", "Пауза!", "Пауза.", "Паузу", "Пауза, пожалуйста"],
}
out = {}
for k, texts in CLASSES.items():
    clips = []
    for v in VOICES:
        for sp in (0.8, 0.95, 1.1, 1.3):
            for emo in EMO.get(v, [None]):
                for t in texts[(len(clips) % 3)::3]:
                    x = trim(tts(t, v, sp, emo))
                    if x is not None: clips.append(x)
    out[k] = np.array(clips, dtype=object); print(k, len(clips), flush=True)
np.savez("cmd_tts.npz", **out)
print("saved")
