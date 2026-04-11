#!/usr/bin/env python3
"""
選挙カレンダー自動更新スクリプト
Claude API の web_search ツールで最新選挙情報を取得し、
data/elections.json と data/diet.json を更新する。
"""

import os
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import anthropic

JST = timezone(timedelta(hours=9))
DATA_DIR = Path(__file__).parent.parent / "data"
ELECTIONS_FILE = DATA_DIR / "elections.json"
DIET_FILE = DATA_DIR / "diet.json"

ELECTIONS_SCHEMA = """
{
  "id": "ユニークID（例: sangiin-2028, kyoto-chiji-2026）",
  "name": "選挙名（例: 第28回参議院議員通常選挙、京都府知事選挙）",
  "type": "選挙種別（例: 参院選, 衆院選, 知事選, 市長選, 町長選, 区長選, 市議選, 補選）",
  "level": "national / pref / city / town",
  "region": "地域名（例: 全国, 京都府, 大阪市, 横浜市中区）",
  "prefecture": "都道府県名またはnull",
  "announcementDate": "公示日 YYYY-MM-DD またはnull（不明な場合）",
  "announcementDateEarliest": "公示日の最早見込み YYYY-MM-DD またはnull",
  "announcementDateLatest": "公示日の最遅見込み YYYY-MM-DD またはnull",
  "announcementDateLabel": "人間が読める日程表示（例: 2026年7月上旬頃）",
  "electionDay": "投票日 YYYY-MM-DD またはnull（不明な場合）",
  "electionDayEarliest": "投票日の最早見込み YYYY-MM-DD またはnull",
  "electionDayLatest": "投票日の最遅見込み YYYY-MM-DD またはnull",
  "electionDayLabel": "人間が読める投票日表示（例: 2026年7月中旬〜下旬頃）",
  "certainty": "confirmed（確定） / estimated（推定） / unknown（未定）",
  "status": "scheduled（予定） / confirmed（確定） / completed（終了） / cancelled（中止）",
  "isUnexpected": false（通常選挙）またはtrue（補選・解散総選挙等の突発選挙）,
  "source": "情報源（例: 総務省, NHK, 朝日新聞）",
  "note": "補足（例: 任期満了に伴う, 現職引退表明）"
}
"""

DIET_SCHEMA = """
{
  "id": "ユニークID（例: 217th-ordinary）",
  "name": "国会名（例: 第217回国会（常会））",
  "type": "常会 / 臨時会 / 特別会",
  "openDate": "開会日 YYYY-MM-DD",
  "closeDate": "閉会日 YYYY-MM-DD またはnull",
  "closeDateUncertain": true/false,
  "milestones": [
    { "date": "YYYY-MM-DD", "label": "イベント名" }
  ]
}
"""


def load_json(path: Path) -> dict:
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ 保存: {path}")


def extract_json_from_text(text: str) -> any:
    """テキストから JSON を抽出する（コードブロックあり/なし両対応）"""
    # コードブロック内を優先
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        return json.loads(m.group(1).strip())
    # 配列またはオブジェクトをそのまま探す
    m = re.search(r"(\[[\s\S]+\]|\{[\s\S]+\})", text)
    if m:
        return json.loads(m.group(1).strip())
    raise ValueError("JSON が見つかりませんでした")


def call_claude_with_search(client: anthropic.Anthropic, prompt: str) -> str:
    """Claude API を web_search ツール付きで呼び出し、最終テキストを返す"""
    messages = [{"role": "user", "content": prompt}]

    while True:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 8,
                }
            ],
            messages=messages,
        )

        # tool_use が含まれていれば続けて処理
        if response.stop_reason == "tool_use":
            # アシスタントの応答をメッセージ履歴に追加
            messages.append({"role": "assistant", "content": response.content})

            # tool_result を作る
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

        # end_turn など → テキストを取り出して返す
        for block in response.content:
            if hasattr(block, "text"):
                return block.text

        return ""


def update_elections(client: anthropic.Anthropic, existing: dict) -> dict:
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    existing_ids = [e["id"] for e in existing.get("elections", [])]

    prompt = f"""
あなたは日本の選挙日程の専門家です。今日は{today_str}です。

web_search を使って、以下の情報を日本語で検索し、できる限り網羅的に収集してください：
1. 今後予定・実施が見込まれる日本のすべての選挙（国政・都道府県・市区町村レベル）
2. 近く行われる補欠選挙・再選挙
3. 解散総選挙の可能性があるニュース

検索キーワード例：
- 「選挙 公示日 投票日 2026」
- 「補欠選挙 2026」
- 「知事選 市長選 2026 2027 日程」
- 「参院選 2028 日程」
- 「国会 解散 総選挙」

既にデータに存在するID一覧（重複不要）：
{json.dumps(existing_ids, ensure_ascii=False)}

収集した情報をもとに、以下のスキーマに従ったJSONの配列を出力してください。
既存IDと異なるものも含め、今後実施される選挙をすべて列挙してください。
日程が未確定なものは certainty を "estimated" または "unknown" にして、
announcementDateLabel / electionDayLabel に「2026年夏頃」などの人間が読める形で記載してください。

スキーマ:
{ELECTIONS_SCHEMA}

出力は必ず JSON 配列のみ（コードブロック可）。説明文は不要です。
"""

    print("🔍 選挙情報を検索中…")
    result_text = call_claude_with_search(client, prompt)

    try:
        new_elections = extract_json_from_text(result_text)
        if not isinstance(new_elections, list):
            raise ValueError("配列ではありません")
    except Exception as e:
        print(f"⚠️ JSON 解析エラー: {e}")
        print("レスポンス先頭:", result_text[:500])
        return existing

    # 既存データにマージ（同一IDは新データで上書き）
    existing_map = {e["id"]: e for e in existing.get("elections", [])}
    for e in new_elections:
        if "id" not in e or "name" not in e:
            continue
        existing_map[e["id"]] = e

    updated = dict(existing)
    updated["elections"] = list(existing_map.values())
    updated["lastUpdated"] = datetime.now(JST).isoformat()
    print(f"✅ 選挙データ: {len(updated['elections'])} 件")
    return updated


def update_diet(client: anthropic.Anthropic, existing: dict) -> dict:
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")

    prompt = f"""
あなたは日本の国会日程の専門家です。今日は{today_str}です。

web_search を使って以下を検索してください：
- 現在開会中の国会の会期（開会日・閉会日・種別）
- 今後召集が予定されている臨時国会・特別国会
- 重要な国会内の出来事（予算成立日、主要法案採決など）

スキーマに従ったJSONの配列を出力してください。

スキーマ:
{DIET_SCHEMA}

出力は必ず JSON 配列のみ（コードブロック可）。説明文は不要です。
"""

    print("🔍 国会日程を検索中…")
    result_text = call_claude_with_search(client, prompt)

    try:
        new_sessions = extract_json_from_text(result_text)
        if not isinstance(new_sessions, list):
            raise ValueError("配列ではありません")
    except Exception as e:
        print(f"⚠️ JSON 解析エラー: {e}")
        return existing

    existing_map = {s["id"]: s for s in existing.get("sessions", [])}
    for s in new_sessions:
        if "id" not in s:
            continue
        existing_map[s["id"]] = s

    updated = dict(existing)
    updated["sessions"] = list(existing_map.values())
    updated["lastUpdated"] = datetime.now(JST).isoformat()
    print(f"✅ 国会セッション: {len(updated['sessions'])} 件")
    return updated


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ 環境変数 ANTHROPIC_API_KEY が設定されていません")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    print("=== 選挙カレンダー自動更新 ===")
    print(f"実行日時: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S JST')}")

    # 選挙データ更新
    elections_data = load_json(ELECTIONS_FILE)
    updated_elections = update_elections(client, elections_data)
    save_json(ELECTIONS_FILE, updated_elections)

    # 国会日程更新
    diet_data = load_json(DIET_FILE)
    updated_diet = update_diet(client, diet_data)
    save_json(DIET_FILE, updated_diet)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
