#!/usr/bin/env python3
"""
Redlib 인스턴스 HTML 파싱 테스트
GitHub Actions에서 실행하여 score/댓글 수 추출 가능 여부 확인

사용법:
  pip install requests beautifulsoup4
  python test_redlib.py
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import sys

# ── Redlib 인스턴스 목록 (2025년 활성 인스턴스) ──
REDLIB_INSTANCES = [
    "https://safereddit.com",
    "https://redlib.tux.pizza",
    "https://redlib.catsarch.com",
    "https://redlib.privacyredirect.com",
    "https://redlib.r4fo.com",
    "https://reddit.rtrace.io",
    "https://redlib.perennialte.ch",
    "https://red.ngn.tf",
    "https://redlib.4o1x5.dev",
    "https://eu.safereddit.com",
    "https://redlib.thebunny.zone",
    "https://reddit.adminforge.de",
    "https://reddit.nerdvpn.de",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

TEST_SUBREDDIT = "todayilearned"


def test_html_parsing(base_url, subreddit=TEST_SUBREDDIT):
    """Redlib HTML 파싱 테스트 — score, 댓글 수, 제목 추출 시도"""
    url = f"{base_url}/r/{subreddit}/hot"
    print(f"\n{'='*60}")
    print(f"[TEST] {url}")
    print(f"{'='*60}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        print(f"  HTTP {resp.status_code} | {len(resp.text)} bytes")

        if resp.status_code != 200:
            print(f"  ❌ 접근 실패 (HTTP {resp.status_code})")
            # 에러 페이지 내용 일부 출력
            if resp.text:
                soup = BeautifulSoup(resp.text, "html.parser")
                err_text = soup.get_text(strip=True)[:300]
                print(f"  에러 내용: {err_text}")
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # ── 1단계: HTML 구조 탐색 ──
        print("\n  [구조 분석]")

        # post 컨테이너 후보들
        post_selectors = [
            ("div.post", "div.post"),
            ("div.link", "div.link"),
            ("div#siteTable > div", "siteTable children"),
            ("article", "article"),
            (".thing", ".thing"),
            (".post-container", ".post-container"),
            ("[class*='post']", "class contains 'post'"),
            ("[class*='link']", "class contains 'link'"),
            ("[data-fullname]", "data-fullname attr"),
        ]

        found_posts = None
        found_selector = None

        for selector, label in post_selectors:
            elements = soup.select(selector)
            if elements:
                print(f"  ✅ {label}: {len(elements)}개 발견")
                if len(elements) >= 3 and not found_posts:
                    found_posts = elements
                    found_selector = label
            else:
                print(f"  · {label}: 없음")

        if not found_posts:
            # 범용 탐색: class에 post가 포함된 모든 div
            all_divs = soup.find_all("div")
            post_like = [d for d in all_divs if d.get("class") and
                         any("post" in c.lower() for c in d.get("class", []))]
            if post_like:
                print(f"  🔎 범용 탐색: post-like div {len(post_like)}개")
                found_posts = post_like[:10]
                found_selector = "generic post-like"

        # ── 2단계: 첫 번째 포스트 상세 분석 ──
        if found_posts:
            print(f"\n  [포스트 상세 분석] (selector: {found_selector})")
            for i, post in enumerate(found_posts[:3]):
                print(f"\n  --- 포스트 #{i+1} ---")
                # 클래스 출력
                classes = post.get("class", [])
                print(f"  classes: {classes}")
                print(f"  id: {post.get('id', 'none')}")

                # 전체 텍스트 (줄여서)
                text = post.get_text(separator=" | ", strip=True)[:200]
                print(f"  text: {text}")

                # score 후보 탐색
                score_selectors = [
                    ".score", ".likes", ".points", ".votes",
                    "[class*='score']", "[class*='vote']", "[class*='point']",
                    ".post_score", ".post-score", ".post_votes",
                ]
                for sel in score_selectors:
                    score_el = post.select_one(sel)
                    if score_el:
                        print(f"  🎯 SCORE [{sel}]: '{score_el.get_text(strip=True)}'")
                        print(f"     attrs: {dict(score_el.attrs)}")

                # 댓글 수 후보 탐색
                comment_selectors = [
                    ".comments", "[class*='comment']",
                    "a[href*='comments']",
                ]
                for sel in comment_selectors:
                    comment_els = post.select(sel)
                    for cel in comment_els[:2]:
                        txt = cel.get_text(strip=True)
                        if txt:
                            print(f"  💬 COMMENTS [{sel}]: '{txt}'")
                            print(f"     attrs: {dict(cel.attrs)}")

                # 제목 후보
                title_selectors = [
                    "a.post_title", "a[class*='title']", "h2 a", "h3 a",
                    ".post_title", ".title", "p.post_title",
                ]
                for sel in title_selectors:
                    title_el = post.select_one(sel)
                    if title_el:
                        print(f"  📌 TITLE [{sel}]: '{title_el.get_text(strip=True)[:100]}'")
                        href = title_el.get("href", "")
                        if href:
                            print(f"     href: {href}")

                # 작성자
                author_selectors = [
                    "a[class*='author']", ".author", "[class*='author']",
                ]
                for sel in author_selectors:
                    auth_el = post.select_one(sel)
                    if auth_el:
                        print(f"  👤 AUTHOR [{sel}]: '{auth_el.get_text(strip=True)}'")

        # ── 3단계: 전체 HTML에서 패턴 추출 ──
        print(f"\n  [전체 HTML 패턴 분석]")

        # score 패턴
        all_score = soup.select("[class*='score']")
        print(f"  *score* class 요소: {len(all_score)}개")
        for s in all_score[:3]:
            print(f"    tag={s.name}, class={s.get('class')}, text='{s.get_text(strip=True)[:50]}'")

        # vote 패턴
        all_vote = soup.select("[class*='vote']")
        print(f"  *vote* class 요소: {len(all_vote)}개")
        for v in all_vote[:3]:
            print(f"    tag={v.name}, class={v.get('class')}, text='{v.get_text(strip=True)[:50]}'")

        # comment 링크
        all_comment_links = soup.select("a[href*='/comments/']")
        print(f"  comments 링크: {len(all_comment_links)}개")
        for c in all_comment_links[:3]:
            print(f"    text='{c.get_text(strip=True)[:50]}', href={c.get('href','')[:80]}")

        # ── 4단계: Raw HTML 샘플 (첫 포스트) ──
        if found_posts:
            print(f"\n  [Raw HTML 샘플 - 첫 포스트]")
            raw = str(found_posts[0])
            # 2000자로 제한
            if len(raw) > 2000:
                print(f"  (총 {len(raw)}자, 앞 2000자만)")
                raw = raw[:2000]
            print(raw)

        return {
            "url": base_url,
            "status": resp.status_code,
            "posts_found": len(found_posts) if found_posts else 0,
            "selector": found_selector,
            "has_score": len(all_score) > 0,
            "has_comments": len(all_comment_links) > 0,
        }

    except requests.exceptions.Timeout:
        print(f"  ❌ 타임아웃 (15초)")
        return None
    except requests.exceptions.ConnectionError as e:
        print(f"  ❌ 연결 실패: {e}")
        return None
    except Exception as e:
        print(f"  ❌ 예외: {e}")
        return None


def test_json_endpoint(base_url, subreddit=TEST_SUBREDDIT):
    """혹시 JSON 엔드포인트가 있는지 시도"""
    json_urls = [
        f"{base_url}/r/{subreddit}.json",
        f"{base_url}/r/{subreddit}/hot.json",
    ]
    for url in json_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            ct = resp.headers.get("content-type", "")
            print(f"  JSON [{resp.status_code}] {url} (content-type: {ct})")
            if resp.status_code == 200 and "json" in ct:
                data = resp.json()
                print(f"    ✅ JSON 응답! keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                return True
        except Exception as e:
            print(f"  JSON [FAIL] {url}: {e}")
    return False


# ── 메인 ──

def main():
    print("=" * 60)
    print("Redlib 인스턴스 HTML 파싱 테스트")
    print(f"테스트 서브레딧: r/{TEST_SUBREDDIT}")
    print("=" * 60)

    results = []

    for inst in REDLIB_INSTANCES:
        print(f"\n{'#'*60}")
        print(f"# 인스턴스: {inst}")
        print(f"{'#'*60}")

        # JSON 먼저 시도
        json_ok = test_json_endpoint(inst)
        if json_ok:
            results.append({"url": inst, "method": "json", "success": True})
            time.sleep(1)
            continue

        # HTML 파싱 시도
        result = test_html_parsing(inst)
        if result:
            results.append({**result, "method": "html", "success": True})
        else:
            results.append({"url": inst, "method": None, "success": False})

        time.sleep(2)  # 인스턴스간 간격

    # ── 결과 요약 ──
    print("\n" + "=" * 60)
    print("결과 요약")
    print("=" * 60)

    working = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    if working:
        print(f"\n✅ 접근 가능: {len(working)}개")
        for w in working:
            method = w.get("method", "?")
            posts = w.get("posts_found", "?")
            has_score = w.get("has_score", "?")
            has_comments = w.get("has_comments", "?")
            print(f"  {w['url']}")
            print(f"    방법: {method} | 포스트: {posts}개 | score: {has_score} | comments: {has_comments}")
    else:
        print("\n❌ 모든 인스턴스 접근 실패")
        print("→ .rss fallback 유지 + 소스 균등배분으로 커버")

    if failed:
        print(f"\n❌ 실패: {len(failed)}개")
        for f in failed:
            print(f"  {f['url']}")

    # JSON으로도 저장 (디버깅용)
    with open("redlib_test_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n상세 결과: redlib_test_results.json")

    return 0 if working else 1


if __name__ == "__main__":
    sys.exit(main())
