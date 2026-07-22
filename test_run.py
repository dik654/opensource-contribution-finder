"""
테스트용 - 크롤링 → 요약 → Discord 전송을 한번에 실행
확인 후 삭제해도 됨
"""

import asyncio
from crawl import collect_all, load_existing, merge_posts, save_data
from digest import classify_and_summarize, send_to_discord, load_and_rank

async def main():
    print("=" * 50)
    print("🧪 테스트 실행: 크롤링 → 요약 → Discord 전송")
    print("=" * 50)

    # 1. 크롤링
    print("\n[1/4] 크롤링 중...")
    new_posts = await collect_all()
    if not new_posts:
        print("[ERROR] 수집 실패")
        return

    # 2. 저장 (테스트에서도 동일 경로 사용)
    print("\n[2/4] 데이터 저장...")
    existing = load_existing()
    merged = merge_posts(existing, new_posts)
    save_data(merged)

    # 3. 요약
    print("\n[3/4] AI 분류/요약 중...")
    posts = load_and_rank()
    digest = await classify_and_summarize(posts)
    if not digest:
        print("[ERROR] 요약 실패")
        return

    print(f"  → {len(digest)}개 토픽:")
    for item in digest:
        print(f"  [{item['category']}] {item['headline']}")
        if item.get("best_comments"):
            for c in item["best_comments"]:
                print(f"    💬 {c}")

    # 4. Discord 전송
    print("\n[4/4] Discord 전송 중...")
    await send_to_discord(digest)

    print("\n✅ 테스트 완료! Discord를 확인하세요.")


if __name__ == "__main__":
    asyncio.run(main())
