import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
SEEN_FILE = BASE_DIR / "seen_posts.json"

# RSS-Bridge 公開節點：測試過 rsshub.app（已被官方限制存取，403）、
# 以及用 Google News site: 搜尋當備援（抓到的是「提到這個帳號的新聞」，不是帳號本身的貼文，
# 內容可能完全不相關甚至不合適，所以不採用），最後決定只用 RSS-Bridge。
# 目前實測 RSS-Bridge 對 Instagram 會回傳 items: []（抓不到貼文），這裡先保留呼叫，
# 抓不到就讓該帳號在早報中顯示為空，不強行湊內容。
RSS_BRIDGE_URL = (
    "https://rss-bridge.org/bridge01/?action=display&bridge=InstagramBridge"
    "&context=Username&u={username}&format=Json"
)

EXTRA_RSS_PER_FEED = 5  # 過去 7 天內符合的文章，每個來源最多取幾篇精選
EXTRA_RSS_DAYS = 7  # 延伸文章的時間篩選範圍（天）

IG_ACCOUNTS = [
    # 商業與觀點
    "ooc.tw", "simplyugrow", "businessfocus.io", "the_insight_circle", "visionarytw",
    # 交易與投資
    "_trading.motivation_", "daytrading", "wealth", "grindandgold", "gdp.analyse", "shan__wealth",
    # 科技與 AI
    "hkepc", "evolving.ai", "b.creative.ai", "gamenews_daily",
    # 潮流與質感生活
    "overdope_com", "cool_magazine_taiwan", "ldope", "chilling_tw", "luxury_watcher",
    "douzo_labs", "tatlertaiwan",
    # 綜合媒體與觀點
    "stormmedia_tw", "zass17", "orientaldailynews",
]

# section 名稱 -> [(來源名稱, RSS 網址), ...]
EXTRA_SECTIONS = {
    "延伸科技與 AI 焦點": [
        ("INSIDE 硬塞的網路趨勢", "https://www.inside.com.tw/feed"),
        ("TechOrange 科技報橘", "https://buzzorange.com/techorange/feed/"),
        ("Google 新聞：AI 趨勢", "https://news.google.com/rss/search?q=AI%20%E8%A6%83%E5%8B%A2%20OR%20ChatGPT&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ],
    "延伸財經與加密貨幣": [
        ("鏈新聞 ABMedia", "https://www.abmedia.io/feed"),
    ],
    "延伸商業思維": [
        ("Google 新聞：商業模式", "https://news.google.com/rss/search?q=%E5%95%86%E6%A5%AD%E6%A8%A1%E5%BC%8F&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"),
    ],
}


def load_config():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat_id:
        return {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id}

    if not CONFIG_FILE.exists():
        raise SystemExit(
            "找不到環境變數 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，也找不到 "
            f"{CONFIG_FILE}。請設定環境變數（GitHub Actions 用），或複製 "
            "config.example.json 為 config.json 並填入金鑰（本機測試用）。"
        )
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def load_seen():
    if SEEN_FILE.exists():
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def strip_html(text, limit=150):
    text = re.sub(r"<[^>]+>", "", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def fetch_latest_ig_post(account_id):
    url = RSS_BRIDGE_URL.format(username=account_id)
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[IG/{account_id}] RSS-Bridge 請求失敗: {e}")
        return None

    items = data.get("items", [])
    if not items:
        return None
    return items[0]  # JSON feed 慣例：第一筆是最新的


def collect_ig_section(seen):
    lines = []
    for account_id in IG_ACCOUNTS:
        key = f"instagram:{account_id}"
        item = fetch_latest_ig_post(account_id)
        if not item:
            continue

        guid = item.get("id") or item.get("url") or item.get("link")
        if not guid or seen.get(key) == guid:
            continue  # 沒有新貼文

        title = item.get("title") or account_id
        link = item.get("url") or item.get("link") or ""
        lines.append(f"• <b>{account_id}</b>：<a href='{link}'>{strip_html(title, 60)}</a>")
        seen[key] = guid

    return lines


def fetch_rss_top(url, limit=EXTRA_RSS_PER_FEED, days=EXTRA_RSS_DAYS):
    try:
        feed = feedparser.parse(url)
    except Exception as e:
        print(f"[RSS] {url} 解析失敗: {e}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    results = []
    for entry in feed.entries:
        published = entry.get("published_parsed") or entry.get("updated_parsed")
        if published:
            entry_time = datetime(*published[:6], tzinfo=timezone.utc)
            if entry_time < cutoff:
                continue  # 超過時間範圍，跳過

        title = strip_html(getattr(entry, "title", ""), 60)
        link = getattr(entry, "link", "")
        results.append(f"• <a href='{link}'>{title}</a>")
        if len(results) >= limit:
            break
    return results


def collect_extra_sections():
    section_lines = {}
    for section_name, sources in EXTRA_SECTIONS.items():
        lines = []
        for source_name, url in sources:
            items = fetch_rss_top(url)
            if not items:
                continue
            lines.append(f"<b>{source_name}</b>")
            lines.extend(items)
        section_lines[section_name] = lines
    return section_lines


def build_digest(ig_lines, extra_sections):
    from datetime import date

    parts = [f"<b>🗞 每日社群與新知早報 {date.today().isoformat()}</b>"]

    parts.append("\n<b>【今日 IG 精選】</b>")
    parts.append("\n".join(ig_lines) if ig_lines else "（今日監控帳號沒有偵測到新貼文）")

    for section_name, lines in extra_sections.items():
        parts.append(f"\n<b>【{section_name}】</b>")
        parts.append("\n".join(lines) if lines else "（今日沒有抓到相關文章）")

    return "\n".join(parts)


def chunk_text(text, max_len=4000):
    chunks = []
    while len(text) > max_len:
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks


def send_telegram_text(config, text):
    token = config["TELEGRAM_BOT_TOKEN"]
    chat_id = config["TELEGRAM_CHAT_ID"]
    for chunk in chunk_text(text):
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"Telegram 發送失敗（{resp.status_code}）：{resp.text}")
        else:
            print("已發送一則早報訊息")


def main():
    config = load_config()
    seen = load_seen()

    print(f"開始產生每日早報，監控 {len(IG_ACCOUNTS)} 個 IG 帳號...")
    ig_lines = collect_ig_section(seen)
    extra_sections = collect_extra_sections()

    digest = build_digest(ig_lines, extra_sections)
    send_telegram_text(config, digest)

    save_seen(seen)
    print("早報產生並發送完成。")


if __name__ == "__main__":
    main()
