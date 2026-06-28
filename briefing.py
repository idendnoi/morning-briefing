#!/usr/bin/env python3
"""매일 아침 8시 브리핑 — GitHub Actions에서 실행"""

import asyncio
import os
import re
from datetime import datetime, timedelta

import feedparser
import pytz
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pykrx import stock as krx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

KST = pytz.timezone("Asia/Seoul")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = "U0BDNU84FQE"  # idenchoi77@gmail.com


# ──────────────────────────────────────────────
# 1. 리노공업 주가  (pykrx → KRX 직접 조회)
# ──────────────────────────────────────────────
def get_stock_section() -> str:
    today = datetime.now(KST)
    start = (today - timedelta(days=10)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")

    try:
        df = krx.get_market_ohlcv_by_date(start, end, "058470")
        if df.empty:
            raise ValueError("데이터 없음")

        latest_date = df.index[-1].strftime("%m/%d")
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None

        close = int(row["종가"])
        high  = int(row["고가"])
        low   = int(row["저가"])
        vol   = int(row["거래량"])

        if prev is not None:
            diff = close - int(prev["종가"])
            pct  = diff / int(prev["종가"]) * 100
            sign = "▲" if diff >= 0 else "▼"
            change_str = f"{sign} {abs(diff):,}원 ({pct:+.2f}%)"
        else:
            change_str = "전일 데이터 없음"

        lines = [
            f"*종가 {close:,}원* ({latest_date} 기준)  |  전일比 {change_str}",
            f"고가 {high:,}원 / 저가 {low:,}원  |  거래량 {vol:,}주",
            f"<https://finance.naver.com/item/main.naver?code=058470|네이버 금융 바로가기>",
        ]
        return "\n".join(lines)

    except Exception as e:
        return (
            f"주가 조회 실패 ({e})\n"
            "<https://finance.naver.com/item/main.naver?code=058470|네이버 금융에서 직접 확인>"
        )


# ──────────────────────────────────────────────
# 2. AI 기술 동향  (Google News RSS)
# ──────────────────────────────────────────────
def get_ai_news_section() -> str:
    rss_queries = [
        "AI+model+release+2026",
        "artificial+intelligence+research+breakthrough",
        "인공지능+AI+기술+동향",
    ]
    cutoff = (datetime.now(KST) - timedelta(days=2)).date()

    items: list[dict] = []
    seen: set[str] = set()
    skip_kw = {"주식", "코인", "암호화폐", "부동산", "증시", "광고", "채용", "공채"}

    for q in rss_queries:
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        for entry in feed.entries[:15]:
            title = entry.get("title", "").strip()
            link  = entry.get("link", "")
            pub   = entry.get("published_parsed")
            if not pub or not title:
                continue
            pub_date = datetime(*pub[:6], tzinfo=pytz.utc).astimezone(KST).date()
            if pub_date < cutoff:
                continue
            if any(k in title for k in skip_kw):
                continue
            key = title[:20]
            if key in seen:
                continue
            seen.add(key)
            items.append({"title": title, "link": link, "date": pub_date.strftime("%m/%d")})
            if len(items) >= 5:
                break
        if len(items) >= 5:
            break

    if not items:
        return "최근 1~2일 내 주요 AI 뉴스를 찾지 못했습니다."

    return "\n".join(
        f"{i}. <{it['link']}|{it['title']}> ({it['date']})"
        for i, it in enumerate(items, 1)
    )


# ──────────────────────────────────────────────
# 3. 링커리어 공모전/대외활동  (Playwright)
# ──────────────────────────────────────────────
async def get_linkareer(page) -> list[dict]:
    items: list[dict] = []

    for list_url in [
        "https://linkareer.com/list/contest",
        "https://linkareer.com/list/activity",
    ]:
        if len(items) >= 5:
            break
        try:
            await page.goto(list_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(2)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # 링커리어 카드: <a> 태그 안에 제목·마감 정보가 있음
            for a in soup.select("a[href*='/activity/']"):
                title_el    = a.select_one("[class*='title'], h2, h3, strong")
                deadline_el = a.select_one("[class*='deadline'], [class*='date'], time, [class*='dday']")
                title    = title_el.get_text(strip=True)    if title_el    else a.get_text(strip=True)
                deadline = deadline_el.get_text(strip=True) if deadline_el else "마감일 확인"
                href     = a.get("href", "")
                full_url = f"https://linkareer.com{href}" if href.startswith("/") else href

                if not title or len(title) < 4:
                    continue
                # 중복 제거
                if any(it["title"] == title for it in items):
                    continue
                items.append({"title": title, "deadline": deadline, "url": full_url})
                if len(items) >= 5:
                    break
        except Exception:
            pass

    return items


# ──────────────────────────────────────────────
# 4. 숭실대 공지사항  (Playwright + BeautifulSoup)
# ──────────────────────────────────────────────
SSU_CATEGORIES = {
    "학사":     "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%ED%95%99%EC%82%AC&keyword",
    "장학":     "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EC%9E%A5%ED%95%99&keyword",
    "국제교류": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EA%B5%AD%EC%A0%9C%EA%B5%90%EB%A5%98&keyword",
    "비교과·행사": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EB%B9%84%EA%B5%90%EA%B3%BC%C2%B7%ED%96%89%EC%82%AC&keyword",
    "봉사":     "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EB%B4%89%EC%82%AC&keyword",
}

def parse_date(text: str):
    """YYYY.MM.DD / YYYY-MM-DD 형식 날짜 파싱"""
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).date()
        except ValueError:
            pass
    return None

async def get_ssu_notices(page) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    cutoff = (datetime.now(KST) - timedelta(days=2)).date()

    for cat, url in SSU_CATEGORIES.items():
        notices: list[dict] = []
        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(1)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            # SSU:catch는 WordPress 기반 — 게시물 링크와 날짜 추출
            # <a href> 로 시작하는 공지 제목 탐색
            for a in soup.select("a[href*='scatch.ssu.ac.kr'], a[href^='/']"):
                title = a.get_text(strip=True)
                href  = a.get("href", "")
                if not title or len(title) < 5:
                    continue
                # 부모 또는 인접 요소에서 날짜 찾기
                parent  = a.parent
                date_text = ""
                for _ in range(4):   # 최대 4단계 위 조상까지 탐색
                    if parent is None:
                        break
                    date_text = parent.get_text(" ", strip=True)
                    if re.search(r"\d{4}[.\-]\d{2}[.\-]\d{2}", date_text):
                        break
                    parent = parent.parent

                post_date = parse_date(date_text)
                if post_date and post_date >= cutoff:
                    full_url = href if href.startswith("http") else f"https://scatch.ssu.ac.kr{href}"
                    # 중복 제거
                    if any(n["title"] == title for n in notices):
                        continue
                    notices.append({
                        "title": title,
                        "date":  post_date.strftime("%m/%d"),
                        "url":   full_url,
                    })
        except Exception:
            pass

        result[cat] = notices

    return result


# ──────────────────────────────────────────────
# 메시지 조합
# ──────────────────────────────────────────────
def build_message(stock: str, ai: str, linkareer: list, ssu: dict) -> str:
    now = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    header = f"📅 *{now.strftime('%Y년 %m월 %d일')} ({weekdays[now.weekday()]}) 아침 브리핑*"

    # ── 리노공업
    s1 = f"📊 *리노공업 (058470)*\n{stock}"

    # ── AI 동향
    s2 = f"🤖 *AI 기술 동향*\n{ai}"

    # ── 링커리어
    if linkareer:
        lk_lines = [f"{i}. *{it['title']}*  |  마감: {it['deadline']}  |  <{it['url']}|링커리어 상세>"
                    for i, it in enumerate(linkareer[:5], 1)]
    else:
        lk_lines = ["정보를 가져오지 못했습니다. <https://linkareer.com/list/contest|링커리어>에서 직접 확인하세요."]
    s3 = "🎯 *공모전·대외활동 (링커리어)*\n" + "\n".join(lk_lines)

    # ── 숭실대 공지
    ssu_lines: list[str] = []
    for cat, notices in ssu.items():
        if notices:
            ssu_lines.append(f"*[{cat}]*")
            for n in notices:
                ssu_lines.append(f"• {n['date']} — <{n['url']}|{n['title']}>")
        else:
            ssu_lines.append(f"*[{cat}]* 신규 공지 없음")
    s4 = "🏫 *숭실대 공지 (SSU:catch)*\n" + "\n".join(ssu_lines)

    sep = "\n\n" + "─" * 30 + "\n\n"
    return sep.join([header, s1, s2, s3, s4])


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
async def main():
    # 1. 주가 (동기)
    stock_text = get_stock_section()

    # 2. AI 뉴스 (동기)
    ai_text = get_ai_news_section()

    # 3 & 4. Playwright (비동기)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            locale="ko-KR",
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        linkareer_items = await get_linkareer(page)
        ssu_notices     = await get_ssu_notices(page)

        await ctx.close()
        await browser.close()

    # 5. 메시지 조합
    message = build_message(stock_text, ai_text, linkareer_items, ssu_notices)
    print("=== 전송할 메시지 ===")
    print(message)

    # 6. Slack DM 전송
    client = WebClient(token=SLACK_BOT_TOKEN)
    try:
        resp = client.chat_postMessage(
            channel=SLACK_USER_ID,
            text=message,
            mrkdwn=True,
        )
        print(f"\n✅ Slack 전송 성공 — ts: {resp['ts']}")
    except SlackApiError as e:
        print(f"\n❌ Slack 전송 실패: {e.response['error']}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
