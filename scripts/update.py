#!/usr/bin/env python3
"""
選挙カレンダー自動更新スクリプト

【方針】
  レートリミット対策のため Claude 呼び出しを最小限にする。

  A. BeautifulSoup による直接パース（Claude 不使用）
     - Wikipedia「YYYY年日本の補欠選挙」のテーブルを直接解析（wikitable がある年のみ有効）

  B. Claude API web_search（2回のみ）
     - 定期選挙（知事選・統一地方選・参院選など）＋補欠選挙・急選
     - 国会日程
"""

import os
import json
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent.parent / "data"
ELECTIONS_FILE = DATA_DIR / "elections.json"
DIET_FILE = DATA_DIR / "diet.json"

FETCH_HEADERS = {
    "User-Agent": (
        "election-calendar-bot/1.0 "
        "(public election data aggregator; "
        "contact: yusukesakaipolitics@gmail.com)"
    ),
    "Accept-Language": "ja,en;q=0.9",
}
FETCH_TIMEOUT = 20

ELECTIONS_SCHEMA = """
{
  "id": "ユニークID（例: shiga-pref-assembly-omihachiman-2026）",
  "name": "選挙名（例: 滋賀県議会議員補欠選挙（近江八幡市・竜王町選挙区））",
  "type": "選挙種別（例: 知事選, 市長選, 市議選, 県議選, 補選, 参院選, 衆院選）",
  "level": "national / pref / city / town",
  "region": "地域名",
  "prefecture": "都道府県名またはnull",
  "announcementDate": "YYYY-MM-DD またはnull",
  "announcementDateLabel": "公示日の表示文字列",
  "electionDay": "YYYY-MM-DD またはnull",
  "electionDayEarliest": "YYYY-MM-DD またはnull",
  "electionDayLatest": "YYYY-MM-DD またはnull",
  "electionDayLabel": "投票日の表示文字列",
  "certainty": "confirmed / estimated / unknown",
  "status": "scheduled / confirmed / completed / cancelled",
  "isUnexpected": true または false（補欠選挙・急選は true、定期選挙は false）,
  "source": "情報源URL",
  "note": "補足"
}
"""

DIET_SCHEMA = """
{
  "id": "ユニークID（例: 221st-special）",
  "name": "国会名（例: 第221回国会（特別国会））",
  "type": "常会 / 臨時会 / 特別会",
  "openDate": "YYYY-MM-DD",
  "closeDate": "YYYY-MM-DD またはnull",
  "closeDateUncertain": true/false,
  "milestones": [{ "date": "YYYY-MM-DD", "label": "イベント名" }]
}
"""


# ===== ユーティリティ =====

def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 保存: {path}")


def extract_json_from_text(text: str):
    """テキストから JSON 配列または JSON オブジェクトを抽出する"""
    # コードブロック内
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    # 生 JSON
    m = re.search(r"(\[[\s\S]+\]|\{[\s\S]+\})", text)
    if m:
        return json.loads(m.group(1).strip())
    raise ValueError("JSON が見つかりませんでした")


def fetch_url(url: str) -> str:
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception as e:
        print(f"  ⚠️ fetch 失敗 ({url}): {e}")
        return ""


def merge_elections(existing: dict, new_elections: list,
                    force_unexpected: bool = False) -> dict:
    existing_map = {e["id"]: e for e in existing.get("elections", [])}
    added, updated = 0, 0
    for e in new_elections:
        if "id" not in e or "name" not in e:
            continue
        if force_unexpected:
            e["isUnexpected"] = True
        if e["id"] in existing_map:
            updated += 1
        else:
            added += 1
        existing_map[e["id"]] = e
    print(f"   → 新規: {added}件、更新: {updated}件")
    result = dict(existing)
    result["elections"] = list(existing_map.values())
    result["lastUpdated"] = datetime.now(JST).isoformat()
    return result


# ===== Claude API 呼び出し（リトライ付き）=====

def call_claude(client: anthropic.Anthropic, prompt: str,
                use_search: bool = False, max_uses: int = 5) -> str:
    """
    Claude を呼び出してテキストを返す。
    web_search_20250305 は Anthropic がサーバー側で実行するため、
    クライアントは tool_result を自分で返す必要はない。
    レスポンスの content には複数の TextBlock が含まれる場合があるので、
    すべてを結合して返す。
    """
    tools = []
    if use_search:
        tools = [{"type": "web_search_20250305", "name": "web_search",
                  "max_uses": max_uses}]

    kwargs = dict(model="claude-haiku-4-5-20251001",
                  max_tokens=4096,
                  messages=[{"role": "user", "content": prompt}])
    if tools:
        kwargs["tools"] = tools

    for attempt in range(5):
        try:
            response = client.messages.create(**kwargs)
            break
        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            print(f"  ⏳ レートリミット。{wait}秒待機 ({attempt+1}/5)…")
            time.sleep(wait)
    else:
        print("  ❌ レートリミットで断念")
        return ""

    # stop_reason のデバッグ出力
    print(f"  stop_reason: {response.stop_reason}, blocks: {len(response.content)}")

    # すべての TextBlock を結合して返す
    # (web_search は preamble + ToolUseBlock + ToolResultBlock + 最終TextBlock という構造になる)
    texts = [block.text for block in response.content if hasattr(block, "text")]
    return "\n".join(texts)


# ===== A. Wikipedia 直接パース（Claude 不使用）=====

def make_id(text: str) -> str:
    """テキストから URL safe な ID を生成"""
    text = re.sub(r"[^\w\u3040-\u9fff]", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:60]


def detect_level(name: str, region: str) -> str:
    text = name + region
    if re.search(r"衆議院|参議院|国政", text):
        return "national"
    if re.search(r"知事|道議|府議|県議|都議", text):
        return "pref"
    if re.search(r"市長|区長|市議|区議", text):
        return "city"
    if re.search(r"町長|村長|町議|村議", text):
        return "town"
    return "city"


def parse_date_jp(text: str, year: int) -> str | None:
    """「4月19日」→「2026-04-19」 に変換"""
    m = re.search(r"(\d{1,2})月(\d{1,2})日", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return f"{year}-{month:02d}-{day:02d}"
    return None


def parse_wikipedia_bosen(html: str, year: int) -> list:
    """Wikipedia の補欠選挙一覧テーブルを直接パースして選挙リストを返す"""
    soup = BeautifulSoup(html, "html.parser")
    elections = []

    for table in soup.find_all("table", class_="wikitable"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        print(f"  テーブルヘッダー: {headers[:8]}")

        col_date   = next((i for i, h in enumerate(headers)
                           if re.search(r"投票日|選挙日|日付", h)), 0)
        col_region = next((i for i, h in enumerate(headers)
                           if re.search(r"選挙区|地域|都道府県|区域", h)), 1)
        col_type   = next((i for i, h in enumerate(headers)
                           if re.search(r"選挙の種類|種別|種類", h)), 2)

        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(col_date, col_region, col_type):
                continue

            date_text   = cells[col_date].get_text(strip=True)   if len(cells) > col_date   else ""
            region_text = cells[col_region].get_text(strip=True) if len(cells) > col_region else ""
            type_text   = cells[col_type].get_text(strip=True)   if len(cells) > col_type   else ""

            if not date_text or not region_text:
                continue

            election_day = parse_date_jp(date_text, year)
            name = f"{region_text}{type_text}補欠選挙" if type_text else f"{region_text}補欠選挙"
            eid  = f"bosen-{make_id(region_text)}-{year}"
            level = detect_level(type_text, region_text)

            elections.append({
                "id": eid,
                "name": name,
                "type": type_text or "補選",
                "level": level,
                "region": region_text,
                "prefecture": None,
                "announcementDate": None,
                "announcementDateLabel": "未定",
                "electionDay": election_day,
                "electionDayEarliest": None,
                "electionDayLatest": None,
                "electionDayLabel": date_text,
                "certainty": "confirmed" if election_day else "estimated",
                "status": "scheduled",
                "isUnexpected": True,
                "source": f"Wikipedia「{year}年日本の補欠選挙」",
                "note": "",
            })

    return elections


def update_from_wikipedia(existing: dict) -> dict:
    """Wikipedia の補欠選挙ページを直接パース（Claude 不使用）"""
    year = datetime.now(JST).year
    results = []

    for y in [year, year + 1]:
        title = f"{y}年日本の補欠選挙"
        url = f"https://ja.wikipedia.org/wiki/{urllib.parse.quote(title)}"
        print(f"  📖 Wikipedia fetch: {title}")
        html = fetch_url(url)
        if not html:
            continue
        parsed = parse_wikipedia_bosen(html, y)
        print(f"  → {len(parsed)} 件パース（wikitableなしの場合は0件）")
        results.extend(parsed)
        time.sleep(2)

    return merge_elections(existing, results, force_unexpected=True)


# ===== B. Claude web_search =====

def update_all_elections(client: anthropic.Anthropic, existing: dict) -> dict:
    """定期選挙＋補欠選挙・急選を web_search で一括取得"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    existing_ids = [e["id"] for e in existing.get("elections", [])]

    prompt = f"""今日は{today_str}です。日本の今後の選挙日程を web_search で調べてください。

【検索してほしいこと】
1. 「知事選 {datetime.now(JST).year} {datetime.now(JST).year + 1} 日程」
2. 「統一地方選 {datetime.now(JST).year + 1}」
3. 「参院選 日程」「衆院選 日程」
4. 「補欠選挙 {datetime.now(JST).year} 日程」「急選 {datetime.now(JST).year}」
5. 「市議会 補欠選挙 {datetime.now(JST).strftime('%Y年%m月')}」

【重要】
- 直近〜2年以内に実施予定のすべての選挙を網羅してください
- 補欠選挙・急選は特に漏れなく拾ってください（市議・町議の小規模なものも含む）
- 既存ID（重複不要）: {json.dumps(existing_ids, ensure_ascii=False)}

以下スキーマの **JSONコードブロック（```json ... ```）のみ** 出力してください。説明文は不要です。

{ELECTIONS_SCHEMA}
"""
    print("  🔍 定期選挙＋補欠選挙を検索中…")
    result = call_claude(client, prompt, use_search=True, max_uses=8)
    if not result:
        print("  ⚠️ Claude が空文字を返しました")
        return existing
    print(f"  Claude 応答（先頭200字）: {result[:200]}")
    try:
        elections = extract_json_from_text(result)
        if isinstance(elections, dict):
            elections = [elections]
        if not isinstance(elections, list):
            raise ValueError(f"listではありません: {type(elections)}")
        return merge_elections(existing, elections)
    except Exception as e:
        print(f"  ⚠️ パースエラー: {e}")
        print(f"  Claude 全応答: {result[:1000]}")
        return existing


def update_diet(client: anthropic.Anthropic, existing: dict) -> dict:
    """国会日程を web_search で取得"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    year = datetime.now(JST).year

    prompt = f"""今日は{today_str}です。現在および直近の国会会期を web_search で調べてください。

【検索してほしいこと】
1. 「第{year}回国会 会期 開会日 閉会日」
2. 「国会 会期 {year} 延長」

以下スキーマの **JSONコードブロック（```json ... ```）のみ** 出力してください。説明文は不要です。
現在開会中の国会のみで構いません。

{DIET_SCHEMA}

出力例（必ずこの形式で）:
```json
[
  {{
    "id": "221st-special",
    "name": "第221回国会（特別国会）",
    "type": "特別会",
    "openDate": "2026-02-18",
    "closeDate": "2026-07-17",
    "closeDateUncertain": false,
    "milestones": []
  }}
]
```
"""
    print("  🔍 国会日程を検索中…")
    result = call_claude(client, prompt, use_search=True, max_uses=3)
    if not result:
        print("  ⚠️ Claude が空文字を返しました")
        return existing
    print(f"  Claude 応答（先頭300字）: {result[:300]}")
    try:
        sessions = extract_json_from_text(result)
        if isinstance(sessions, dict):
            sessions = [sessions]
        if not isinstance(sessions, list):
            raise ValueError(f"listではありません: {type(sessions)}")
    except Exception as e:
        print(f"  ⚠️ パースエラー: {e}")
        print(f"  Claude 全応答: {result[:1000]}")
        return existing

    existing_map = {s["id"]: s for s in existing.get("sessions", [])}
    for s in sessions:
        if "id" in s:
            existing_map[s["id"]] = s
    updated = dict(existing)
    updated["sessions"] = list(existing_map.values())
    updated["lastUpdated"] = datetime.now(JST).isoformat()
    print(f"  ✅ 国会セッション: {len(updated['sessions'])} 件")
    return updated


# ===== メイン =====

def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ 環境変数 ANTHROPIC_API_KEY が設定されていません")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    print("=== 選挙カレンダー自動更新 ===")
    print(f"実行日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}")

    # 選挙データ
    elections = load_json(ELECTIONS_FILE)

    print("\n[A] Wikipedia 補欠選挙テーブルを直接パース（Claude不使用）")
    elections = update_from_wikipedia(elections)

    print("\n[B] 定期選挙＋補欠選挙を web_search で検索")
    time.sleep(5)
    elections = update_all_elections(client, elections)

    save_json(ELECTIONS_FILE, elections)
    print(f"\n✅ 選挙データ合計: {len(elections.get('elections', []))} 件")

    # 国会日程
    print("\n[C] 国会日程を更新")
    time.sleep(20)
    diet = load_json(DIET_FILE)
    diet = update_diet(client, diet)
    save_json(DIET_FILE, diet)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
