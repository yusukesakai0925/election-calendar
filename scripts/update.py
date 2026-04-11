#!/usr/bin/env python3
"""
選挙カレンダー自動更新スクリプト

【情報源】
  A. 直接 fetch（構造化データを確実に取得）
     - Wikipedia「YYYY年日本の補欠選挙」
     - 総務省 選挙情報ページ
     - 各都道府県選挙管理委員会（ClaudeにURLを探させてから fetch）

  B. Claude API web_search（予定選挙・国会日程）
     - 知事選・市長選・統一地方選・参院選の日程
     - 国会会期
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
  "type": "選挙種別（例: 参院選, 衆院選, 知事選, 市長選, 町長選, 市議選, 県議選, 補選）",
  "level": "national / pref / city / town",
  "region": "地域名（例: 滋賀県近江八幡市・竜王町）",
  "prefecture": "都道府県名またはnull",
  "announcementDate": "公示日 YYYY-MM-DD またはnull",
  "announcementDateEarliest": "公示日の最早見込み YYYY-MM-DD またはnull",
  "announcementDateLatest": "公示日の最遅見込み YYYY-MM-DD またはnull",
  "announcementDateLabel": "人間が読める日程表示（例: 2026年4月10日）",
  "electionDay": "投票日 YYYY-MM-DD またはnull",
  "electionDayEarliest": "投票日の最早見込み YYYY-MM-DD またはnull",
  "electionDayLatest": "投票日の最遅見込み YYYY-MM-DD またはnull",
  "electionDayLabel": "人間が読める投票日表示",
  "certainty": "confirmed / estimated / unknown",
  "status": "scheduled / confirmed / completed / cancelled",
  "isUnexpected": true または false,
  "source": "情報源（例: Wikipedia, 総務省, 滋賀県選管）",
  "note": "補足（例: 重田剛県議の辞職に伴う）"
}
"""

DIET_SCHEMA = """
{
  "id": "ユニークID（例: 221st-special）",
  "name": "国会名（例: 第221回国会（特別国会））",
  "type": "常会 / 臨時会 / 特別会",
  "openDate": "開会日 YYYY-MM-DD",
  "closeDate": "閉会日 YYYY-MM-DD またはnull",
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
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    m = re.search(r"(\[[\s\S]+\]|\{[\s\S]+\})", text)
    if m:
        return json.loads(m.group(1).strip())
    raise ValueError("JSON が見つかりませんでした")


def fetch_url(url: str, timeout: int = FETCH_TIMEOUT) -> str:
    """URL を取得してテキストを返す。失敗時は空文字を返す。"""
    try:
        resp = requests.get(url, headers=FETCH_HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
        return resp.text
    except Exception as e:
        print(f"  ⚠️ fetch 失敗 ({url}): {e}")
        return ""


def html_to_text(html: str, max_chars: int = 12000) -> str:
    """HTML から本文テキストを抽出（Claude へ渡す用）"""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # 連続する空行を削除
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def merge_elections(existing: dict, new_elections: list, force_unexpected: bool = False) -> dict:
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


# ===== Claude API 呼び出し =====

def call_claude(client: anthropic.Anthropic, prompt: str,
                use_search: bool = False, max_uses: int = 8) -> str:
    tools = []
    if use_search:
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": max_uses}]

    messages = [{"role": "user", "content": prompt}]

    while True:
        kwargs = dict(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools

        response = client.messages.create(**kwargs)

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": block.input.get("query", ""),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""


def parse_elections_from_text(client: anthropic.Anthropic,
                               source_text: str, source_name: str,
                               is_unexpected: bool, today_str: str) -> list:
    """テキスト（HTML変換後）を Claude にパースさせて選挙リストを返す"""
    unexpected_note = "isUnexpected は必ず true にすること。" if is_unexpected else "isUnexpected は false にすること（補選は true）。"
    prompt = f"""
以下は「{source_name}」から取得したテキストです。今日は{today_str}です。

このテキストから、今後実施される（または最近実施された）日本の選挙情報をすべて抽出し、
以下のスキーマに従った JSON 配列を出力してください。
{unexpected_note}
過去の選挙は status を "completed" にしてください。

スキーマ:
{ELECTIONS_SCHEMA}

---テキスト開始---
{source_text}
---テキスト終了---

出力は必ず JSON 配列のみ（コードブロック可）。説明文は不要です。
"""
    result = call_claude(client, prompt, use_search=False)
    try:
        elections = extract_json_from_text(result)
        if not isinstance(elections, list):
            raise ValueError("配列ではありません")
        return elections
    except Exception as e:
        print(f"  ⚠️ パースエラー ({source_name}): {e}")
        return []


# ===== A. 直接 fetch による情報収集 =====

def update_from_wikipedia(client: anthropic.Anthropic, existing: dict) -> dict:
    """Wikipedia の補欠選挙ページを直接 fetch して全補選を取得"""
    today = datetime.now(JST)
    today_str = today.strftime("%Y年%m月%d日")

    # 当年・翌年の両方を取得
    results = []
    for year in [today.year, today.year + 1]:
        title = f"{year}年日本の補欠選挙"
        url = f"https://ja.wikipedia.org/wiki/{urllib.parse.quote(title)}"
        print(f"  📖 Wikipedia fetch: {title}")
        html = fetch_url(url)
        if not html:
            continue
        text = html_to_text(html, max_chars=15000)
        elections = parse_elections_from_text(
            client, text, f"Wikipedia「{title}」", True, today_str
        )
        results.extend(elections)
        time.sleep(1)  # Wikipedia への負荷軽減

    print(f"  → Wikipedia から {len(results)} 件抽出")
    return merge_elections(existing, results, force_unexpected=True)


def update_from_soumu(client: anthropic.Anthropic, existing: dict) -> dict:
    """総務省の選挙情報ページを fetch して選挙情報を取得"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    urls = [
        ("総務省 選挙情報", "https://www.soumu.go.jp/senkyo/senkyo_s/news/"),
        ("総務省 選挙期日等一覧", "https://www.soumu.go.jp/senkyo/senkyo_s/data/"),
    ]

    results = []
    for name, url in urls:
        print(f"  📖 fetch: {name}")
        html = fetch_url(url)
        if not html:
            continue
        text = html_to_text(html, max_chars=12000)
        elections = parse_elections_from_text(client, text, name, False, today_str)
        results.extend(elections)
        time.sleep(1)

    print(f"  → 総務省から {len(results)} 件抽出")
    return merge_elections(existing, results)


def update_from_pref_senkans(client: anthropic.Anthropic, existing: dict) -> dict:
    """
    各都道府県選挙管理委員会のURLをClaudeに探させ、直接 fetch してパース。
    補選が多い・直近の都道府県を優先的にカバーする。
    """
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")

    # Step1: Claudeに直近の補選が発生している都道府県と選管URLを探させる
    print("  🔍 都道府県選管URLを検索中…")
    url_prompt = f"""
今日は{today_str}です。
web_search を使って、最近（過去2ヶ月以内）に補欠選挙の告示・実施が発表された
日本の都道府県・市区町村を調べ、その選挙管理委員会の公式ウェブサイトURLを列挙してください。

都道府県レベルの選管URL例：
- 滋賀県: https://www.pref.shiga.lg.jp/senkyo/
- 東京都: https://www.senkyo.metro.tokyo.lg.jp/
- 大阪府: https://www.pref.osaka.lg.jp/senkyo/

出力形式（JSON配列）:
[
  {{"pref": "滋賀県", "url": "https://www.pref.shiga.lg.jp/senkyo/tihou/", "reason": "県議補選が4月19日"}},
  ...
]
JSON 配列のみ出力。説明不要。
"""
    url_result = call_claude(client, url_prompt, use_search=True, max_uses=8)
    try:
        pref_urls = extract_json_from_text(url_result)
        if not isinstance(pref_urls, list):
            pref_urls = []
    except Exception:
        pref_urls = []

    print(f"  → {len(pref_urls)} 件の都道府県選管URLを取得")

    # Step2: 各URLを fetch してパース
    results = []
    for item in pref_urls[:10]:  # 最大10件（コスト制限）
        url = item.get("url", "")
        pref = item.get("pref", url)
        if not url:
            continue
        print(f"  📖 fetch: {pref} ({url})")
        html = fetch_url(url)
        if not html:
            continue
        text = html_to_text(html, max_chars=10000)
        elections = parse_elections_from_text(
            client, text, f"{pref} 選挙管理委員会", False, today_str
        )
        results.extend(elections)
        time.sleep(1.5)  # 各選管への負荷軽減

    print(f"  → 都道府県選管から {len(results)} 件抽出")
    return merge_elections(existing, results)


# ===== B. Claude web_search による情報収集 =====

def update_scheduled_elections(client: anthropic.Anthropic, existing: dict) -> dict:
    """通常の予定選挙を web_search で取得（統一地方選・知事選・参院選など）"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    existing_ids = [e["id"] for e in existing.get("elections", [])]

    prompt = f"""
あなたは日本の選挙日程の専門家です。今日は{today_str}です。

web_search を使って、今後予定されている日本の「通常選挙」の日程を収集してください。
（補欠選挙は除く）

検索キーワード：
- 「知事選 2026 2027 日程 公示日 投票日」
- 「統一地方選挙 2027 日程」
- 「参院選 2028 日程」
- 「政令市長選 2026 2027」

既存ID（重複不要）: {json.dumps(existing_ids, ensure_ascii=False)}

スキーマに従った JSON 配列を出力。isUnexpected は false。

スキーマ:
{ELECTIONS_SCHEMA}

JSON 配列のみ（コードブロック可）。説明不要。
"""
    print("  🔍 予定選挙を web_search で検索中…")
    result = call_claude(client, prompt, use_search=True, max_uses=10)
    try:
        elections = extract_json_from_text(result)
        if not isinstance(elections, list):
            raise ValueError()
    except Exception as e:
        print(f"  ⚠️ エラー: {e}")
        return existing
    return merge_elections(existing, elections)


def update_unexpected_via_search(client: anthropic.Anthropic, existing: dict) -> dict:
    """補選・解散情報を web_search で補完（fetch で拾えなかった分）"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    existing_ids = [e["id"] for e in existing.get("elections", [])]

    prompt = f"""
あなたは日本の選挙日程の専門家です。今日は{today_str}です。

web_search を使って、Wikipedia や各報道機関から補欠選挙情報を収集してください。

検索：
- 「衆議院補欠選挙 2026」
- 「参議院補欠選挙 2026」
- 「知事 辞職 補欠選挙 2026」
- 「市長 辞職 補欠選挙 2026」
- 「解散総選挙 可能性 2026」
- Wikipedia「2026年日本の補欠選挙」

既存ID（重複不要）: {json.dumps(existing_ids, ensure_ascii=False)}

isUnexpected は必ず true。
スキーマ:
{ELECTIONS_SCHEMA}

JSON 配列のみ（コードブロック可）。説明不要。
"""
    print("  🔍 補選を web_search で検索中…")
    result = call_claude(client, prompt, use_search=True, max_uses=10)
    try:
        elections = extract_json_from_text(result)
        if not isinstance(elections, list):
            raise ValueError()
    except Exception as e:
        print(f"  ⚠️ エラー: {e}")
        return existing
    return merge_elections(existing, elections, force_unexpected=True)


def update_diet(client: anthropic.Anthropic, existing: dict) -> dict:
    """国会会期を web_search で取得"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")

    # 総務省 or 衆議院の会期ページも fetch
    diet_urls = [
        ("衆議院 国会会期一覧",
         "https://www.shugiin.go.jp/internet/itdb_annai.nsf/html/statics/shiryo/kaiki.htm"),
    ]
    fetched_text = ""
    for name, url in diet_urls:
        print(f"  📖 fetch: {name}")
        html = fetch_url(url)
        if html:
            fetched_text += f"\n\n=={name}==\n" + html_to_text(html, 6000)
        time.sleep(1)

    prompt = f"""
あなたは日本の国会日程の専門家です。今日は{today_str}です。

以下の資料と web_search を使って、現在・直近の国会会期情報を収集してください。

資料:
{fetched_text[:8000] if fetched_text else "（取得できませんでした）"}

web_search キーワード：「国会 会期 {datetime.now(JST).year} 開会日 閉会日」

スキーマ:
{DIET_SCHEMA}

JSON 配列のみ（コードブロック可）。説明不要。
"""
    print("  🔍 国会日程を検索中…")
    result = call_claude(client, prompt, use_search=True, max_uses=5)
    try:
        sessions = extract_json_from_text(result)
        if not isinstance(sessions, list):
            raise ValueError()
    except Exception as e:
        print(f"  ⚠️ エラー: {e}")
        return existing

    existing_map = {s["id"]: s for s in existing.get("sessions", [])}
    for s in sessions:
        if "id" not in s:
            continue
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

    elections = load_json(ELECTIONS_FILE)

    print("\n[A-1] Wikipedia 補欠選挙ページを直接 fetch")
    elections = update_from_wikipedia(client, elections)

    print("\n[A-2] 総務省 選挙情報ページを直接 fetch")
    elections = update_from_soumu(client, elections)

    print("\n[A-3] 都道府県選管ページを直接 fetch")
    elections = update_from_pref_senkans(client, elections)

    print("\n[B-1] 予定選挙を web_search で検索")
    elections = update_scheduled_elections(client, elections)

    print("\n[B-2] 補選・急選を web_search で補完")
    elections = update_unexpected_via_search(client, elections)

    save_json(ELECTIONS_FILE, elections)
    print(f"\n✅ 選挙データ合計: {len(elections.get('elections', []))} 件")

    print("\n[C] 国会日程を更新")
    diet = load_json(DIET_FILE)
    diet = update_diet(client, diet)
    save_json(DIET_FILE, diet)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
