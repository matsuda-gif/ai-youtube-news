# yt_minutes.py
# 目的：
#  - 直近HOURS_WINDOW時間の新着動画をYouTube Data APIで取得
#  - 「議事録スタイル」に整形してSlackに投稿
#
# 前提（GitHub Secrets）:
#  - YT_API_KEY, SLACK_WEBHOOK_URL, CHANNEL_IDS (UC...をカンマ区切り)
# 任意（Secrets or env）:
#  - HOURS_WINDOW (デフォルト24), MAX_PER_CHANNEL (デフォルト10)

import os, re, requests
from datetime import datetime, timedelta, timezone
from itertools import islice

API_KEY = os.environ["YT_API_KEY"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
CHANNEL_IDS = [c.strip() for c in os.environ["CHANNEL_IDS"].split(",") if c.strip()]
YAPI = "https://www.googleapis.com/youtube/v3"

HOURS_WINDOW = int(os.environ.get("HOURS_WINDOW", "24"))
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "10"))
JST = timezone(timedelta(hours=9))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def to_jst_str(iso_z: str) -> str:
    dt = datetime.fromisoformat(iso_z.replace("Z","+00:00")).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M")

def http_get(url: str, **params):
    params["key"] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_uploads_pid(channel_id: str) -> str | None:
    r = http_get(f"{YAPI}/channels", part="contentDetails", id=channel_id)
    items = r.get("items", [])
    if not items: return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def list_recent_video_ids(uploads_pid: str, published_after_z: str) -> list[str]:
    r = http_get(
        f"{YAPI}/playlistItems",
        part="contentDetails",
        playlistId=uploads_pid,
        maxResults=min(MAX_PER_CHANNEL, 50)
    )
    ids = []
    for it in r.get("items", []):
        vid = it["contentDetails"]["videoId"]
        pub = it["contentDetails"].get("videoPublishedAt")
        if pub and pub >= published_after_z:
            ids.append(vid)
    return ids

def chunked(iterable, size):
    it = iter(iterable)
    while True:
        chunk = list(islice(it, size))
        if not chunk:
            return
        yield chunk

def fetch_videos_meta(video_ids: list[str]) -> list[dict]:
    items = []
    for chunk in chunked(video_ids, 50):
        r = http_get(
            f"{YAPI}/videos",
            part="snippet,statistics",
            id=",".join(chunk)
        )
        items.extend(r.get("items", []))
    return items

KEYWORDS = {
    "multimodal":"マルチモーダル","vision":"画像/動画","video":"画像/動画","audio":"音声",
    "agent":"エージェント","tool":"ツール実行","safety":"安全性",
    "copyright":"著作権","regulation":"規制","construction":"建設/施工",
    "marketing":"営業/マーケ","open-source":"OSS","finance":"金融"
}

def make_tags(title: str, desc: str) -> list[str]:
    text = f"{title}\n{desc}".lower()
    tags = [v for k,v in KEYWORDS.items() if k in text]
    return tags[:5] or ["一般トピック"]

def normalize_text(s: str) -> str:
    s = s.strip()
    s = re.sub(r"\s+", " ", s)
    return s

def bullets_from_description(desc: str, max_items: int = 3) -> list[str]:
    """
    超簡易：説明欄から箇条書き候補を抽出。
    - 改行/・/.-で分割し、短い文を削除して上位3件を返す
    - LLM未使用（0円運用・規約順守）
    """
    if not desc:
        return []
    raw = desc.replace("\r","").split("\n")
    parts = []
    for line in raw:
        line = line.strip(" ・-•\t")
        if not line: continue
        # 句点で増やす（英語にも対応軽め）
        fragments = re.split(r"[。.!?・•\-]+", line)
        for f in fragments:
            f = normalize_text(f)
            if len(f) >= 8:  # あまり短いのは除外
                parts.append(f)
    # 先頭から3件
    return parts[:max_items]

def build_minutes_text(metas: list[dict]) -> str:
    date_str = datetime.now(JST).strftime("%Y-%m-%d")
    lines = [f"📄 AIトレンド社内報（{date_str}）", ""]
    for idx, it in enumerate(metas, 1):
        sn = it["snippet"]
        title = sn.get("title", "(no title)")
        url = f"https://www.youtube.com/watch?v={it['id']}"
        chname = sn.get("channelTitle", "(unknown)")
        pub_jst = to_jst_str(sn["publishedAt"])
        desc = sn.get("description", "")
        tags = " ".join([f"#{t}" for t in make_tags(title, desc)])
        bullets = bullets_from_description(desc, 3) or [normalize_text(desc)[:100] + "…"] if desc else ["（説明なし）"]

        lines.append(f"【議題{idx}】{title}")
        lines.append(f"- 日時：{pub_jst} JST")
        lines.append(f"- 発表者：{chname}")
        lines.append(f"- 概要：")
        for b in bullets:
            lines.append(f"   ・{b}")
        lines.append(f"- URL：{url}")
        lines.append(f"- タグ：{tags if tags else '（なし）'}")
        lines.append("────────────────────────")
        lines.append("")
    return "\n".join(lines)

def post_to_slack_text(text: str):
    payload = {"text": text}
    r = requests.post(SLACK_WEBHOOK, json=payload, timeout=30)
    r.raise_for_status()

def main():
    published_after_z = to_iso_z(now_utc() - timedelta(hours=HOURS_WINDOW))
    all_ids = set()
    for ch in CHANNEL_IDS:
        upid = get_uploads_pid(ch)
        if not upid: continue
        all_ids.update(list_recent_video_ids(upid, published_after_z))

    if not all_ids:
        # 新着なし通知（必要なければreturn）
        date_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
        post_to_slack_text(f"📄 AIトレンド社内報：新着なし（{date_str} JST, 過去{HOURS_WINDOW}h）")
        return

    metas = fetch_videos_meta(list(all_ids))
    metas.sort(key=lambda x: x["snippet"]["publishedAt"], reverse=True)
    text = build_minutes_text(metas)
    post_to_slack_text(text)

if __name__ == "__main__":
    main()
