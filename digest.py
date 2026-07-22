"""
다이제스트 - 아침에 실행되어 누적 데이터를 AI로 정리하고 Discord에 전송
전송 후 데이터 초기화

변경사항:
- load_and_rank() → 소스별 균등 배분으로 변경 (HN 독점 방지)
- Gemini 프롬프트: 개수 제한 대폭 완화, "선별"보다 "중복 제거 + 요약"에 집중
- 대량 글 처리를 위해 배치 분할 요약
- Discord 역순 전송 + 글 번호 + 목차 + 카테고리 점프 링크
- [FIX] Gemini 응답에서 headline 등 필드 누락 시 KeyError 방지 (방어적 .get() 처리)
- [FIX] 분류 단계도 50개씩 배치 처리 (JSON 응답 토큰 초과로 잘림 방지)
"""

import os
import re
import json
import asyncio
import aiohttp
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
POSTS_FILE = DATA_DIR / "posts.json"

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DISCORD_TREND_WEBHOOK = os.environ["DISCORD_TREND_WEBHOOK"]

GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.5-flash-lite:generateContent"
)

CATEGORY_EMOJI = {
    "테크/AI": "🤖",
    "과학/건강": "🔬",
    "세계 이슈": "🌍",
    "문화/라이프": "🎬",
    "신기한 사실": "💡",
    "생활/팁": "✨",
    "취미/덕질": "☕",
    "유머/썰": "😂",
}


# ─── 웹훅 URL에서 guild_id, channel_id 추출 ────────────

def _parse_webhook_url(webhook_url: str) -> tuple:
    """웹훅 URL에서 (webhook_id, webhook_token) 추출.
    형식: https://discord.com/api/webhooks/{id}/{token}
    """
    m = re.search(r"/webhooks/(\d+)/([A-Za-z0-9_-]+)", webhook_url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


async def _get_webhook_info(session: aiohttp.ClientSession) -> tuple:
    """웹훅 API를 호출하여 (guild_id, channel_id) 반환."""
    wh_id, wh_token = _parse_webhook_url(DISCORD_TREND_WEBHOOK)
    if not wh_id:
        return None, None
    url = f"https://discord.com/api/webhooks/{wh_id}/{wh_token}"
    try:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("guild_id"), data.get("channel_id")
    except Exception as e:
        print(f"  [WARN] 웹훅 정보 조회 실패: {e}")
    return None, None


# ─── 데이터 로드 & 소스별 균등 배분 ─────────────────────

def load_and_rank() -> list:
    """
    누적 데이터를 로드하고 소스별로 균등 배분하여 반환.
    HN이 score 높다고 독점하지 않도록 소스 그룹별 상한을 둔다.
    """
    if not POSTS_FILE.exists():
        return []

    with open(POSTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    posts = list(data.get("posts", {}).values())

    # trend_score 계산 (소스 내 정렬용)
    for p in posts:
        p["trend_score"] = (
            p.get("score", 0)
            + p.get("comments", 0) * 2
            + p.get("seen_count", 1) * 50
        )

    # 소스 그룹 분류
    hn_posts = []
    rss_posts = []
    reddit_posts = []
    for p in posts:
        src = p.get("source", "")
        if src == "Hacker News":
            hn_posts.append(p)
        elif src.startswith("Reddit"):
            reddit_posts.append(p)
        else:
            rss_posts.append(p)

    # 각 그룹 내에서 trend_score 순 정렬
    hn_posts.sort(key=lambda x: x["trend_score"], reverse=True)
    rss_posts.sort(key=lambda x: x["trend_score"], reverse=True)
    reddit_posts.sort(key=lambda x: x["trend_score"], reverse=True)

    # 소스별 상한: 전체 많이 가져가되, 한 소스가 독점하지 않도록
    # 총 목표 ~150개 (Gemini가 분류/중복제거 후 줄여줌)
    MAX_PER_GROUP = 50
    selected = (
        hn_posts[:MAX_PER_GROUP]
        + rss_posts[:MAX_PER_GROUP]
        + reddit_posts[:MAX_PER_GROUP]
    )

    # 전체를 다시 trend_score 순 정렬 (Gemini 입력용)
    selected.sort(key=lambda x: x["trend_score"], reverse=True)

    return selected


# ─── Gemini API ──────────────────────────────────────────

async def call_gemini(prompt: str, max_retries: int = 3) -> str:
    url = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 8192,
        },
    }
    async with aiohttp.ClientSession() as session:
        for attempt in range(max_retries):
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            return candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                        return ""

                    text = await resp.text()
                    if resp.status in (429, 503) and attempt < max_retries - 1:
                        wait = (attempt + 1) * 15
                        print(f"  [RETRY] Gemini {resp.status} - {wait}초 후 재시도 ({attempt+1}/{max_retries})")
                        await asyncio.sleep(wait)
                        continue

                    print(f"[ERROR] Gemini: {resp.status} - {text[:300]}")
                    return ""
            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 10
                    print(f"  [RETRY] Gemini 타임아웃 - {wait}초 후 재시도 ({attempt+1}/{max_retries})")
                    await asyncio.sleep(wait)
                    continue
                print("[ERROR] Gemini: 타임아웃 (최종 실패)")
                return ""
    return ""


def parse_json_response(text: str) -> list:
    r"""
    Gemini 응답에서 JSON 추출.
    LLM이 생성한 문자열에는 \n, \", \\, \/ 외의 잘못된 escape가
    포함될 수 있으므로 (예: \e, \s, \p 등) 이를 정리한 뒤 파싱한다.
    """
    text = text.strip()

    # 1) 마크다운 코드 블록 제거
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    # 2) JSON 배열 범위만 추출 (앞뒤 잡다한 텍스트 제거)
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start:end + 1]

    # 3) 첫 시도: 그대로 파싱
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 4) 잘못된 escape 시퀀스 수정
    def fix_invalid_escapes(s: str) -> str:
        return re.sub(
            r'\\(?!["\\/bfnrtu])',
            r'\\\\',
            s,
        )

    text_fixed = fix_invalid_escapes(text)
    try:
        return json.loads(text_fixed)
    except json.JSONDecodeError:
        pass

    # 5) 더 공격적인 수정: 잘못된 escape를 아예 제거
    def strip_invalid_escapes(s: str) -> str:
        return re.sub(
            r'\\(?!["\\/bfnrtu])',
            '',
            s,
        )

    text_stripped = strip_invalid_escapes(text)
    try:
        return json.loads(text_stripped)
    except json.JSONDecodeError as e:
        print(f"  [DEBUG] parse_json_response 최종 실패")
        print(f"  [DEBUG] 에러: {e}")
        print(f"  [DEBUG] 정리된 텍스트 앞부분: {text_stripped[:300]}")
        raise


# ─── 분류 단계 배치 처리 ─────────────────────────────────

def _build_posts_text(posts: list, index_offset: int = 0) -> str:
    """posts 목록을 Gemini 분류 프롬프트용 텍스트로 변환.
    index_offset: 원본 배열 기준 인덱스를 유지하기 위한 오프셋.
    """
    posts_text = ""
    for i, p in enumerate(posts):
        actual_idx = index_offset + i
        posts_text += (
            f"[{actual_idx}] ({p['source']}) {p['title']} "
            f"[score:{p.get('score',0)}, comments:{p.get('comments',0)}, "
            f"seen:{p.get('seen_count',1)}]\n"
        )
        if p.get("hint"):
            posts_text += f"    {p['hint'][:300]}\n"
    return posts_text


def _build_classify_prompt(posts_text: str) -> str:
    return f"""아래는 해외 소스(Hacker News, RSS 뉴스, Reddit)에서 하루 동안 수집한 글 목록이야.

★ 핵심 목표: 중복만 제거하고, 가능한 한 모든 글을 살려서 분류해.
  "선별"하지 말고, 명백히 무의미한 글만 빼.

카테고리:
- 테크/AI: 일상에 영향 주는 기술 트렌드 (코딩/프로그래밍 전문 글은 제외)
- 과학/건강: 연구 결과, 건강 정보, 심리학
- 세계 이슈: 국제 뉴스, 경제, 사회 변화
- 문화/라이프: 영화, 드라마, 음식, 여행, 바이럴
- 신기한 사실: TIL, 잡학, 대화 소재
- 생활/팁: 유용한 생활 정보
- 취미/덕질: 커피, 차, 위스키, 맥주, 향수, 퍼즐, DIY 등 취미 관련 화제
- 유머/썰: 아재개그, 웃긴 실수담, 직장 썰, 관계 판정, 황당한 질문

규칙:
1. 개발/프로그래밍/코딩 전문 글만 제외 (일반인이 이해 불가능한 것)
2. 비슷한 주제가 여러 소스에 있으면 하나로 합쳐 (merged_indices에 기록)
3. ★ 개수 제한 없음 — 중복 제거 후 남는 글은 전부 포함해
4. HN, RSS, Reddit 소스를 골고루 포함해. 특정 소스 글만 남기지 마
5. score가 0인 RSS/Reddit 글도 제목이 흥미로우면 반드시 포함
6. index는 입력에 표시된 번호를 그대로 사용해 (오프셋 포함)

글 목록:
{posts_text}

JSON 배열로만 응답해. 다른 텍스트 없이 JSON만:
[{{"index": 번호, "category": "카테고리명", "merged_indices": [합쳐진 번호들]}}]"""


async def _classify_in_batches(posts: list) -> list:
    """
    분류 단계를 CLASSIFY_BATCH_SIZE개씩 나눠서 처리.
    각 배치에서 반환된 index는 원본 posts 배열 기준 절대 인덱스.
    배치 간 중복은 merged_indices 기준으로 후처리에서 제거.
    """
    CLASSIFY_BATCH_SIZE = 50

    all_selected = []
    total_batches = (len(posts) + CLASSIFY_BATCH_SIZE - 1) // CLASSIFY_BATCH_SIZE

    for batch_num, c_start in enumerate(range(0, len(posts), CLASSIFY_BATCH_SIZE), start=1):
        c_batch = posts[c_start:c_start + CLASSIFY_BATCH_SIZE]
        print(f"  분류 배치 {batch_num}/{total_batches} ({len(c_batch)}개, index {c_start}~{c_start+len(c_batch)-1})...")

        posts_text = _build_posts_text(c_batch, index_offset=c_start)
        prompt = _build_classify_prompt(posts_text)

        result = await call_gemini(prompt)
        try:
            batch_selected = parse_json_response(result)
        except Exception as e:
            print(f"  [ERROR] 분류 배치 {batch_num} 파싱 실패: {e}")
            print(f"    Raw: {result[:500]}")
            # 실패한 배치는 건너뛰되 계속 진행
            if c_start + CLASSIFY_BATCH_SIZE < len(posts):
                await asyncio.sleep(5)
            continue

        # 인덱스 범위 검증: 해당 배치 범위를 벗어난 index는 무시
        valid_range = range(c_start, c_start + len(c_batch))
        for item in batch_selected:
            idx = item.get("index", -1)
            if idx in valid_range:
                all_selected.append(item)
            else:
                print(f"  [WARN] 분류 배치 {batch_num}: index={idx}가 배치 범위({valid_range.start}~{valid_range.stop-1}) 밖 → 스킵")

        print(f"    → 배치 {batch_num} 분류 결과: {len([i for i in batch_selected if i.get('index',-1) in valid_range])}개 선택")

        # 배치 간 rate limit 대비 대기
        if c_start + CLASSIFY_BATCH_SIZE < len(posts):
            await asyncio.sleep(5)

    # 배치 간 중복 제거: merged_indices에 이미 포함된 index를 제거
    all_merged = set()
    for item in all_selected:
        for merged_idx in item.get("merged_indices", []):
            all_merged.add(merged_idx)

    deduped = [item for item in all_selected if item["index"] not in all_merged]

    print(f"  → 전체 분류 완료: {len(all_selected)}개 → 배치 간 중복 제거 후 {len(deduped)}개")
    return deduped


# ─── 분류 + 요약 ─────────────────────────────────────────

async def classify_and_summarize(posts: list) -> list:
    """수집한 글들을 분류/중복제거/요약 — 최대한 전부 포함"""

    # ── 1단계: 분류 + 중복 제거 (배치 처리) ──
    selected = await _classify_in_batches(posts)

    if not selected:
        return []

    # 2단계: 한글 요약 — 배치 분할 처리
    selected_posts = []
    for item in selected:
        idx = item.get("index", -1)
        if 0 <= idx < len(posts):
            post = posts[idx].copy()
            post["category"] = item["category"]
            selected_posts.append(post)

    if not selected_posts:
        return []

    print(f"  → 분류 완료: {len(selected_posts)}개 (중복 제거 후)")

    # 15개씩 배치로 나눠서 요약 (Gemini 토큰 한도 대응)
    BATCH_SIZE = 15
    all_final = []

    for batch_start in range(0, len(selected_posts), BATCH_SIZE):
        batch = selected_posts[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (len(selected_posts) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"  요약 배치 {batch_num}/{total_batches} ({len(batch)}개)...")

        summary_input = ""
        for i, p in enumerate(batch):
            summary_input += (
                f"[{i}] 카테고리: {p['category']}\n"
                f"    소스: {p['source']}\n"
                f"    제목: {p['title']}\n"
                f"    URL: {p['url']}\n"
            )
            if p.get("hint"):
                summary_input += f"    내용: {p['hint'][:500]}\n"
            if p.get("top_comments"):
                summary_input += "    베스트 댓글:\n"
                for c in p["top_comments"][:3]:
                    summary_input += f"      - u/{c['author']} ({c['score']}점): {c['body'][:150]}\n"
            summary_input += "\n"

        summary_prompt = f"""각 글을 한국어로 요약해줘:
1. headline: 흥미를 끄는 핵심 한줄 (15~25자). 구체적 사실을 넣어 (숫자, 고유명사 등)
2. detail: 상세 설명 5-7줄. 반드시 아래 규칙을 지켜:

   ★ 절대 금지: "~에 중요한 정보를 제공합니다", "~를 보여줍니다", "~에 기여할 수 있습니다" 같은 빈말.
     이런 문장은 정보가 0이므로 절대 쓰지 마.

   ★ 필수: 육하원칙(누가/언제/어디서/무엇을/어떻게/왜)에 맞는 구체적 사실만 써.
     - 제목과 내용에 나온 숫자, 이름, 장소, 연구결과 등 구체적 정보를 반드시 포함
     - "수소 농도가 변했다"가 아니라 "1100년간 H2 농도가 X에서 Y로 Z% 증가했다" 식으로
     - 내용에 구체적 수치가 없으면 최소한 무슨 일이 일어났는지 명확하게 서술

   ★ 구조 (이 순서대로):
     1문장: 무슨 일인지 한줄 팩트 (누가 뭘 했는지/발견했는지)
     2-3문장: 구체적 내용 (연구면 방법과 결과, 뉴스면 경위와 영향, 팁이면 구체적 방법)
     4-5문장: 의미나 영향 (왜 중요한지, 어떤 변화가 예상되는지)

3. best_comments: 베스트 댓글이 있으면 최대 2개를 한국어로 번역해줘.
   - 원문 내용을 그대로 번역 (요약하거나 의역하지 말고, 원래 댓글 톤 그대로)
   - 형식: "닉네임: 댓글 내용" (예: "u/someone: 이거 완전 내 얘기인데ㅋㅋ")
   - 없으면 빈 배열

톤: 뉴스 브리핑처럼 간결하고 정보 전달 중심으로. 사실관계를 정확하게.
주의:
- 제공된 '내용'과 '베스트 댓글'에 나온 구체적 정보를 최대한 활용해서 써.
- 모르는 건 지어내지 말고, 아는 범위에서 최대한 구체적으로.
- JSON 문자열 안에서 백슬래시(\\)를 쓸 때는 반드시 이중 이스케이프(\\\\)로 써.
- 줄바꿈이 필요하면 \\n을 써.
- 반드시 모든 항목에 index, headline, detail, best_comments 키를 포함해야 함.

{summary_input}

JSON 배열로만 응답해. 다른 텍스트 없이 JSON만:
[{{"index": 번호, "headline": "한줄 제목", "detail": "상세 설명 5-7줄", "best_comments": ["댓글1 번역", "댓글2 번역"]}}]"""

        result = await call_gemini(summary_prompt)
        try:
            summaries = parse_json_response(result)
        except Exception as e:
            print(f"  [ERROR] 요약 배치 {batch_num} 파싱 실패: {e}")
            print(f"    Raw: {result[:500]}")
            continue

        # ── [FIX] 방어적 필드 접근: headline 등 누락 시 KeyError 방지 ──
        for s in summaries:
            idx = s.get("index", -1)
            if not (0 <= idx < len(batch)):
                print(f"  [WARN] 배치 {batch_num}: index={idx} 범위 초과, 스킵")
                continue

            # headline: 누락 시 대체 키 순서대로 탐색, 모두 없으면 스킵
            headline = (
                s.get("headline")
                or s.get("title")
                or s.get("summary")
                or ""
            ).strip()
            if not headline:
                print(f"  [WARN] 배치 {batch_num} index={idx}: headline 누락, 스킵")
                continue

            # detail: 누락 시 빈 문자열 fallback
            detail = (
                s.get("detail")
                or s.get("summary")
                or ""
            ).strip()
            if not detail:
                print(f"  [WARN] 배치 {batch_num} index={idx}: detail 누락")

            post = batch[idx]
            all_final.append({
                "category": post["category"],
                "headline": headline,
                "detail": detail,
                "best_comments": s.get("best_comments") or [],
                "url": post["url"],
                "source": post["source"],
                "thumbnail": post.get("thumbnail", ""),
            })

        # 배치 간 rate limit 대비 대기
        if batch_start + BATCH_SIZE < len(selected_posts):
            await asyncio.sleep(5)

    return all_final


# ─── Discord 전송 (역순 + 점프 링크) ────────────────────

async def _send_webhook(session: aiohttp.ClientSession, payload: dict) -> dict | None:
    """웹훅으로 메시지 전송, ?wait=true로 메시지 ID 포함 응답 받기."""
    url = DISCORD_TREND_WEBHOOK
    # ?wait=true 추가 (이미 쿼리 파라미터가 있을 수 있으므로)
    sep = "&" if "?" in url else "?"
    url = f"{url}{sep}wait=true"
    try:
        async with session.post(url, json=payload) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 204:
                return None
            else:
                text = await resp.text()
                print(f"[WARN] Discord: {resp.status} - {text[:200]}")
                return None
    except Exception as e:
        print(f"[WARN] Discord 전송 실패: {e}")
        return None


async def send_to_discord(digest: list):
    kst = timezone(timedelta(hours=9))
    today = datetime.now(kst).strftime("%Y.%m.%d (%a)")

    by_category = {}
    for item in digest:
        cat = item["category"]
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)

    # 소스 통계
    source_counts = {}
    for item in digest:
        src = item["source"].split(" r/")[0]  # "Reddit r/xxx" → "Reddit"
        source_counts[src] = source_counts.get(src, 0) + 1
    stats = " / ".join(f"{k} {v}" for k, v in sorted(source_counts.items()))

    # 카테고리 순서 고정
    category_order = [
        "테크/AI", "과학/건강", "세계 이슈", "문화/라이프",
        "신기한 사실", "생활/팁", "취미/덕질", "유머/썰",
    ]

    # ── 글 번호 매기기 (정순 기준) ──
    global_num = 1
    item_numbers = {}  # id(item) → 번호
    cat_ranges = {}    # category → (start, end)
    for category in category_order:
        items = by_category.get(category)
        if not items:
            continue
        start = global_num
        for item in items:
            item_numbers[id(item)] = global_num
            global_num += 1
        cat_ranges[category] = (start, global_num - 1)

    async with aiohttp.ClientSession() as session:
        # guild_id, channel_id 조회 (점프 링크 생성용)
        guild_id, channel_id = await _get_webhook_info(session)
        can_jump = bool(guild_id and channel_id)
        if can_jump:
            print(f"  점프 링크 활성: guild={guild_id}, channel={channel_id}")
        else:
            print("  [WARN] guild/channel ID 조회 실패, 점프 링크 비활성")

        # 카테고리별 첫 글 메시지 ID 저장 (점프 링크용)
        cat_first_message_id = {}  # category → message_id

        # ── 역순 전송: 마지막 카테고리부터 ──
        for category in reversed(category_order):
            items = by_category.get(category)
            if not items:
                continue

            emoji = CATEGORY_EMOJI.get(category, "📌")
            first_msg_id_for_cat = None

            # 카테고리 내 아이템도 역순
            for item in reversed(items):
                num = item_numbers[id(item)]
                description = f"||{item['detail']}||"
                description += f"\n\n🔗 [원문 보기]({item['url']})  •  📡 {item['source']}"

                embed = {
                    "title": f"{emoji} #{num}  {item['headline']}",
                    "description": description,
                    "color": _category_color(category),
                }

                thumb = item.get("thumbnail", "")
                if thumb and thumb.startswith("http"):
                    if any(thumb.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp")):
                        embed["image"] = {"url": thumb}
                    else:
                        embed["thumbnail"] = {"url": thumb}

                resp_data = await _send_webhook(session, {"embeds": [embed]})
                if resp_data and resp_data.get("id"):
                    # 역순이므로 마지막에 전송된 게 정순 첫 글
                    first_msg_id_for_cat = resp_data["id"]

                await asyncio.sleep(1)

                # 베스트 댓글
                if item.get("best_comments"):
                    comments_lines = []
                    for c in item["best_comments"]:
                        if c:
                            comments_lines.append(f"💬 {c}")
                    if comments_lines:
                        await _send_webhook(session, {"content": "\n".join(comments_lines)})
                        await asyncio.sleep(0.5)

            # 카테고리 첫 글 메시지 ID 저장
            if first_msg_id_for_cat:
                cat_first_message_id[category] = first_msg_id_for_cat

            # 카테고리 구분선 (아이템 뒤에 = 디스코드에서는 위에 표시됨)
            s, e = cat_ranges[category]
            divider = {"content": f"─── {emoji} **{category}** #{s}~#{e} ({len(items)}개) ───"}
            await _send_webhook(session, divider)
            await asyncio.sleep(0.5)

        # ── 헤더 (맨 마지막 전송 = 디스코드에서 맨 아래) ──
        toc_lines = []
        for category in category_order:
            if category not in cat_ranges:
                continue
            emoji = CATEGORY_EMOJI.get(category, "📌")
            s, e = cat_ranges[category]
            cnt = e - s + 1

            # 점프 링크 생성
            msg_id = cat_first_message_id.get(category)
            if can_jump and msg_id:
                jump_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{msg_id}"
                toc_lines.append(f"{emoji} [{category}: **#{s}~#{e}** ({cnt}개)]({jump_url})")
            else:
                toc_lines.append(f"{emoji} {category}: **#{s}~#{e}** ({cnt}개)")

        header = {
            "embeds": [{
                "title": f"📰 오늘의 트렌드  |  {today}",
                "description": (
                    f"해외에서 화제가 되고 있는 소식 **{len(digest)}개**를 모았습니다.\n"
                    f"📡 {stats}\n\n"
                    + "\n".join(toc_lines)
                    + "\n\n↑ 카테고리를 클릭하면 해당 위치로 점프합니다!"
                ),
                "color": 0x5865F2,
            }]
        }
        await _send_webhook(session, header)


def _category_color(category: str) -> int:
    return {
        "테크/AI": 0x00D4AA,
        "과학/건강": 0x3498DB,
        "세계 이슈": 0xE74C3C,
        "문화/라이프": 0xF39C12,
        "신기한 사실": 0x9B59B6,
        "생활/팁": 0x2ECC71,
        "취미/덕질": 0xE67E22,
        "유머/썰": 0xF1C40F,
    }.get(category, 0x95A5A6)


# ─── 데이터 초기화 ───────────────────────────────────────

def clear_data():
    DATA_DIR.mkdir(exist_ok=True)
    with open(POSTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"posts": {}, "last_crawl": None}, f)
    print("[INFO] 데이터 초기화 완료")


# ─── 메인 ────────────────────────────────────────────────

async def main():
    kst = timezone(timedelta(hours=9))
    now = datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")
    print(f"{'='*50}")
    print(f"Daily Trend Digest - {now}")
    print(f"{'='*50}")

    print("\n[1/4] 누적 데이터 로드...")
    posts = load_and_rank()
    if not posts:
        print("[ERROR] 누적 데이터가 없습니다. 크롤링이 실행되었는지 확인하세요.")
        return

    # 소스별 통계 출력
    source_stats = {}
    for p in posts:
        src = p["source"].split(" r/")[0]
        source_stats[src] = source_stats.get(src, 0) + 1
    stats_str = ", ".join(f"{k}: {v}" for k, v in sorted(source_stats.items()))
    print(f"  → {len(posts)}개 글 로드됨 ({stats_str})")

    print("\n[2/4] AI 분류/요약 중...")
    digest = await classify_and_summarize(posts)
    if not digest:
        print("[ERROR] 요약 결과가 없습니다.")
        return
    print(f"  → 최종 {len(digest)}개 토픽")
    for item in digest:
        print(f"  [{item['category']}] {item['headline']} ({item['source']})")

    print("\n[3/4] Discord 전송 중...")
    await send_to_discord(digest)

    print("\n[4/4] 데이터 초기화...")
    clear_data()

    print("\n✅ 완료!")


if __name__ == "__main__":
    asyncio.run(main())
