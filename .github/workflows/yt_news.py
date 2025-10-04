# yt_news.py
# 目的：
# - 直近24時間の新着動画を YouTube Data API で取得
# - タイトル/説明/公開時刻を整形して Slack に投稿
# 必要な環境変数（GitHub Secrets で設定）：
# - YT_API_KEY
# - SLACK_WEBHOOK_URL
# - CHANNEL_IDS（カンマ区切りの UC... を並べる）
import os
import requests
from datetime import datetime, timedelta, timezone
from itertools import islice

API_KEY = os.environ["YT_API_KEY"]
SLACK_WEBHOOK = os.environ["SLACK_WEBHOOK_URL"]
CHANNEL_IDS = [c.strip() for c in os.environ["CHANNEL_IDS"].split(",") if c.strip()]
YAPI = "https://www.googleapis.com/youtube/v3"

# 設定（必要なら Secrets ではなくここを書き換え）
HOURS_WINDOW = int(os.environ.get("HOURS_WINDOW", "24"))   # 直近何時間を見るか
MAX_PER_CHANNEL = int(os.environ.get("MAX_PER_CHANNEL", "10"))  # 各chで見る最大件数（軽量運用）

JST = timezone(timedelta(hours=9))

def now_utc():
    return datetime.now(timezone.utc)

def to_iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

def to_jst_str(iso_z: str) -> str:
    dt = datetime.fromisoformat(iso_z.replace("Z", "+00:00")).astimezone(JST)
    return dt.strftime("%Y-%m-%d %H:%M")

def http_get(url: str, **params):
    params["key"] = API_KEY
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def get_uploads_pid(channel_id: str) -> str | None:
    # channels.list: contentDetails.relatedPlaylists.uploads を取得
    r = http_get(f"{YAPI}/channels", part="contentDetails", id=channel_id)
    items = r.get("items", [])
    if not items:
        return None
    return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

def list_recent_video_ids(uploads_pid: str, published_after_z: str) -> list[str]:
    # playlistItems.list で直近 MAX_PER_CHANNEL 件から、publishedAfter 以降の videoId を抽出
    r = http_get(
        f"{YAPI}/playlistItems",
        part="contentDetails",
        playlistId=uploads_pid,
        maxResults=min(MAX_PER_CHANNEL, 50)
    )
    ids = []
    for it in r.get("items", []):
        vid = it["contentDetails"]["videoId"]
        pub = it["contentDetails"].get("videoPublishedAt")  # ISO Z
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
    # videos.list は 1回で最大50件
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
    "multimodal": "マルチモーダル",
    "vision": "画像/動画",
    "video": "画像/動画",
    "audio": "音声",
    "agent": "エージェント",
    "tool": "ツール実行",
    "safety": "安全性",
    "copyright": "著作権",
    "regulation": "規制",
    "construction": "建設/施工",
    "marketing": "営業/マーケ",
    "open-source": "OSS",
    "finance": "金融",
}

def make_tags(title: str, desc: str) -> list[str]:
    text = f"{title}\n{desc}".lower()
    tags = [v for k, v in KEYWORDS.items() if k in text]
    return tags[:5] or ["一般トピック"]

def trim(s: str, n: int = 180) -> str:
    s = " ".join(s.split())
    return s[:n] + "…" if len(s) > n else s

def post_to_slack(blocks: list[dict]):
    payload = {"text": "AIトレンド新着", "blocks": blocks}
    r = requests.post(SLACK_WEBHOOK, json=payload, timeout=30)
    r.raise_for_status()

def main():
    published_after_z = to_iso_z(now_utc() - timedelta(hours=HOURS_WINDOW))

    all_ids = set()
    for ch in CHANNEL_IDS:
        upid = get_uploads_pid(ch)
        if not upid:
            continue
        ids = list_recent_video_ids(upid, published_after_z)
        all_ids.update(ids)

    if not all_ids:
        # 新着なしの場合は静かに終了（必要なら Slack に「新着なし」を投稿しても良い）
        return

    metas = fetch_videos_meta(list(all_ids))
    metas.sort(key=lambda x: x["snippet"]["publishedAt"], reverse=True)

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": f"AIトレンド新着（過去{HOURS_WINDOW}h）"}}
    ]

    for it in metas:
        sn = it["snippet"]
        title = sn.get("title", "(no title)")
        url = f"https://www.youtube.com/watch?v={it['id']}"
        chname = sn.get("channelTitle", "(unknown)")
        pub_jst = to_jst_str(sn["publishedAt"])
        desc = trim(sn.get("description", "（説明なし）"))
        tags = ", ".join(make_tags(title, desc))

        blocks += [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*<{url}|{title}>*\n{desc}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f":bust_in_silhouette: {chname}"},
                {"type": "mrkdwn", "text": f":calendar: {pub_jst} JST"},
                {"type": "mrkdwn", "text": f":label: {tags}"}
            ]},
            {"type": "divider"}
        ]

    post_to_slack(blocks)

if __name__ == "__main__":
    main()
