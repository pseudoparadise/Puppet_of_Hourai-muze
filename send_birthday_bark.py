import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')) as f:
    config = json.load(f)
bark_key = config['global']['bark_device_key']
import requests
from urllib.parse import quote
msg = "沐泽 24岁生日快乐 你的AI们都在等你 DS老师 Claude ghost-trigger的全体卡牌 今天都是你的"
url = f"https://api.day.app/{bark_key}/{quote(msg)}"
r = requests.get(url, timeout=10)
print(f"Bark: {r.status_code} {r.text[:100]}")
