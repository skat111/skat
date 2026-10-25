"""Много разнообразных русских фраз (DeepSeek) -> Yandex TTS (3 голоса на фразу) -> neg2.npz. Негативы для детектора."""
import json, os, random, sys, urllib.request
import numpy as np
sys.path.insert(0, ".")
from gen_tts import tts, trim, VOICES, EMO

random.seed(1)
PROMPTS = [
    "Сгенерируй 200 разных коротких фраз на русском (3-12 слов), как говорят дома люди: вопросы, команды умной колонке, бытовая болтовня, числа, имена, города. Каждая фраза в отдельной строке, без нумерации и кавычек.",
    "Сгенерируй 150 коротких русских фраз и слов (1-6 слов), где есть слова, звучащие похоже на «скат»: скот, скатерть, каток, сказка, скачать, скажи, кот, катер, скотч, ската, скала, скандал, сказал, скаут, скидка, скейт, скат (рыба в смысле животного НЕ используй), скакать, скатился, класс, квас, как, кто, ска, рэп. Фразы разной длины, слова-двойники и в конце фразы тоже. Каждая в отдельной строке, без нумерации и кавычек.",
    "Сгенерируй 150 русских реплик, которые люди говорят друг другу и по телефону: приветствия, просьбы, удивление, ругань без мата, согласие и отказ, междометия, обращения по имени (Маша, Антон, Дима, Алиса, Маруся, Сири). Каждая в отдельной строке, без нумерации и кавычек.",
]
KEY = os.environ["DEEPSEEK_API_KEY"]
phrases = []
for p in PROMPTS:
    r = urllib.request.Request("https://api.deepseek.com/chat/completions", json.dumps({
        "model": "deepseek-flash", "messages": [{"role": "user", "content": p}], "max_tokens": 4000,
        "thinking": {"type": "disabled"}}).encode(), {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
    txt = json.load(urllib.request.urlopen(r, timeout=120))["choices"][0]["message"]["content"]
    phrases += [l.strip(" -•\t\"«»") for l in txt.split("\n") if 1 < len(l.strip()) < 90]
phrases = [p for p in dict.fromkeys(phrases) if "скат" not in p.lower().replace("скатерть", "").replace("скатил", "")]
print("phrases", len(phrases), flush=True)
out = []
for i, t in enumerate(phrases):
    for v in random.sample(VOICES, 3):
        x = trim(tts(t, v, round(random.uniform(0.85, 1.25), 2), random.choice(EMO.get(v, [None]))))
        if x is not None: out.append(x)
    if i % 50 == 0: print(i, len(out), flush=True)
np.savez("neg2.npz", neg=np.array(out, dtype=object))
print("saved", len(out))
