"""
크롤러 - 매 1시간마다 실행, 소스별 주기 차등 적용
  - HN + RSS: 매 실행 (1시간마다)
  - Reddit:    8시간마다 (하루 3회)
               → hot 알고리즘 decay ~12.5h, 피크 3회/일 (06-09, 12-14, 19-21 ET)
               → 8h 간격이면 모든 피크의 인기글을 놓치지 않음

Reddit 접근 우선순위:
  1. Redlib HTML 파싱 (score/댓글 수 포함, 서드파티 프론트엔드)
  2. PullPush API (비공식 아카이브)
  3. Reddit .json 직접 접근
  4. Reddit .rss 피드 (feedparser, API 키 불필요, score/댓글 없음)
"""

import os
import re
import json
import asyncio
import aiohttp
import feedparser
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup

DATA_DIR = Path(__file__).parent / "data"
POSTS_FILE = DATA_DIR / "posts.json"
STATE_FILE = DATA_DIR / "crawl_state.json"

# ─── 실행 주기 설정 ──────────────────────────────────────
# Reddit: N회마다 실행 (1시간 간격 기준, 8 = 8시간마다)
REDDIT_EVERY_N = 8

# PullPush API (Reddit 비공식 아카이브 - 인증 불필요)
PULLPUSH_BASE = "https://api.pullpush.io/reddit"
PULLPUSH_HEADERS = {"User-Agent": "DailyTrendBot/1.0"}

# Reddit 직접 접근 (fallback)
REDDIT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ─── Redlib 설정 ─────────────────────────────────────────
# 테스트 결과 확인된 인스턴스 (우선순위 순)
REDLIB_INSTANCES = [
    "https://redlib.perennialte.ch",       # ✅ 테스트 성공 (57KB, 25 posts)
    "https://redlib.privacyredirect.com",
    "https://reddit.adminforge.de",
    "https://reddit.nerdvpn.de",
    "https://redlib.thebunny.zone",
    "https://safereddit.com",
    "https://redlib.catsarch.com",
    "https://redlib.r4fo.com",
    "https://redlib.4o1x5.dev",
    "https://eu.safereddit.com",
]

REDLIB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Redlib rate limit 방어
REDLIB_DELAY_BETWEEN_SUBS = 3       # 서브레딧 간 대기 (초)
REDLIB_DELAY_BETWEEN_INSTANCES = 2  # 인스턴스 fallback 간 대기 (초)
REDLIB_TIMEOUT = 20                 # 단일 요청 타임아웃 (초)
REDLIB_MIN_HTML_SIZE = 10000        # 차단된 응답 판별 기준 (바이트)


# ─── 실행 상태 관리 ──────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return {"run_count": 0, "last_reddit": None, "last_run": None}


def save_state(state: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def should_run_reddit(state: dict) -> bool:
    return state["run_count"] % REDDIT_EVERY_N == 0


# ─── 공통 유틸 ──────────────────────────────────────────

async def fetch_json(session: aiohttp.ClientSession, url: str, headers: dict = None):
    try:
        async with session.get(url, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return await resp.json()
            else:
                print(f"  [WARN] {url} → HTTP {resp.status}")
    except Exception as e:
        print(f"  [WARN] {url} - {e}")
    return None


def _pick_reddit_image(d: dict) -> str:
    post_url = d.get("url", "")
    if any(post_url.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return post_url
    preview = d.get("preview", {})
    images = preview.get("images", [])
    if images:
        source = images[0].get("source", {})
        url = source.get("url", "")
        if url:
            return url
    thumb = d.get("thumbnail", "")
    if thumb.startswith("http") and thumb not in ("self", "default", "nsfw", "spoiler"):
        return thumb
    return ""


# ─── Reddit 방법 0: Redlib HTML 파싱 (최우선) ───────────

def _parse_redlib_score(score_div) -> int:
    """post_score div에서 정확한 숫자 추출.
    title 속성에 '17010' 같은 원본 숫자가 있음."""
    if not score_div:
        return 0
    title = score_div.get("title", "")
    if title and title.isdigit():
        return int(title)
    text = score_div.get_text(strip=True)
    m = re.match(r"([\d.]+)\s*k", text, re.IGNORECASE)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.match(r"([\d,]+)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


def _parse_redlib_comments(comment_a) -> int:
    """post_comments 링크에서 댓글 수 추출.
    title 속성에 '522 comments' 같은 텍스트가 있음."""
    if not comment_a:
        return 0
    title = comment_a.get("title", "")
    m = re.search(r"(\d[\d,]*)", title)
    if m:
        return int(m.group(1).replace(",", ""))
    text = comment_a.get_text(strip=True)
    m = re.match(r"([\d.]+)\s*k", text, re.IGNORECASE)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"(\d[\d,]*)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    return 0


def _parse_redlib_html(html: str, subreddit: str, base_url: str) -> list:
    """Redlib HTML에서 포스트 목록 파싱.

    확인된 구조:
      <div class="post" id="1qw9vkj">
        <a class="post_author" href="/u/...">u/Author</a>
        <h2 class="post_title"><a href="/r/.../comments/...">제목</a></h2>
        <div class="post_score" title="17010">17.0k<span>Upvotes</span></div>
        <a class="post_comments" title="522 comments">522 comments</a>
      </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    posts = []

    for post_div in soup.select("div.post"):
        try:
            post_id = post_div.get("id", "")
            if not post_id:
                continue

            title_el = post_div.select_one("h2.post_title a, a.post_title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")

            score_div = post_div.select_one("div.post_score, .post_score")
            score = _parse_redlib_score(score_div)

            comment_a = post_div.select_one("a.post_comments")
            comments = _parse_redlib_comments(comment_a)

            author_el = post_div.select_one("a.post_author")
            author = author_el.get_text(strip=True).replace("u/", "") if author_el else ""

            # 썸네일
            thumb_el = post_div.select_one("a.post_thumbnail")
            thumbnail = ""
            external_url = ""
            if thumb_el:
                external_url = thumb_el.get("href", "")
                img_el = thumb_el.select_one("image")
                if img_el:
                    img_href = img_el.get("href", "")
                    if img_href:
                        thumbnail = f"{base_url}{img_href}" if img_href.startswith("/") else img_href

            # 본문 미리보기
            body_el = post_div.select_one("div.post_body")
            hint = body_el.get_text(strip=True)[:300] if body_el else ""

            # permalink
            permalink = f"/r/{subreddit}/comments/{post_id}/"
            if href and "/comments/" in href:
                permalink = href

            posts.append({
                "id": f"reddit_{post_id}",
                "source": f"Reddit r/{subreddit}",
                "title": title,
                "url": external_url if external_url and external_url.startswith("http") else f"https://reddit.com{permalink}",
                "permalink": permalink,
                "score": score,
                "comments": comments,
                "hint": hint,
                "thumbnail": thumbnail,
                "top_comments": [],
            })

        except Exception as e:
            # 개별 포스트 파싱 실패는 무시하고 계속
            continue

    return posts


async def _redlib_fetch_one(
    session: aiohttp.ClientSession,
    base_url: str,
    subreddit: str,
) -> list:
    """단일 Redlib 인스턴스에서 단일 서브레딧 HTML 가져와 파싱.
    실패 시 빈 리스트 반환."""
    url = f"{base_url}/r/{subreddit}/hot"
    try:
        async with session.get(
            url,
            headers=REDLIB_HEADERS,
            timeout=aiohttp.ClientTimeout(total=REDLIB_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
            # 차단된 인스턴스는 4-6KB 빈 페이지 반환
            if len(html) < REDLIB_MIN_HTML_SIZE:
                return []
            return _parse_redlib_html(html, subreddit, base_url)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return []


async def _redlib_find_working_instance(session: aiohttp.ClientSession) -> str | None:
    """작동하는 Redlib 인스턴스를 찾아 반환.
    todayilearned 서브레딧으로 테스트."""
    for inst in REDLIB_INSTANCES:
        posts = await _redlib_fetch_one(session, inst, "todayilearned")
        if posts:
            return inst
        await asyncio.sleep(REDLIB_DELAY_BETWEEN_INSTANCES)
    return None


async def fetch_reddit_redlib(
    session: aiohttp.ClientSession,
    subreddits: list,
    limit_per_sub: int = 8,
) -> list:
    """Redlib HTML 파싱으로 여러 서브레딧의 hot 포스트 수집.

    방어적 전략:
    - 먼저 작동하는 인스턴스 1개를 찾음
    - 해당 인스턴스로 모든 서브레딧 순회
    - 중간에 실패하면 다른 인스턴스로 교체
    - 서브레딧 간 3초 대기 (rate limit 방어)
    - 연속 실패 3회 시 조기 종료
    """
    print("  [Redlib] 작동 인스턴스 탐색 중...")
    working = await _redlib_find_working_instance(session)

    if not working:
        print("  [Redlib] ❌ 작동하는 인스턴스 없음")
        return []

    print(f"  [Redlib] ✅ 사용 인스턴스: {working}")

    all_posts = []
    success = 0
    consecutive_fails = 0
    max_consecutive_fails = 3  # 연속 3회 실패 시 인스턴스 교체 또는 종료
    remaining_instances = [i for i in REDLIB_INSTANCES if i != working]

    for i, sub in enumerate(subreddits):
        posts = await _redlib_fetch_one(session, working, sub)

        if posts:
            all_posts.extend(posts[:limit_per_sub])
            success += 1
            consecutive_fails = 0
        else:
            consecutive_fails += 1

            if consecutive_fails >= max_consecutive_fails:
                # 현재 인스턴스가 죽었을 수 있음 → 다른 인스턴스 시도
                print(f"  [Redlib] ⚠️ 연속 {consecutive_fails}회 실패, 인스턴스 교체 시도...")
                new_working = None
                for fallback in remaining_instances:
                    test_posts = await _redlib_fetch_one(session, fallback, "todayilearned")
                    if test_posts:
                        new_working = fallback
                        break
                    await asyncio.sleep(REDLIB_DELAY_BETWEEN_INSTANCES)

                if new_working:
                    print(f"  [Redlib] 🔄 새 인스턴스: {new_working}")
                    working = new_working
                    remaining_instances = [i for i in remaining_instances if i != new_working]
                    consecutive_fails = 0
                else:
                    print(f"  [Redlib] ❌ 대체 인스턴스 없음, 중단 ({success}/{i+1} 성공)")
                    break

        # rate limit 방어: 서브레딧 간 대기
        if i < len(subreddits) - 1:
            await asyncio.sleep(REDLIB_DELAY_BETWEEN_SUBS)

    print(f"  Redlib: {len(subreddits)}개 서브레딧 중 {success}개 성공, {len(all_posts)}개 글")
    return all_posts


# ─── Reddit 방법 1: PullPush API ────────────────────────

async def fetch_reddit_pullpush(session: aiohttp.ClientSession, subreddit: str, limit: int = 10):
    after_epoch = int((datetime.now(timezone.utc) - timedelta(hours=48)).timestamp())
    url = (
        f"{PULLPUSH_BASE}/search/submission/"
        f"?subreddit={subreddit}&sort=desc&sort_type=score"
        f"&size={limit}&after={after_epoch}"
    )
    data = await fetch_json(session, url, PULLPUSH_HEADERS)
    if not data or "data" not in data:
        return []
    posts = []
    for d in data["data"]:
        if d.get("stickied") or d.get("removed_by_category"):
            continue
        permalink = d.get("permalink", f"/r/{subreddit}/comments/{d.get('id', '')}/")
        posts.append({
            "id": f"reddit_{d.get('id', '')}",
            "source": f"Reddit r/{subreddit}",
            "title": d.get("title", ""),
            "url": f"https://reddit.com{permalink}",
            "permalink": permalink,
            "score": d.get("score", 0),
            "comments": d.get("num_comments", 0),
            "hint": (d.get("selftext", "") or "")[:300],
            "thumbnail": _pick_reddit_image(d),
            "top_comments": [],
        })
    return posts


# ─── Reddit 방법 2: .json 직접 접근 ─────────────────────

async def fetch_reddit_direct(session: aiohttp.ClientSession, subreddit: str, limit: int = 10):
    endpoints = [
        f"https://old.reddit.com/r/{subreddit}/hot.json?limit={limit}&raw_json=1",
        f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}&raw_json=1",
    ]
    data = None
    for ep in endpoints:
        data = await fetch_json(session, ep, REDDIT_HEADERS)
        if data:
            break
        await asyncio.sleep(0.5)
    if not data:
        return []
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied"):
            continue
        posts.append({
            "id": f"reddit_{d.get('id', '')}",
            "source": f"Reddit r/{subreddit}",
            "title": d.get("title", ""),
            "url": f"https://reddit.com{d.get('permalink', '')}",
            "permalink": d.get("permalink", ""),
            "score": d.get("score", 0),
            "comments": d.get("num_comments", 0),
            "hint": (d.get("selftext", "") or "")[:300],
            "thumbnail": _pick_reddit_image(d),
            "top_comments": [],
        })
    return posts


# ─── Reddit 방법 3: .rss 피드 (최종 fallback) ───────────

def fetch_reddit_rss(subreddit: str, limit: int = 10) -> list:
    url = f"https://www.reddit.com/r/{subreddit}/.rss?limit={limit}"
    try:
        feed = feedparser.parse(url)
        if feed.bozo and not feed.entries:
            return []
    except Exception as e:
        print(f"  [WARN] Reddit RSS r/{subreddit} - {e}")
        return []

    posts = []
    for entry in feed.entries[:limit]:
        link = entry.get("link", "")
        title = entry.get("title", "")
        entry_id = entry.get("id", "")
        reddit_id = entry_id.split("_")[-1] if "_" in entry_id else str(hash(link) % 10**10)
        permalink = ""
        if "reddit.com" in link:
            permalink = link.split("reddit.com")[-1]
        thumb = ""
        if hasattr(entry, "content"):
            for c in entry.content:
                html = c.get("value", "")
                img_match = re.search(r'<img\s+src="([^"]+)"', html)
                if img_match:
                    thumb = img_match.group(1)
                    break
        if not thumb:
            media_thumb = entry.get("media_thumbnail", [])
            if media_thumb and isinstance(media_thumb, list):
                thumb = media_thumb[0].get("url", "")
        summary = entry.get("summary", "") or ""
        hint = re.sub(r'<[^>]+>', '', summary)[:300]
        posts.append({
            "id": f"reddit_{reddit_id}",
            "source": f"Reddit r/{subreddit}",
            "title": title,
            "url": link,
            "permalink": permalink,
            "score": 0,
            "comments": 0,
            "hint": hint,
            "thumbnail": thumb,
            "top_comments": [],
        })
    return posts


def fetch_reddit_rss_multi(subreddits: list, limit_per_sub: int = 8) -> list:
    all_posts = []
    chunk_size = 6

    for i in range(0, len(subreddits), chunk_size):
        chunk = subreddits[i:i + chunk_size]
        combined = "+".join(chunk)
        url = f"https://www.reddit.com/r/{combined}/hot/.rss?limit={limit_per_sub * len(chunk)}"
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                for sub in chunk:
                    all_posts.extend(fetch_reddit_rss(sub, limit=limit_per_sub))
                continue
        except Exception:
            for sub in chunk:
                all_posts.extend(fetch_reddit_rss(sub, limit=limit_per_sub))
            continue

        for entry in feed.entries:
            link = entry.get("link", "")
            title = entry.get("title", "")
            entry_id = entry.get("id", "")
            reddit_id = entry_id.split("_")[-1] if "_" in entry_id else str(hash(link) % 10**10)
            permalink = ""
            if "reddit.com" in link:
                permalink = link.split("reddit.com")[-1]
            source_sub = "unknown"
            if permalink:
                parts = permalink.split("/")
                if len(parts) >= 3 and parts[1] == "r":
                    source_sub = parts[2]
            thumb = ""
            if hasattr(entry, "content"):
                for c in entry.content:
                    html = c.get("value", "")
                    img_match = re.search(r'<img\s+src="([^"]+)"', html)
                    if img_match:
                        thumb = img_match.group(1)
                        break
            if not thumb:
                media_thumb = entry.get("media_thumbnail", [])
                if media_thumb and isinstance(media_thumb, list):
                    thumb = media_thumb[0].get("url", "")
            summary = entry.get("summary", "") or ""
            hint = re.sub(r'<[^>]+>', '', summary)[:300]
            all_posts.append({
                "id": f"reddit_{reddit_id}",
                "source": f"Reddit r/{source_sub}",
                "title": title,
                "url": link,
                "permalink": permalink,
                "score": 0,
                "comments": 0,
                "hint": hint,
                "thumbnail": thumb,
                "top_comments": [],
            })

    return all_posts


# ─── Reddit 통합 + 댓글 ─────────────────────────────────

async def fetch_reddit(session: aiohttp.ClientSession, subreddit: str, limit: int = 10, use_pullpush: bool = True):
    if use_pullpush:
        posts = await fetch_reddit_pullpush(session, subreddit, limit)
        if posts:
            return posts
    posts = await fetch_reddit_direct(session, subreddit, limit)
    if posts:
        return posts
    return fetch_reddit_rss(subreddit, limit)


async def fetch_reddit_comments_pullpush(session: aiohttp.ClientSession, submission_id: str, limit: int = 3):
    url = (
        f"{PULLPUSH_BASE}/search/comment/"
        f"?link_id={submission_id}&sort=desc&sort_type=score&size={limit}"
    )
    data = await fetch_json(session, url, PULLPUSH_HEADERS)
    if not data or "data" not in data:
        return []
    comments = []
    for c in data["data"]:
        body = (c.get("body", "") or "")[:200]
        if not body or c.get("stickied") or body == "[deleted]" or body == "[removed]":
            continue
        comments.append({"author": c.get("author", ""), "body": body, "score": c.get("score", 0)})
    comments.sort(key=lambda x: x["score"], reverse=True)
    return comments[:3]


async def fetch_reddit_comments_direct(session: aiohttp.ClientSession, permalink: str, limit: int = 3):
    endpoints = [
        f"https://old.reddit.com{permalink}.json?sort=top&limit={limit}&raw_json=1",
        f"https://www.reddit.com{permalink}.json?sort=top&limit={limit}&raw_json=1",
    ]
    data = None
    for ep in endpoints:
        data = await fetch_json(session, ep, REDDIT_HEADERS)
        if data:
            break
        await asyncio.sleep(0.5)
    if not data or len(data) < 2:
        return []
    comments = []
    for child in data[1].get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        c = child["data"]
        body = (c.get("body", "") or "")[:200]
        if not body or c.get("stickied"):
            continue
        comments.append({"author": c.get("author", ""), "body": body, "score": c.get("score", 0)})
    comments.sort(key=lambda x: x["score"], reverse=True)
    return comments[:3]


# ─── HN + 일반 RSS ──────────────────────────────────────

async def fetch_hackernews(session: aiohttp.ClientSession, limit: int = 15):
    ids = await fetch_json(session, "https://hacker-news.firebaseio.com/v0/topstories.json")
    if not ids:
        return []
    posts = []
    for item_id in ids[:limit]:
        item = await fetch_json(session, f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
        if not item or item.get("type") != "story":
            continue
        posts.append({
            "id": f"hn_{item_id}",
            "source": "Hacker News",
            "title": item.get("title", ""),
            "url": item.get("url", f"https://news.ycombinator.com/item?id={item_id}"),
            "score": item.get("score", 0),
            "comments": item.get("descendants", 0),
            "hint": "",
            "thumbnail": "",
        })
    return posts


def fetch_rss(feed_url: str, source_name: str, limit: int = 10):
    try:
        feed = feedparser.parse(feed_url)
        posts = []
        for entry in feed.entries[:limit]:
            link = entry.get("link", "")
            thumb = ""
            media_thumb = entry.get("media_thumbnail", [])
            if media_thumb and isinstance(media_thumb, list):
                thumb = media_thumb[0].get("url", "")
            if not thumb:
                media_content = entry.get("media_content", [])
                if media_content and isinstance(media_content, list):
                    for mc in media_content:
                        if mc.get("medium") == "image" or (mc.get("type", "").startswith("image")):
                            thumb = mc.get("url", "")
                            break
            if not thumb:
                enclosures = entry.get("enclosures", [])
                if enclosures:
                    for enc in enclosures:
                        if enc.get("type", "").startswith("image"):
                            thumb = enc.get("href", "") or enc.get("url", "")
                            break
            posts.append({
                "id": f"rss_{hash(link) % 10**10}",
                "source": source_name,
                "title": entry.get("title", ""),
                "url": link,
                "score": 0,
                "comments": 0,
                "hint": (entry.get("summary", "") or "")[:300],
                "thumbnail": thumb,
            })
        return posts
    except Exception as e:
        print(f"  [WARN] RSS {source_name} - {e}")
        return []


# ─── 소스별 수집 오케스트레이션 ─────────────────────────

REDDIT_SUBS = [
    "todayilearned", "science", "worldnews", "Futurology",
    "LifeProTips", "movies", "television", "food",
    "UpliftingNews", "explainlikeimfive",
    "Coffee", "tea", "whiskey", "CraftBeer", "fragrance",
    "puzzles", "DIY", "Breadit", "Baking", "knitting",
    "Embroidery", "cocktails", "minipainting", "modelmakers",
    "dadjokes", "tifu", "antiwork", "AmItheAsshole",
    "NoStupidQuestions",
]

RSS_SOURCES = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    ("https://feeds.reuters.com/reuters/topNews", "Reuters"),
    ("https://www.theverge.com/rss/index.xml", "The Verge"),
    ("https://feeds.npr.org/1001/rss.xml", "NPR"),
    ("https://www.nature.com/nature.rss", "Nature"),
    ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "NYT"),
]


async def collect_reddit(session: aiohttp.ClientSession) -> list:
    """Reddit 전체 수집: Redlib → PullPush → .json → .rss 순서로 시도"""

    # ── 1단계: Redlib HTML 파싱 (score + 댓글 수 포함) ──
    print("  [1/4] Redlib HTML 파싱 시도...")
    redlib_posts = await fetch_reddit_redlib(session, REDDIT_SUBS, limit_per_sub=8)
    if redlib_posts:
        print(f"  [Redlib] 수집 완료: {len(redlib_posts)}개 글 (score/댓글 포함)")
        # Redlib은 댓글 본문을 못 가져오므로, score 높은 글에 대해
        # PullPush/직접접근으로 댓글 수집 시도
        await _enrich_comments(session, redlib_posts)
        return redlib_posts

    # ── 2단계: PullPush API ──
    print("  [2/4] PullPush API 시도...")
    use_pullpush = False
    api_available = False
    test = await fetch_reddit_pullpush(session, "todayilearned", limit=2)
    if test:
        use_pullpush = True
        api_available = True
        print("  [OK] PullPush API 사용")
    else:
        # ── 3단계: .json 직접 접근 ──
        print("  [3/4] Reddit .json 직접 접근 시도...")
        test2 = await fetch_reddit_direct(session, "todayilearned", limit=2)
        if test2:
            api_available = True
            print("  [OK] Reddit .json 직접 접근 사용")
        else:
            # ── 4단계: .rss 피드 ──
            print("  [4/4] Reddit .rss 피드 시도...")
            test3 = fetch_reddit_rss("todayilearned", limit=2)
            if test3:
                print("  [OK] Reddit .rss 피드 사용 (score/댓글 수 없음)")
            else:
                print("  [WARN] Reddit 접근 완전 불가 - Reddit 건너뜀")
                return []

    all_posts = []
    reddit_posts = []
    reddit_ok = 0

    if api_available:
        for sub in REDDIT_SUBS:
            posts = await fetch_reddit(session, sub, limit=8, use_pullpush=use_pullpush)
            if posts:
                reddit_ok += 1
            all_posts.extend(posts)
            for p in posts:
                if p.get("permalink") or p.get("id"):
                    reddit_posts.append(p)
            await asyncio.sleep(2 if use_pullpush else 3)
    else:
        # RSS 일괄 수집
        rss_posts = fetch_reddit_rss_multi(REDDIT_SUBS, limit_per_sub=8)
        all_posts.extend(rss_posts)
        reddit_ok = len(set(p["source"] for p in rss_posts)) if rss_posts else 0

    print(f"  Reddit: {len(REDDIT_SUBS)}개 서브레딧 중 {reddit_ok}개 성공, {len(all_posts)}개 글")

    # 댓글 수집 (API 사용 가능 + score 있는 경우만)
    if reddit_posts and api_available:
        await _enrich_comments_api(session, reddit_posts, use_pullpush)

    return all_posts


async def _enrich_comments(session: aiohttp.ClientSession, posts: list):
    """Redlib으로 가져온 포스트의 상위 댓글을 PullPush/.json으로 보강."""
    # score 높은 상위 10개만
    scored = sorted(posts, key=lambda x: x.get("score", 0), reverse=True)
    top = scored[:30]

    # PullPush 가능 여부 테스트
    test = await fetch_reddit_pullpush(session, "todayilearned", limit=1)
    use_pullpush = bool(test)

    if not use_pullpush:
        # .json 직접 접근 테스트
        test2 = await fetch_reddit_direct(session, "todayilearned", limit=1)
        if not test2:
            print("  [댓글] API 접근 불가, 댓글 수집 건너뜀")
            return

    enriched = 0
    for p in top:
        try:
            if use_pullpush:
                sub_id = p["id"].replace("reddit_", "")
                comments = await fetch_reddit_comments_pullpush(session, sub_id)
            else:
                comments = await fetch_reddit_comments_direct(session, p["permalink"])
            if comments:
                p["top_comments"] = comments
                enriched += 1
        except Exception:
            pass
        await asyncio.sleep(2)

    if enriched:
        print(f"  [댓글] {enriched}/{len(top)}개 글에 댓글 추가")


async def _enrich_comments_api(session: aiohttp.ClientSession, reddit_posts: list, use_pullpush: bool):
    """PullPush/.json API로 가져온 포스트의 댓글 보강."""
    reddit_posts.sort(key=lambda x: x["score"], reverse=True)
    top_reddit = reddit_posts[:30]
    print(f"  댓글 수집: 상위 {len(top_reddit)}개 Reddit 글")
    for p in top_reddit:
        if use_pullpush:
            sub_id = p["id"].replace("reddit_", "")
            comments = await fetch_reddit_comments_pullpush(session, sub_id)
        else:
            comments = await fetch_reddit_comments_direct(session, p["permalink"])
        p["top_comments"] = comments
        await asyncio.sleep(2)


async def collect_fast(session: aiohttp.ClientSession) -> list:
    all_posts = []
    hn_posts = await fetch_hackernews(session, limit=15)
    all_posts.extend(hn_posts)
    print(f"  HN: {len(hn_posts)}개")
    rss_count = 0
    for url, name in RSS_SOURCES:
        posts = fetch_rss(url, name, limit=8)
        all_posts.extend(posts)
        rss_count += len(posts)
    print(f"  RSS: {rss_count}개 ({len(RSS_SOURCES)}개 피드)")
    return all_posts


async def collect_all(run_reddit: bool = True):
    async with aiohttp.ClientSession() as session:
        all_posts = []
        fast_posts = await collect_fast(session)
        all_posts.extend(fast_posts)
        if run_reddit:
            reddit_posts = await collect_reddit(session)
            all_posts.extend(reddit_posts)
        else:
            print("  Reddit: 이번 실행 건너뜀")
    return all_posts


# ─── 데이터 누적 ────────────────────────────────────────

def load_existing() -> dict:
    if POSTS_FILE.exists():
        try:
            with open(POSTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            pass
    return {"posts": {}, "last_crawl": None}


def merge_posts(existing: dict, new_posts: list) -> dict:
    posts = existing.get("posts", {})
    for p in new_posts:
        pid = p["id"]
        if pid in posts:
            posts[pid]["score"] = max(posts[pid].get("score", 0), p["score"])
            posts[pid]["comments"] = max(posts[pid].get("comments", 0), p["comments"])
            posts[pid]["seen_count"] = posts[pid].get("seen_count", 1) + 1
            if not posts[pid].get("thumbnail") and p.get("thumbnail"):
                posts[pid]["thumbnail"] = p["thumbnail"]
            new_comments = p.get("top_comments", [])
            if new_comments:
                existing_comments = posts[pid].get("top_comments", [])
                all_comments = {c["body"][:50]: c for c in existing_comments + new_comments}
                merged_comments = sorted(all_comments.values(), key=lambda x: x["score"], reverse=True)
                posts[pid]["top_comments"] = merged_comments[:3]
        else:
            p["seen_count"] = 1
            p["first_seen"] = datetime.now(timezone.utc).isoformat()
            posts[pid] = p
    existing["posts"] = posts
    existing["last_crawl"] = datetime.now(timezone.utc).isoformat()
    return existing


def save_data(data: dict):
    DATA_DIR.mkdir(exist_ok=True)
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─── 메인 ────────────────────────────────────────────────

async def main():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    state = load_state()
    run_count = state["run_count"]
    run_reddit = should_run_reddit(state)
    sources_label = "HN+RSS+Reddit" if run_reddit else "HN+RSS"
    print(f"[크롤링 #{run_count}] {now} ({sources_label})")

    new_posts = await collect_all(run_reddit=run_reddit)
    print(f"  수집 합계: {len(new_posts)}개")

    existing = load_existing()
    before = len(existing.get("posts", {}))
    merged = merge_posts(existing, new_posts)
    after = len(merged["posts"])
    print(f"  누적: {before} → {after}개 (신규 {after - before}개)")

    save_data(merged)
    print(f"  저장 완료: {POSTS_FILE}")

    state["run_count"] = run_count + 1
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    if run_reddit:
        state["last_reddit"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    next_reddit = REDDIT_EVERY_N - ((run_count + 1) % REDDIT_EVERY_N)
    if next_reddit == REDDIT_EVERY_N:
        next_reddit = 0
    print(f"  다음 Reddit 크롤링: {'지금 완료' if run_reddit else f'{next_reddit}회 후'}")


if __name__ == "__main__":
    asyncio.run(main())
