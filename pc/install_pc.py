"""Установка исполнителя команд Ската на этот ПК (Windows, без прав администратора): папка %LOCALAPPDATA%\\SkatPC,
конфиг с белым списком, автозапуск при входе (скрытый запуск через .vbs в папке «Автозагрузка») и запуск прямо сейчас."""
import json, os, secrets, shutil, subprocess, sys

SPEAKER_IP = sys.argv[1] if len(sys.argv) > 1 else input("IP колонки в домашней сети (например 192.168.1.50): ").strip()
home = os.path.join(os.environ["LOCALAPPDATA"], "SkatPC"); os.makedirs(home, exist_ok=True)
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "pcagent.py"), os.path.join(home, "pcagent.py"))
LA, AD = os.environ["LOCALAPPDATA"], os.environ["APPDATA"]


def first(*paths):
    return next((p for p in paths if os.path.exists(p)), None)


def exe(path, *args, proc=None):
    return {"launch": [path, *args], "exe": proc or os.path.basename(path)}


apps = {}
chrome = first(r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
if chrome: apps["chrome"] = exe(chrome)
code = first(os.path.join(LA, r"Programs\Microsoft VS Code\Code.exe"), r"C:\Program Files\Microsoft VS Code\Code.exe")
if code: apps["vscode"] = exe(code)
discord = os.path.join(LA, r"Discord\Update.exe")
if os.path.exists(discord): apps["discord"] = {"launch": [discord, "--processStart", "Discord.exe"], "exe": "Discord.exe"}
steam = first(r"C:\Program Files (x86)\Steam\steam.exe", r"C:\Program Files\Steam\steam.exe")
if steam: apps["steam"] = exe(steam)
apps["notepad"] = {"launch": ["notepad.exe"], "exe": "notepad.exe"}
apps["calc"] = {"launch": ["calc.exe"], "exe": "CalculatorApp.exe"}
apps["explorer"] = {"launch": ["explorer.exe"]}
# приложения из меню «Пуск» запускаются по AppID
apps["telegram"] = {"launch": ["explorer.exe", r"shell:AppsFolder\Telegram.TelegramDesktop.d274200159983d7326f00aca660c30c7"], "exe": "Telegram.exe"}
apps["cursor"] = {"launch": ["explorer.exe", r"shell:AppsFolder\Anysphere.Cursor"], "exe": "Cursor.exe"}
apps["yamusic"] = {"launch": ["explorer.exe", r"shell:AppsFolder\ru.yandex.desktop.music"]}

cfg_path = os.path.join(home, "config.json")
token = json.load(open(cfg_path))["token"] if os.path.exists(cfg_path) else secrets.token_hex(16)
cfg = {"token": token, "port": 8790, "allow_ip": ["127.0.0.1", SPEAKER_IP], "apps": apps,
       "sites": ["youtube.com", "google.com", "yandex.ru", "ya.ru", "github.com", "wikipedia.org", "vk.com", "kinopoisk.ru",
                 "twitch.tv", "music.yandex.ru", "web.telegram.org", "habr.com"]}
json.dump(cfg, open(cfg_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("программы в белом списке:", ", ".join(apps))

if "--autostart" in sys.argv:  # автозагрузка только по явному флагу (запускает сам пользователь)
    startup = os.path.join(AD, r"Microsoft\Windows\Start Menu\Programs\Startup", "SkatPC.vbs")
    open(startup, "w", encoding="utf-8").write(
        'CreateObject("WScript.Shell").Run """%s"" ""%s""", 0, False\n' % (sys.executable, os.path.join(home, "pcagent.py")))
    print("автозапуск:", startup)
else:
    print("автозапуск НЕ включён (запуск с флагом --autostart включает его)")
# перезапуск агента, если уже работал
subprocess.run(["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*SkatPC*pcagent.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
               capture_output=True)
subprocess.Popen([sys.executable, os.path.join(home, "pcagent.py")], creationflags=0x08000000, close_fds=True)
open(os.path.join(home, "token.txt"), "w").write(token)  # для переноса на колонку (файл только для текущего пользователя Windows)
print("агент запущен, порт 8790")
