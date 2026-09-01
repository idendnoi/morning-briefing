#!/usr/bin/env python3
"""매일 아침 8시 브리핑 — GitHub Actions에서 실행"""

import asyncio
import math
import os
import re
from datetime import datetime, timedelta

import FinanceDataReader as fdr
import feedparser
import pytz
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

KST = pytz.timezone("Asia/Seoul")
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = "U0BDNU84FQE"  # idenchoi77@gmail.com
KMA_API_KEY = os.environ.get("KMA_API_KEY", "")

UA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

# 날씨 위치 (위도, 경도)
WEATHER_LOCATIONS = [
    ("시흥 대야동", 37.434, 126.793),
    ("숭실대 (동작구)", 37.4967, 126.9572),
]

# 보유 종목 (이름, 종목코드, 매수 평균단가)
STOCKS = [
    {"name": "리노공업", "code": "058470", "avg_price": 99_350},
    {"name": "현대오토에버", "code": "307950", "avg_price": 542_000},
    {"name": "TIGER 미국 S&P500", "code": "360750", "avg_price": 26_550},
]

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
# 0. 날씨 브리핑
#    강수: 기상청 단기예보 API (한국 날씨 최정확)
#    자외선: Open-Meteo ECMWF (기상청 단기예보에 UV 미포함)
# ──────────────────────────────────────────────
_UV_LABELS = [(2, "낮음"), (5, "보통"), (7, "높음"), (10, "매우 높음"), (99, "위험")]


def _latlon_to_kma_grid(lat: float, lon: float) -> tuple[int, int]:
    """위경도 → 기상청 Lambert 격자 좌표 (nx, ny) 변환"""
    DEGRAD = math.pi / 180.0
    re = 6371.00877 / 5.0
    slat1, slat2 = 30.0 * DEGRAD, 60.0 * DEGRAD
    olon, olat = 126.0 * DEGRAD, 38.0 * DEGRAD
    XO, YO = 43, 136

    sn = math.log(math.cos(slat1) / math.cos(slat2)) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5) / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = (math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn) * math.cos(slat1) / sn
    ro = re * sf / (math.tan(math.pi * 0.25 + olat * 0.5) ** sn)
    ra = re * sf / (math.tan(math.pi * 0.25 + lat * DEGRAD * 0.5) ** sn)
    theta = (lon * DEGRAD - olon) * sn
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    return int(ra * math.sin(theta) + XO + 0.5), int(ro - ra * math.cos(theta) + YO + 0.5)


def _kma_rain_hours(nx: int, ny: int) -> list[tuple[int, int]]:
    """기상청 단기예보 → 오늘 강수확률 30% 이상 시간대 [(hour, pop%)]"""
    now = datetime.now(KST)
    params = {
        "serviceKey": KMA_API_KEY,
        "numOfRows": "1000",
        "pageNo": "1",
        "dataType": "JSON",
        "base_date": now.strftime("%Y%m%d"),
        "base_time": "0500",
        "nx": nx,
        "ny": ny,
    }
    resp = requests.get(
        "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst",
        params=params,
        timeout=10,
    )
    print(f"  [KMA] HTTP {resp.status_code}, nx={nx}, ny={ny}")
    try:
        data = resp.json()
    except Exception:
        raise ValueError(f"응답이 JSON이 아님: {resp.text[:200]}")
    header = data.get("response", {}).get("header", {})
    result_code = header.get("resultCode", "??")
    result_msg = header.get("resultMsg", "??")
    print(f"  [KMA] resultCode={result_code}, resultMsg={result_msg}")
    if result_code != "00":
        raise ValueError(f"기상청 API 오류 {result_code}: {result_msg}")
    items = data["response"]["body"]["items"]["item"]
    today = now.strftime("%Y%m%d")
    hourly: dict[int, int] = {}
    for it in items:
        if it["fcstDate"] == today and it["category"] == "POP":
            hourly[int(it["fcstTime"][:2])] = int(it["fcstValue"])
    return [(h, pop) for h, pop in sorted(hourly.items()) if pop >= 30]


def _openmeteo_rain_hours(lat: float, lon: float) -> list[tuple[int, int]]:
    """Open-Meteo → 오늘 강수확률 30% 이상 시간대 (KMA 실패 시 대체용)"""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=precipitation_probability"
        "&timezone=Asia%2FSeoul&forecast_days=1"
    )
    data = requests.get(url, timeout=10).json()["hourly"]
    result = []
    for t, pop in zip(data["time"], data["precipitation_probability"]):
        if pop is not None and pop >= 30:
            result.append((int(t[11:13]), int(pop)))
    return result


def _uv_peak(lat: float, lon: float) -> tuple[float, int]:
    """Open-Meteo ECMWF → (오늘 최고 자외선지수, 최고 시각)"""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&hourly=uv_index&models=ecmwf_ifs025"
        "&timezone=Asia%2FSeoul&forecast_days=1"
    )
    data = requests.get(url, timeout=10).json()["hourly"]
    peak, peak_h = 0.0, 12
    for i, t in enumerate(data["time"]):
        v = data["uv_index"][i] or 0.0
        if v > peak:
            peak, peak_h = v, int(t[11:13])
    return peak, peak_h


def _fmt_rain(rain_hours: list[tuple[int, int]]) -> str:
    if not rain_hours:
        return "☀️ 비 없음"
    periods: list[str] = []
    s, e, mp = rain_hours[0][0], rain_hours[0][0], rain_hours[0][1]
    for h, p in rain_hours[1:]:
        if h == e + 1:
            e, mp = h, max(mp, p)
        else:
            periods.append(f"{s:02d}시~{e + 1:02d}시 ({mp}%)")
            s, e, mp = h, h, p
    periods.append(f"{s:02d}시~{e + 1:02d}시 ({mp}%)")
    return "🌧 " + ", ".join(periods)


def _fmt_uv(uv: float, h: int) -> str:
    label = next(lb for thr, lb in _UV_LABELS if uv <= thr)
    return f"자외선 {uv:.0f} ({label}, {h:02d}시 최고)"


def get_weather_section() -> str:
    if not KMA_API_KEY:
        return "날씨 조회 불가 — GitHub Secret에 KMA_API_KEY 미설정"
    lines: list[str] = []
    for name, lat, lon in WEATHER_LOCATIONS:
        nx, ny = _latlon_to_kma_grid(lat, lon)
        try:
            try:
                rain_hours = _kma_rain_hours(nx, ny)
                rain_src = "기상청"
            except ValueError:
                rain_hours = _openmeteo_rain_hours(lat, lon)
                rain_src = "Open-Meteo"
            rain = _fmt_rain(rain_hours)
            uv_val, uv_h = _uv_peak(lat, lon)
            lines.append(f"*{name}*  {rain}  |  {_fmt_uv(uv_val, uv_h)}  _(강수: {rain_src})_")
        except Exception as e:
            lines.append(f"*{name}*  날씨 조회 실패 ({e})")
    return "\n".join(lines)


# ──────────────────────────────────────────────
# 0-B. 오늘의 학식 (숭실대 학생식당 중식 3종)
# ──────────────────────────────────────────────
LUNCH_MENU_LABELS = ["중식1", "중식2", "중식3"]


def _clean_side_items(items: list[str]) -> list[str]:
    """반찬 후보 텍스트 중 실제 반찬명만 채택 (영문 번역/알러지 표시 등 제외)"""
    return [
        t for t in items
        if t and len(t) <= 40 and re.search(r"[가-힣]", t) and not t.startswith("*")
    ]


def _parse_lunch_item(list_td) -> dict | None:
    """td.menu_list 하나 → {"category":..., "main":..., "sides":[...]} (파싱 실패 시 None)

    이 사이트는 담당자가 매일 수기로 서식을 입력해서 항목마다 태그 구조/스타일이
    조금씩 달라짐 (예: 메인 메뉴 글자색을 <b style=...>로 넣는 날도 있고
    <font color=...>로 감싸는 날도 있음, 반찬 목록이 <ul><li>일 때도 있고
    표(<table>)일 때도 있음). 그래서 태그/스타일이 아니라 텍스트 패턴(★, [..])
    기준으로 파싱한다.
    """
    text = list_td.get_text(" ", strip=True)

    name_match = re.search(r"★\s*(.+?)\s*-\s*([\d.]+)", text)
    if not name_match:
        return None
    main = f"{name_match.group(1)} ({name_match.group(2)})"

    cat_match = re.search(r"\[([^\]]+)\]", text)
    category = cat_match.group(1) if cat_match else ""

    # 반찬 목록: <ul class="mean_list"> 구조를 우선 시도하고, 없으면 표(<table>) 구조로 대체
    li_texts = [li.get_text(" ", strip=True) for li in list_td.select("ul.mean_list > li.mean_item")]
    sides = _clean_side_items(li_texts)
    if not sides:
        table = list_td.find("table")
        if table is not None:
            sides = _clean_side_items(table.get_text(separator="\n", strip=True).split("\n"))

    return {"category": category, "main": main, "sides": sides}


def get_lunch_menu_section() -> str:
    """숭실대 생협 학생식당(rcd=1) 오늘의 중식 3종 → Slack 텍스트"""
    today_str = datetime.now(KST).strftime("%Y%m%d")
    try:
        resp = requests.get(
            "https://soongguri.com/m/m_req/m_menu.php",
            params={"rcd": "1", "sdt": today_str},
            headers=UA_HEADERS,
            timeout=10,
        )
        print(f"  [학식] HTTP {resp.status_code}, sdt={today_str}, 응답 길이={len(resp.text)}")
        resp.raise_for_status()

        # 사이트가 명시적으로 "오늘은 쉽니다."라고 답할 때만 휴무로 처리.
        # (이전 버전은 중식1/2/3 태그를 못 찾으면 무조건 휴무로 간주해서,
        #  네트워크 오류나 페이지 구조 변경 때도 "휴무"로 잘못 표시되는 문제가 있었음)
        if "오늘은 쉽니다" in resp.text:
            return "오늘은 학식 운영을 하지 않습니다."

        soup = BeautifulSoup(resp.text, "html.parser")

        items: dict[str, dict | None] = {label: None for label in LUNCH_MENU_LABELS}
        found_menu_td = False
        for td in soup.select("td.menu_nm"):
            label = td.get_text(strip=True)
            if label in items:
                found_menu_td = True
                list_td = td.find_next_sibling("td", class_="menu_list")
                if list_td is not None:
                    items[label] = _parse_lunch_item(list_td)

        if not found_menu_td:
            raise ValueError(f"메뉴 항목을 찾지 못함 (응답 시작: {resp.text[:200]!r})")

        lines = []
        for label in LUNCH_MENU_LABELS:
            item = items[label]
            if item is None:
                lines.append(f"*{label}* 정보 없음")
                continue
            cat = f"[{item['category']}] " if item["category"] else ""
            sides = ", ".join(item["sides"]) if item["sides"] else "-"
            lines.append(f"*{label}* {cat}{item['main']}\n  └ {sides}")
        return "\n".join(lines)

    except Exception as e:
        return f"학식 정보 조회 실패 ({e})"


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
# 1-A. 보유 종목 주가  (pykrx → KRX 직접 조회)
# ──────────────────────────────────────────────
def _fmt_change(diff: int, base: int) -> str:
    pct = diff / base * 100
    sign = "▲" if diff >= 0 else "▼"
    return f"{sign} {abs(diff):,}원 ({pct:+.2f}%)"


def get_stock_price_line(code: str, avg_price: int) -> str:
    today = datetime.now(KST)
    start = (today - timedelta(days=10)).strftime("%Y-%m-%d")

    try:
        df = fdr.DataReader(code, start)
        if df.empty:
            raise ValueError("데이터 없음")

        latest_date = df.index[-1].strftime("%m/%d")
        row = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else None

        close = int(row["Close"])
        high = int(row["High"])
        low = int(row["Low"])
        vol = int(row["Volume"])

        if prev is not None:
            change_str = _fmt_change(close - int(prev["Close"]), int(prev["Close"]))
        else:
            change_str = "전일 데이터 없음"

        avg_diff_str = _fmt_change(close - avg_price, avg_price)

        return (
            f"*종가 {close:,}원* ({latest_date} 기준)  |  전일比 {change_str}\n"
            f"매수 평균단가 {avg_price:,}원  |  평단比 {avg_diff_str}\n"
            f"고가 {high:,}원 / 저가 {low:,}원  |  거래량 {vol:,}주\n"
            f"<https://finance.naver.com/item/main.naver?code={code}|네이버 금융 바로가기>"
        )

    except Exception as e:
        return (
            f"주가 조회 실패 ({e})\n"
            f"매수 평균단가 {avg_price:,}원\n"
            f"<https://finance.naver.com/item/main.naver?code={code}|네이버 금융에서 직접 확인>"
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


def get_stock_sections() -> dict[str, str]:
    """{종목명: 브리핑 텍스트} — 리노공업만 관련 뉴스 포함"""
    sections: dict[str, str] = {}
    for s in STOCKS:
        price = get_stock_price_line(s["code"], s["avg_price"])
        if s["name"] == "리노공업":
            news = get_stock_news_section()
            sections[s["name"]] = f"{price}\n\n{news}"
        else:
            sections[s["name"]] = price
    return sections


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
def build_message(
    weather: str, lunch: str, stocks: dict[str, str], ai: str, linkareer: list, ssu: dict
) -> str:
    now = datetime.now(KST)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    header = f"📅 *{now.strftime('%Y년 %m월 %d일')} ({weekdays[now.weekday()]}) 아침 브리핑*"

    s0 = f"🌤 *오늘 날씨*\n{weather}"
    s_lunch = f"🍱 *오늘의 학식 (숭실대 학생식당)*\n{lunch}"
    code_by_name = {s["name"]: s["code"] for s in STOCKS}
    stock_blocks = [
        f"📊 *{name} ({code_by_name[name]})*\n{text}" for name, text in stocks.items()
    ]
    s1 = "\n\n".join(stock_blocks)
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
    return sep.join([header, s0, s_lunch, s1, s2, s3, s4])


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────
async def main():
    weather_text = get_weather_section()
    lunch_text = get_lunch_menu_section()
    stock_sections = get_stock_sections()
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

    message = build_message(
        weather_text, lunch_text, stock_sections, ai_text, linkareer_items, ssu_notices
    )
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
