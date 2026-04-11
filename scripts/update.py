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
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic
import requests

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

    # 名前ベースの重複検出用インデックス（正規化した選挙名）
    def norm(s: str) -> str:
        return re.sub(r"[^\w\u3040-\u9fff]", "", s).lower()

    name_to_id = {norm(e["name"]): eid for eid, e in existing_map.items()}

    # 過去の選挙を自動的に completed に更新
    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    for e in existing_map.values():
        if e.get("electionDay") and e["electionDay"] < today_str and e.get("status") == "scheduled":
            e["status"] = "completed"

    added, updated, skipped = 0, 0, 0
    for e in new_elections:
        if "id" not in e or "name" not in e:
            continue
        if force_unexpected:
            e["isUnexpected"] = True
        # 同名の選挙が別IDで既に存在する場合はスキップ（重複防止）
        n = norm(e["name"])
        if n in name_to_id and name_to_id[n] != e["id"]:
            skipped += 1
            continue
        if e["id"] in existing_map:
            updated += 1
        else:
            added += 1
        existing_map[e["id"]] = e
        name_to_id[n] = e["id"]
    print(f"   → 新規: {added}件、更新: {updated}件、重複スキップ: {skipped}件")
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


# ===== B. Claude web_search =====

def update_all_elections(client: anthropic.Anthropic, existing: dict) -> dict:
    """定期選挙＋補欠選挙・急選を web_search で一括取得"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    year = datetime.now(JST).year
    existing_names = [e["name"] for e in existing.get("elections", [])]

    next_month = (datetime.now(JST).replace(day=1) + timedelta(days=32)).strftime("%Y年%m月")
    prompt = f"""今日は{today_str}です。日本の今後の選挙日程を web_search で調べてください。

【検索してほしいこと】
1. 「知事選 {year} {year + 1} 日程」
2. 「統一地方選 {year + 1}」
3. 「参院選 日程」「衆院選 日程」
4. 「補欠選挙 {year} 日程」「急選 {year}」
5. 「{datetime.now(JST).strftime('%Y年%m月')} 補欠選挙 告示」
6. 「{next_month} 補欠選挙 告示」
7. 「区長選 {year} {year + 1} 日程」「東京23区 区長選」
8. 「区議選 {year} 補欠選挙」

【重要】
- 直近〜2年以内に実施予定のすべての選挙を網羅してください
- 市長選だけでなく区長選（東京23区など特別区）も必ず検索してください
- 市議選・区議選の補欠選挙も漏れなく拾ってください（小規模なものも含む）
- 以下の選挙はすでに登録済みなので出力不要（同名・類似名も不要）:
{json.dumps(existing_names, ensure_ascii=False, indent=2)}

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

    print("\n[B] 定期選挙＋補欠選挙を web_search で検索")
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
