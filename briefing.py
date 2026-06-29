#!/usr/bin/env python3
"""매일 아침 8시 브리핑 — GitHub Actions에서 실행"""

import asyncio
import os
import re
from datetime import datetime, timedelta

import feedparser
import pytz
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from pykrx import stock as krx
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

KST = pytz.timezone("Asia/Seoul")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = "U0BDNU84FQE"  # idenchoi77@gmail.com

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# TradingView 히트맵 URL (URL 인코딩된 형태)
HEATMAP_URLS = {
    "KOSPI": (
        "https://kr.tradingview.com/heatmap/stock/"
        "#%7B%22dataSource%22%3A%22KOSPI%22%2C%22blockColor%22%3A%22change%22%2C"
        "%22blockSize%22%3A%22market_cap_basic%22%2C%22grouping%22%3A%22sector%22%7D"
    ),
    "SPX500": (
        "https://kr.tradingview.com/heatmap/stock/"
        "#%7B%22dataSource%22%3A%22SPX500%22%2C%22blockColor%22%3A%22change%22%2C"
        "%22blockSize%22%3A%22market_cap_basic%22%2C%22grouping%22%3A%22sector%22%7D"
    ),
}


# ──────────────────────────────────────────────
# 공통: 기사 원문 페이지의 meta description을 가져와 한줄 요약으로 사용
# ──────────────────────────────────────────────
def fetch_meta_description(url: str, timeout: int = 8, max_len: int = 90) -> str:
    # Google News RSS 링크를 따라가면 구글 집계 페이지 설명이 나옴 → 무시
    SKIP_PHRASES = [
        "Comprehensive up-to-date news coverage",
        "aggregated from sources all over the world",
    ]
    try:
        resp = requests.get(url, headers=UA_HEADERS, timeout=timeout, allow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        meta = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"}
        )
        if meta and meta.get("content"):
            text = re.sub(r"\s+", " ", meta["content"]).strip()
            if any(p in text for p in SKIP_PHRASES):
                return ""
            if len(text) > max_len:
                text = text[:max_len].rsplit(" ", 1)[0] + "..."
            return text
    except Exception:
        pass
    return ""


def attach_summaries(items: list[dict]) -> list[dict]:
    """items: [{"title":..., "link":..., "date":...}, ...] 각 item에 'summary' 키 추가"""
    for it in items:
        summary = fetch_meta_description(it["link"])
        if summary:
            it["summary"] = summary
    return items


# ──────────────────────────────────────────────
# 1-A. 리노공업 주가  (pykrx → KRX 직접 조회)
# ──────────────────────────────────────────────
def get_stock_price_line() -> str:
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
        high = int(row["고가"])
        low = int(row["저가"])
        vol = int(row["거래량"])

        if prev is not None:
            diff = close - int(prev["종가"])
            pct = diff / int(prev["종가"]) * 100
            sign = "▲" if diff >= 0 else "▼"
            change_str = f"{sign} {abs(diff):,}원 ({pct:+.2f}%)"
        else:
            change_str = "전일 데이터 없음"

        return (
            f"*종가 {close:,}원* ({latest_date} 기준)  |  전일比 {change_str}\n"
            f"고가 {high:,}원 / 저가 {low:,}원  |  거래량 {vol:,}주\n"
            f"<https://finance.naver.com/item/main.naver?code=058470|네이버 금융 바로가기>"
        )

    except Exception as e:
        return (
            f"주가 조회 실패 ({e})\n"
            "<https://finance.naver.com/item/main.naver?code=058470|네이버 금융에서 직접 확인>"
        )


# ──────────────────────────────────────────────
# 1-B. 리노공업 관련 뉴스/이슈  (Google News RSS + 기사 meta description)
# ──────────────────────────────────────────────
def get_stock_news_section() -> str:
    rss_queries = ["리노공업+058470", "리노공업+주가", "리노공업+실적"]
    cutoff = (datetime.now(KST) - timedelta(days=5)).date()

    items: list[dict] = []
    seen: set[str] = set()

    for q in rss_queries:
        url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            pub = entry.get("published_parsed")
            if not pub or not title:
                continue
            pub_date = datetime(*pub[:6], tzinfo=pytz.utc).astimezone(KST).date()
            if pub_date < cutoff:
                continue
            key = title[:20]
            if key in seen:
                continue
            seen.add(key)
            items.append({"title": title, "link": link, "date": pub_date.strftime("%m/%d")})
            if len(items) >= 3:
                break
        if len(items) >= 3:
            break

    if not items:
        return "특이 뉴스 없음"

    items = attach_summaries(items)

    lines = []
    for it in items:
        summary = it.get("summary", "")
        if summary:
            lines.append(f"• <{it['link']}|{it['title']}> ({it['date']})\n  → {summary}")
        else:
            lines.append(f"• <{it['link']}|{it['title']}> ({it['date']})")
    return "\n".join(lines)


def get_stock_section() -> str:
    price = get_stock_price_line()
    news = get_stock_news_section()
    return f"{price}\n\n{news}"


# ──────────────────────────────────────────────
# 2. AI 기술 동향 — 오늘 기사 우선, 없으면 어제 기사 재사용
# ──────────────────────────────────────────────
def get_ai_news_section() -> str:
    rss_queries = [
        "AI+model+release+2026",
        "artificial+intelligence+research+breakthrough",
        "인공지능+AI+기술+동향",
    ]
    skip_kw = {"주식", "코인", "암호화폐", "부동산", "증시", "광고", "채용", "공채"}

    def fetch_for_date(target_date):
        """특정 날짜(KST 기준)에 발행된 AI 뉴스만 수집"""
        items: list[dict] = []
        seen: set[str] = set()
        for q in rss_queries:
            url = f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
            feed = feedparser.parse(url)
            for entry in feed.entries[:15]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "")
                pub = entry.get("published_parsed")
                if not pub or not title:
                    continue
                pub_date = datetime(*pub[:6], tzinfo=pytz.utc).astimezone(KST).date()
                if pub_date != target_date:
                    continue
                if any(k in title for k in skip_kw):
                    continue
                key = title[:20]
                if key in seen:
                    continue
                seen.add(key)
                items.append({"title": title, "link": link, "date": pub_date.strftime("%m/%d")})
                if len(items) >= 5:
                    return items
        return items

    today = datetime.now(KST).date()
    yesterday = today - timedelta(days=1)

    # 오늘 기사 먼저 시도
    items = fetch_for_date(today)
    using_today = bool(items)

    # 오늘 기사가 없으면 어제 기사 사용
    if not items:
        items = fetch_for_date(yesterday)

    if not items:
        return "최근 주요 AI 뉴스를 찾지 못했습니다."

    items = attach_summaries(items)

    lines = []
    for i, it in enumerate(items, 1):
        summary = it.get("summary", "")
        if summary:
            lines.append(f"{i}. <{it['link']}|{it['title']}> ({it['date']})\n   → {summary}")
        else:
            lines.append(f"{i}. <{it['link']}|{it['title']}> ({it['date']})")

    result = "\n".join(lines)
    if not using_today:
        result = "_(오늘 새 기사가 없어 전날 기사를 표시합니다)_\n" + result
    return result


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

            for a in soup.select("a[href*='/activity/']"):
                title_el = a.select_one("[class*='title'], h2, h3, strong")
                deadline_el = a.select_one("[class*='deadline'], [class*='date'], time, [class*='dday']")
                title = title_el.get_text(strip=True) if title_el else a.get_text(strip=True)
                title = re.sub(r"^추천\s*", "", title).strip()  # "추천" 뱃지 텍스트 제거
                deadline = deadline_el.get_text(strip=True) if deadline_el else "마감일 확인"
                href = a.get("href", "")
                full_url = f"https://linkareer.com{href}" if href.startswith("/") else href

                if not title or len(title) < 4:
                    continue
                if any(it["title"] == title for it in items):
                    continue
                items.append({"title": title, "deadline": deadline, "url": full_url})
                if len(items) >= 5:
                    break
        except Exception:
            pass

    return items


# ──────────────────────────────────────────────
# 4. 숭실대 공지사항 — 신규 없으면 최신 1개 함께 표시
# ──────────────────────────────────────────────
SSU_CATEGORIES = {
    "학사": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%ED%95%99%EC%82%AC&keyword",
    "장학": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EC%9E%A5%ED%95%99&keyword",
    "국제교류": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EA%B5%AD%EC%A0%9C%EA%B5%90%EB%A5%98&keyword",
    "비교과·행사": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EB%B9%84%EA%B5%90%EA%B3%BC%C2%B7%ED%96%89%EC%82%AC&keyword",
    "봉사": "https://scatch.ssu.ac.kr/%ea%b3%b5%ec%a7%80%ec%82%ac%ed%95%ad/?f&category=%EB%B4%89%EC%82%AC&keyword",
}


def parse_date(text: str):
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mo, d).date()
        except ValueError:
            pass
    return None


async def get_ssu_notices(page) -> dict[str, dict]:
    """각 카테고리별 {"new": [신규공지...], "latest": 최신공지1개} 반환"""
    result: dict[str, dict] = {}
    cutoff = (datetime.now(KST) - timedelta(days=2)).date()

    for cat, url in SSU_CATEGORIES.items():
        all_notices: list[dict] = []   # 날짜 무관하게 수집 (최신 1개 fallback용)
        new_notices: list[dict] = []   # cutoff 이후 신규 공지만

        try:
            await page.goto(url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(1)
            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")

            for a in soup.select("a[href*='scatch.ssu.ac.kr'], a[href^='/']"):
                title = a.get_text(strip=True)
                href = a.get("href", "")
                if not title or len(title) < 5:
                    continue
                parent = a.parent
                date_text = ""
                for _ in range(4):
                    if parent is None:
                        break
                    date_text = parent.get_text(" ", strip=True)
                    if re.search(r"\d{4}[.\-]\d{2}[.\-]\d{2}", date_text):
                        break
                    parent = parent.parent

                post_date = parse_date(date_text)
                if not post_date:
                    continue

                full_url = href if href.startswith("http") else f"https://scatch.ssu.ac.kr{href}"
                notice = {
                    "title": title,
                    "date": post_date.strftime("%m/%d"),
                    "url": full_url,
                    "_post_date": post_date,  # 정렬용 (출력 안 함)
                }

                if not any(n["title"] == title for n in all_notices):
                    all_notices.append(notice)

                if post_date >= cutoff and not any(n["title"] == title for n in new_notices):
                    new_notices.append(notice)

        except Exception:
            pass

        # 최신순 정렬 후 가장 최신 공지 1개 추출
        all_notices.sort(key=lambda x: x["_post_date"], reverse=True)
        latest = all_notices[0] if all_notices else None

        result[cat] = {"new": new_notices, "latest": latest}

    return result


# ──────────────────────────────────────────────
# 5. TradingView 히트맵 스크린샷 (신규 추가)
# ──────────────────────────────────────────────
async def take_heatmap_screenshot(ctx, url: str, filepath: str) -> bool:
    """TradingView 히트맵 페이지를 스크린샷으로 저장"""
    page = await ctx.new_page()
    try:
        await page.set_viewport_size({"width": 1600, "height": 820})
        print(f"히트맵 로딩 시작: {filepath}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # canvas(차트 영역)가 화면에 나타날 때까지 최대 20초 대기
        try:
            await page.wait_for_selector("canvas", state="visible", timeout=20_000)
            print("  → canvas 감지됨, 5초 추가 대기 (데이터 로딩)")
            await asyncio.sleep(5)
        except Exception:
            print("  → canvas 미감지, 15초 폴백 대기")
            await asyncio.sleep(15)

        # 팝업/쿠키 배너 닫기 시도
        for selector in [
            "button[class*='close']",
            "[class*='toast'] button",
            "[data-name='close-button']",
            "[class*='dialog'] button[class*='close']",
        ]:
            try:
                el = page.locator(selector).first
                if await el.is_visible(timeout=500):
                    await el.click()
                    await asyncio.sleep(0.3)
            except Exception:
                pass

        await page.screenshot(path=filepath, full_page=False)
        size_kb = os.path.getsize(filepath) // 1024
        print(f"  → 스크린샷 저장 완료 ({size_kb} KB): {filepath}")
        return True
    except Exception as e:
        print(f"  → 스크린샷 실패: {e}")
        return False
    finally:
        await page.close()


def upload_heatmap(client: WebClient, filepath: str, title: str, dm_channel_id: str) -> bool:
    """스크린샷 이미지를 Slack DM으로 전송 (dm_channel_id: D로 시작하는 채널 ID)"""
    if not os.path.exists(filepath):
        print(f"업로드 건너뜀 — 파일 없음: {filepath}")
        return False
    try:
        size_kb = os.path.getsize(filepath) // 1024
        print(f"업로드 시도: {title} ({size_kb} KB) → {dm_channel_id}")
        client.files_upload_v2(
            channel=dm_channel_id,
            file=filepath,
            filename=os.path.basename(filepath),
            title=title,
        )
        return True
    except SlackApiError as e:
        print(f"이미지 업로드 실패 ({title}): {e.response['error']}")
        return False
    except Exception as e:
        print(f"이미지 업로드 오류 ({title}): {e}")
        return False


# ──────────────────────────────────────────────
# 메시지 조합
# ──────────────────────────────────────────────
def build_message(stock: str, ai: str, linkareer: list, ssu: dict) -> str:
    now = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    header = f"📅 *{now.strftime('%Y년 %m월 %d일')} ({weekdays[now.weekday()]}) 아침 브리핑*"

    s1 = f"📊 *리노공업 (058470)*\n{stock}"
    s2 = f"🤖 *AI 기술 동향*\n{ai}"

    if linkareer:
        lk_lines = [
            f"{i}. *{it['title']}*  |  마감: {it['deadline']}  |  <{it['url']}|링커리어 상세>"
            for i, it in enumerate(linkareer[:5], 1)
        ]
    else:
        lk_lines = ["정보를 가져오지 못했습니다. <https://linkareer.com/list/contest|링커리어>에서 직접 확인하세요."]
    s3 = "🎯 *공모전·대외활동 (링커리어)*\n" + "\n".join(lk_lines)

    ssu_lines: list[str] = []
    for cat, data in ssu.items():
        new_notices = data.get("new", [])
        latest = data.get("latest")
        if new_notices:
            ssu_lines.append(f"*[{cat}]*")
            for n in new_notices:
                ssu_lines.append(f"• {n['date']} — <{n['url']}|{n['title']}>")
        else:
            ssu_lines.append(f"*[{cat}]* 신규 공지 없음")
            if latest:
                ssu_lines.append(f"  └ 최신: {latest['date']} — <{latest['url']}|{latest['title']}>")
    s4 = "🏫 *숭실대 공지 (SSU:catch)*\n" + "\n".join(ssu_lines)

    sep = "\n\n" + "─" * 30 + "\n\n"
    return sep.join([header, s1, s2, s3, s4])


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
async def main():
    stock_text = get_stock_section()
    ai_text = get_ai_news_section()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # 봇 감지 우회
            ],
        )
        ctx = await browser.new_context(
            locale="ko-KR",
            viewport={"width": 1600, "height": 900},
            device_scale_factor=2,  # 2배 해상도 렌더링 → 스크린샷 화질 개선
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        # 자동화 탐지 속성 숨기기
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await ctx.new_page()

        linkareer_items = await get_linkareer(page)
        ssu_notices = await get_ssu_notices(page)

        # 히트맵 스크린샷 (ctx 닫기 전에 실행)
        heatmap_paths: dict[str, str] = {}
        for name, url in HEATMAP_URLS.items():
            filepath = f"/tmp/{name.lower()}_heatmap.png"
            success = await take_heatmap_screenshot(ctx, url, filepath)
            if success:
                heatmap_paths[name] = filepath

        await ctx.close()
        await browser.close()

    message = build_message(stock_text, ai_text, linkareer_items, ssu_notices)
    print("=== 전송할 메시지 ===")
    print(message)

    client = WebClient(token=SLACK_BOT_TOKEN)

    # 텍스트 브리핑 먼저 전송 — 응답의 channel 값이 실제 DM 채널 ID (D...)
    try:
        resp = client.chat_postMessage(
            channel=SLACK_USER_ID,
            text=message,
            mrkdwn=True,
        )
        dm_channel_id = resp["channel"]  # 이후 파일 업로드에 사용
        print(f"\n✅ Slack 전송 성공 — ts: {resp['ts']}, 채널: {dm_channel_id}")
    except SlackApiError as e:
        print(f"\n❌ Slack 전송 실패: {e.response['error']}")
        raise

    # 히트맵 이미지 업로드 (실패해도 전체 실행 중단 안 함)
    heatmap_titles = {
        "KOSPI": "코스피 시장 히트맵",
        "SPX500": "S&P 500 히트맵",
    }
    for name, filepath in heatmap_paths.items():
        title = heatmap_titles.get(name, name)
        ok = upload_heatmap(client, filepath, title, dm_channel_id)
        print(f"{'✅' if ok else '❌'} {title} 업로드 {'성공' if ok else '실패'}")


if __name__ == "__main__":
    asyncio.run(main())
