#!/usr/bin/env python3
"""
選挙カレンダー自動更新スクリプト

【方針】
  A. 選挙ドットコム（go2senkyo.com）をスクレイピング（Claude 不使用）
     - /schedule/{year} のテーブルを直接解析
     - 全国の市区町村レベルまで網羅

  B. Claude API web_search
     - 告示日・定数・当選難度など、スクレイピングで取れない詳細情報を補完
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
from bs4 import BeautifulSoup

JST = timezone(timedelta(hours=9))

PREF_CODE_MAP = {
    "1": "北海道", "2": "青森県", "3": "岩手県", "4": "宮城県", "5": "秋田県",
    "6": "山形県", "7": "福島県", "8": "茨城県", "9": "栃木県", "10": "群馬県",
    "11": "埼玉県", "12": "千葉県", "13": "東京都", "14": "神奈川県", "15": "新潟県",
    "16": "富山県", "17": "石川県", "18": "福井県", "19": "山梨県", "20": "長野県",
    "21": "岐阜県", "22": "静岡県", "23": "愛知県", "24": "三重県", "25": "滋賀県",
    "26": "京都府", "27": "大阪府", "28": "兵庫県", "29": "奈良県", "30": "和歌山県",
    "31": "鳥取県", "32": "島根県", "33": "岡山県", "34": "広島県", "35": "山口県",
    "36": "徳島県", "37": "香川県", "38": "愛媛県", "39": "高知県", "40": "福岡県",
    "41": "佐賀県", "42": "長崎県", "43": "熊本県", "44": "大分県", "45": "宮崎県",
    "46": "鹿児島県", "47": "沖縄県",
}
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
  "seats": 定数（整数またはnull）,
  "candidateCount": 現時点の立候補者数（整数またはnull、不明ならnull）,
  "source": "情報源URL",
  "note": "補足"
}
"""

COMPETITIVENESS_SCHEMA = """
{
  "id": "選挙ID",
  "seats": 定数（整数またはnull）,
  "candidateCount": 現時点の立候補者数（整数またはnull）,
  "competitiveness": {
    "level": "high / medium / low / unknown",
    "label": "激戦 / やや激戦 / 優勢 / 不明",
    "note": "一行で根拠を説明（例: 現職と新人の一騎打ちで拮抗）"
  }
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


# ===== A. 選挙ドットコム スクレイピング =====

def _derive_level_type(name: str):
    """選挙名から (level, type) を推定する"""
    if re.search(r"衆議院|衆院", name):
        return "national", "衆院選"
    if re.search(r"参議院|参院", name):
        return "national", "参院選"
    if "知事" in name:
        return "pref", "知事選"
    if re.search(r"都議|道議|府議|県議", name):
        return "pref", "県議選"
    if "市長" in name:
        return "city", "市長選"
    if "区長" in name:
        return "city", "区長選"
    if "町長" in name:
        return "town", "町長選"
    if "村長" in name:
        return "town", "村長選"
    if "市議" in name:
        return "city", "市議選"
    if "区議" in name:
        return "city", "区議選"
    if "町議" in name:
        return "town", "町議選"
    if "村議" in name:
        return "town", "村議選"
    return "city", "その他"


def _derive_region(name: str, prefecture: str) -> str:
    """選挙名と都道府県から地域名を推定する"""
    if not prefecture:
        return "全国"
    # 市区町村名を選挙名の先頭部分から抽出
    for suffix in ["市長", "区長", "町長", "村長", "市議", "区議", "町議", "村議"]:
        if suffix in name:
            idx = name.index(suffix)
            city_name = name[:idx] + suffix[0]  # 例: "前橋" + "市" = "前橋市"
            if city_name:
                return prefecture + city_name
            break
    # 都道府県レベル（知事選・県議選など）はそのまま
    return prefecture


def scrape_go2senkyo(existing: dict) -> dict:
    """選挙ドットコムの /schedule/{year} をスクレイピングして選挙データを取得"""
    today = datetime.now(JST).date()
    year = today.year
    new_elections = []

    for y in [year, year + 1]:
        url = f"https://go2senkyo.com/schedule/{y}"
        print(f"  🌐 {url}")
        html = fetch_url(url)
        if not html:
            continue
        time.sleep(2)  # サーバー負荷軽減

        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="m_schedule_tab_table")
        if not table:
            print(f"  ⚠️ テーブルが見つかりません ({y}年)")
            continue

        current_date = None
        count = 0
        for row in table.find("tbody").find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            # 投票日（circle_inner が存在する行だけ日付を更新）
            circle = cells[0].find("div", class_="circle_inner")
            if circle:
                raw = circle.get_text(strip=True)
                try:
                    current_date = datetime.strptime(raw, "%Y/%m/%d").date()
                except ValueError:
                    pass

            if not current_date:
                continue

            # 選挙名・go2senkyo内部ID
            a_tag = cells[1].find("a")
            if not a_tag:
                continue
            name = a_tag.get_text(strip=True)
            href = a_tag.get("href", "")
            senkyo_id = href.rstrip("/").split("/")[-1]
            if not senkyo_id.isdigit():
                continue

            # 都道府県
            prefecture = ""
            if len(cells) >= 3:
                pref_a = cells[2].find("a")
                if pref_a:
                    pref_href = pref_a.get("href", "")
                    pref_code = pref_href.rstrip("/").split("/")[-1]
                    prefecture = PREF_CODE_MAP.get(pref_code, pref_a.get_text(strip=True))

            level, type_ = _derive_level_type(name)
            region = _derive_region(name, prefecture)
            is_unexpected = bool(re.search(r"補欠|補選", name))
            election_day_str = current_date.strftime("%Y-%m-%d")
            status = "completed" if current_date < today else "scheduled"
            m, d = current_date.month, current_date.day
            day_label = f"投開票日：{current_date.year}年{m}月{d}日"

            new_elections.append({
                "id": f"go2senkyo-{senkyo_id}",
                "name": name,
                "type": type_,
                "level": level,
                "region": region,
                "prefecture": prefecture or None,
                "announcementDate": None,
                "announcementDateLabel": "告示日：未定",
                "electionDay": election_day_str,
                "electionDayEarliest": election_day_str,
                "electionDayLatest": election_day_str,
                "electionDayLabel": day_label,
                "certainty": "confirmed",
                "status": status,
                "isUnexpected": is_unexpected,
                "source": href,
                "note": "",
            })
            count += 1

        print(f"  → {y}年: {count} 件取得")

    print(f"  合計: {len(new_elections)} 件スクレイプ")
    return merge_elections(existing, new_elections)


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
    """go2senkyo で取得できなかった告示日などを Claude web_search で補完する"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")
    year = datetime.now(JST).year

    # 告示日が不明な今後の選挙を対象に補完を依頼
    missing_ann = [
        {"id": e["id"], "name": e["name"], "electionDay": e.get("electionDay")}
        for e in existing.get("elections", [])
        if e.get("status") not in ("completed", "cancelled")
        and not e.get("announcementDate")
    ]

    if not missing_ann:
        print("  補完対象なし（全選挙に告示日あり）")
        return existing

    prompt = f"""今日は{today_str}です。以下の日本の選挙の**告示日（公示日）**を web_search で調べてください。

【対象選挙（告示日が未設定）】
{json.dumps(missing_ann, ensure_ascii=False, indent=2)}

【重要】
- announcementDate のみ調べれば十分です（投票日は既知）
- 不明な場合は null のまま返してください
- 衆院選・参院選は「公示日」と呼びます

以下スキーマで **JSONコードブロック（```json ... ```）のみ** 出力してください。説明文は不要です。
出力する配列には id と announcementDate（YYYY-MM-DD または null）だけ含めてください。

```json
[
  {{"id": "go2senkyo-12345", "announcementDate": "2026-05-10"}}
]
```
"""
    print(f"  🔍 告示日を補完中（{len(missing_ann)} 件）…")
    result = call_claude(client, prompt, use_search=True, max_uses=8)
    if not result:
        print("  ⚠️ Claude が空文字を返しました")
        return existing
    print(f"  Claude 応答（先頭200字）: {result[:200]}")
    try:
        items = extract_json_from_text(result)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise ValueError(f"listではありません: {type(items)}")
    except Exception as e:
        print(f"  ⚠️ パースエラー: {e}")
        print(f"  Claude 全応答: {result[:1000]}")
        return existing

    id_map = {e["id"]: e for e in existing.get("elections", [])}
    updated = 0
    for item in items:
        eid = item.get("id")
        ann = item.get("announcementDate")
        if eid and eid in id_map and ann:
            el = id_map[eid]
            el["announcementDate"] = ann
            d = datetime.strptime(ann, "%Y-%m-%d")
            el["announcementDateLabel"] = f"告示日：{d.year}年{d.month}月{d.day}日"
            updated += 1
    print(f"  ✅ 告示日を {updated} 件補完")
    result_data = dict(existing)
    result_data["elections"] = list(id_map.values())
    result_data["lastUpdated"] = datetime.now(JST).isoformat()
    return result_data


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


# ===== D. 当選難度・議席・候補者数 =====

def update_competitiveness(client: anthropic.Anthropic, existing: dict) -> dict:
    """選挙ごとに議席数・候補者数・当選難度を web_search で調べて付与する"""
    today_str = datetime.now(JST).strftime("%Y年%m月%d日")

    # 対象: 国政・都道府県レベルの選挙 + 補選・急選のみ
    targets = [e for e in existing.get("elections", [])
               if e.get("status") not in ("completed", "cancelled")
               and (e.get("level") in ("national", "pref") or e.get("isUnexpected"))]
    if not targets:
        print("  対象選挙なし")
        return existing

    target_list = json.dumps(
        [{"id": e["id"], "name": e["name"]} for e in targets],
        ensure_ascii=False, indent=2
    )

    prompt = f"""今日は{today_str}です。以下の日本の選挙について、web_search で各選挙の定数・候補者数・当選難度を調べてください。

【対象選挙】
{target_list}

【各選挙について調べること】
1. 定数（議席数）
2. 立候補者数（現時点で判明している分。未発表ならnull）
3. 当選難度（激戦か否か）
   - high: 激戦（接戦・新人複数・現職苦戦など）
   - medium: やや激戦（一定の競争あり）
   - low: 優勢（現職が圧倒的に有利、または無投票の可能性）
   - unknown: 情報不足

以下スキーマの **JSONコードブロック（```json ... ```）のみ** 出力してください。説明文は不要です。
対象選挙すべてを配列で出力してください。情報が見つからない場合もnullを入れて出力してください。

{COMPETITIVENESS_SCHEMA}

出力例（配列形式）:
```json
[
  {{
    "id": "example-2026",
    "seats": 4,
    "candidateCount": 6,
    "competitiveness": {{
      "level": "high",
      "label": "激戦",
      "note": "定数4に対し6人が立候補、現職2人に新人4人が挑む構図"
    }}
  }}
]
```
"""
    print("  🔍 当選難度・議席・候補者数を調査中…")
    result = call_claude(client, prompt, use_search=True, max_uses=8)
    if not result:
        print("  ⚠️ Claude が空文字を返しました")
        return existing

    print(f"  Claude 応答（先頭300字）: {result[:300]}")
    try:
        items = extract_json_from_text(result)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            raise ValueError(f"listではありません: {type(items)}")
    except Exception as e:
        print(f"  ⚠️ パースエラー: {e}")
        print(f"  Claude 全応答: {result[:1000]}")
        return existing

    id_map = {e["id"]: e for e in existing.get("elections", [])}
    updated_count = 0
    for item in items:
        eid = item.get("id")
        if not eid or eid not in id_map:
            continue
        el = id_map[eid]
        if item.get("seats") is not None:
            el["seats"] = item["seats"]
        if item.get("candidateCount") is not None:
            el["candidateCount"] = item["candidateCount"]
        if item.get("competitiveness"):
            el["competitiveness"] = item["competitiveness"]
        updated_count += 1

    print(f"  ✅ {updated_count} 件を更新")
    result_data = dict(existing)
    result_data["elections"] = list(id_map.values())
    result_data["lastUpdated"] = datetime.now(JST).isoformat()
    return result_data


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

    print("\n[A] 選挙ドットコムをスクレイピング")
    elections = scrape_go2senkyo(elections)
    save_json(ELECTIONS_FILE, elections)
    print(f"✅ 選挙データ合計: {len(elections.get('elections', []))} 件")

    print("\n[B] Claude web_search で告示日・詳細を補完")
    time.sleep(5)
    elections = update_all_elections(client, elections)
    save_json(ELECTIONS_FILE, elections)
    print(f"✅ 選挙データ合計: {len(elections.get('elections', []))} 件")

    # 当選難度・議席・候補者数
    print("\n[D] 当選難度・議席・候補者数を調査")
    time.sleep(20)
    elections = update_competitiveness(client, elections)
    save_json(ELECTIONS_FILE, elections)

    # 国会日程
    print("\n[C] 国会日程を更新")
    time.sleep(20)
    diet = load_json(DIET_FILE)
    diet = update_diet(client, diet)
    save_json(DIET_FILE, diet)

    print("\n=== 完了 ===")


if __name__ == "__main__":
    main()
