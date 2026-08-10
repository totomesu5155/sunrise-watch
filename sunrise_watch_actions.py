from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")
MAINTENANCE_START = dt_time(1, 30)
MAINTENANCE_END = dt_time(5, 30)

STATE_FILE = Path(os.getenv("STATE_FILE", ".state/sunrise_state.json"))
AVAILABLE_MARKS = {"○", "△", "あり"}

# e5489が一時的に「ご案内」等を返す場合に備え、同じ対象を少し待って再確認。
MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 15


class TemporaryCheckError(RuntimeError):
    """一時的なページ取得/判定不能。Secret URLを例外文に含めない。"""
    pass


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret が未設定です: {name}")
    return value


def now_jst() -> datetime:
    return datetime.now(JST)


def now_text() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def in_maintenance() -> bool:
    t = now_jst().time().replace(tzinfo=None)
    return MAINTENANCE_START <= t < MAINTENANCE_END


def normalize_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def status_from_cell(text: str) -> str | None:
    t = normalize_text(text)
    if not t:
        return None

    if "残りわずか" in t or "△" in t:
        return "△"
    if (
        "空席あり" in t
        or re.search(r"(^|\s)あり($|\s)", t)
        or "○" in t
        or "〇" in t
    ):
        return "○"
    if "満席" in t or "選択不可" in t or "×" in t or "✕" in t or "✖" in t:
        return "×"
    if t in {"-", "－", "—", "―"}:
        return "－"
    return None


def collect_table_rows(page) -> list[list[str]]:
    return page.evaluate(
        """
        () => Array.from(document.querySelectorAll('tr')).map(tr =>
          Array.from(tr.querySelectorAll('th, td')).map(cell => {
            const parts = [];
            const text = (cell.innerText || cell.textContent || '').trim();
            if (text) parts.push(text);

            for (const el of cell.querySelectorAll('img, input, button, a, span')) {
              for (const attr of ['alt', 'title', 'value', 'aria-label']) {
                const v = el.getAttribute && el.getAttribute(attr);
                if (v) parts.push(v);
              }
              const et = (el.innerText || el.textContent || '').trim();
              if (et) parts.push(et);
            }
            return parts.join(' | ');
          })
        )
        """
    )


def extract_equipment_status(page, equipment: str) -> dict[str, str]:
    rows = collect_table_rows(page)

    for cells in rows:
        normalized = [normalize_text(c) for c in cells]
        equipment_index = next(
            (i for i, c in enumerate(normalized) if equipment in c),
            None,
        )
        if equipment_index is None:
            continue

        statuses: list[str] = []
        for cell in normalized[equipment_index + 1 :]:
            st = status_from_cell(cell)
            if st is not None:
                statuses.append(st)

        if statuses:
            result: dict[str, str] = {}
            if len(statuses) >= 1:
                result["禁煙席"] = statuses[0]
            if len(statuses) >= 2:
                result["喫煙席"] = statuses[1]
            for i, st in enumerate(statuses[2:], start=3):
                result[f"空席欄{i}"] = st
            return result

    raise RuntimeError(f"{equipment} の空席行をページから取得できませんでした")


def is_available(statuses: dict[str, str]) -> bool:
    return any(v in AVAILABLE_MARKS for v in statuses.values())


def format_statuses(statuses: dict[str, str]) -> str:
    return " / ".join(f"{k}:{v}" for k, v in statuses.items())


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(STATE_FILE)


def _ntfy_json_endpoint_and_topic() -> tuple[str, str]:
    """NTFY_TOPIC_URL から ntfy のルートURLとトピック名を取り出す。"""
    ntfy_url = require_env("NTFY_TOPIC_URL")
    parts = urllib.parse.urlsplit(ntfy_url)
    topic = urllib.parse.unquote(parts.path.strip("/"))
    if not parts.scheme or not parts.netloc or not topic:
        raise RuntimeError("NTFY_TOPIC_URL は https://ntfy.sh/トピック名 の形で設定してください")
    root_url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
    return root_url, topic


def _publish_ntfy(payload: dict) -> None:
    root_url, topic = _ntfy_json_endpoint_and_topic()
    body = dict(payload)
    body["topic"] = topic

    # 日本語のtitle/messageはHTTPヘッダーではなくJSON本文に入れる。
    # Python urllib のヘッダーはLatin-1制約があるため、この方式が安全。
    req = urllib.request.Request(
        root_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(f"ntfy HTTP {response.status}")


def send_ntfy(name: str, statuses: dict[str, str], url: str) -> None:
    trip_label = require_env("TRIP_LABEL")
    status_text = format_statuses(statuses)

    _publish_ntfy({
        "title": f"サンライズ出雲 空席: {name}",
        "message": (
            f"{trip_label}\n"
            f"{name}: {status_text}\n"
            "今すぐe5489を確認してください。"
        ),
        "priority": 5,
        "tags": ["rotating_light", "train"],
        "click": url,
    })


def send_test_ntfy() -> None:
    _publish_ntfy({
        "title": "サンライズ監視 GitHub Actions テスト",
        "message": "GitHub Actions からの ntfy テスト通知です。",
        "priority": 4,
    })


def build_targets() -> list[dict[str, str]]:
    return [
        {
            "name": "シングルデラックス",
            "url": require_env("URL_SINGLE_DELUXE"),
            "equipment": "A寝台",
        },
        {
            "name": "シングルツイン",
            "url": require_env("URL_SINGLE_TWIN"),
            "equipment": "B寝台",
        },
        {
            "name": "シングル",
            "url": require_env("URL_SINGLE"),
            "equipment": "B寝台",
        },
        {
            "name": "ソロ",
            "url": require_env("URL_SOLO"),
            "equipment": "B寝台",
        },
        {
            "name": "サンライズツイン",
            "url": require_env("URL_SUNRISE_TWIN"),
            "equipment": "B寝台",
        },
    ]


def page_diagnostic(page) -> str:
    """
    Public Actionsログに出してよい最小限の診断情報。
    URL・本文・旅行条件は出さない。
    """
    try:
        title = page.title()
    except Exception:
        title = "(取得不能)"

    try:
        body = normalize_text(page.locator("body").inner_text(timeout=5_000))
    except Exception:
        body = ""

    return (
        f"title={title!r}, "
        f"result_page={'経路・設備選択' in title}, "
        f"sunrise={'サンライズ' in body}, "
        f"A寝台={'A寝台' in body}, "
        f"B寝台={'B寝台' in body}"
    )


def validate_page(page) -> None:
    body = normalize_text(page.locator("body").inner_text(timeout=10_000))
    title = page.title()

    congestion_phrases = (
        "アクセスが集中しております",
        "アクセスが集中しています",
        "ただいま大変混み合っております",
        "ただいま大変混み合っています",
        "時間をおいてから再度",
        "しばらく時間をおいて",
    )

    # 「ご案内」は一時的に返ることがあるので、即座に恒久エラーとはみなさない。
    if "ご案内" in title:
        if any(p in body for p in congestion_phrases):
            raise TemporaryCheckError("一時的な混雑・案内ページ")
        raise TemporaryCheckError("一時的なご案内ページ")

    if "入力・選択しなおしてください" in body:
        raise TemporaryCheckError("入力・選択しなおしページ")

    if "メンテナンス" in body and "経路・設備選択" not in title:
        raise TemporaryCheckError("メンテナンス案内ページ")

    if "経路・設備選択" not in title and "サンライズ" not in body:
        raise TemporaryCheckError("検索結果ページを確認できない")


def load_target_page_with_retry(page, target: dict) -> dict[str, str] | None:
    """
    最大2回まで確認。
    1回目がご案内/行取得不能でも15秒待って再試行する。
    2回とも確認不能ならNoneを返し、次の5分巡回に任せる。
    """
    name = target["name"]
    url = target["url"]
    equipment = target["equipment"]

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
            except Exception as exc:
                # Playwrightの元例外文にはSecret URLが入る可能性があるので表示しない。
                raise TemporaryCheckError(
                    f"ページ取得失敗 ({type(exc).__name__})"
                ) from None

            page.wait_for_timeout(1200)
            validate_page(page)

            try:
                statuses = extract_equipment_status(page, equipment)
            except Exception:
                raise TemporaryCheckError(
                    f"{equipment} の空席欄を取得できない"
                ) from None

            if attempt > 1:
                print(f"  -> 再試行 {attempt} 回目で確認成功", flush=True)

            return statuses

        except TemporaryCheckError as exc:
            print(
                f"  -> 確認不能 {attempt}/{MAX_ATTEMPTS}: {exc}; "
                f"{page_diagnostic(page)}",
                flush=True,
            )

            if attempt < MAX_ATTEMPTS:
                print(
                    f"  -> {RETRY_WAIT_SECONDS}秒待って再試行します",
                    flush=True,
                )
                time.sleep(RETRY_WAIT_SECONDS)

    print(
        "  -> 今回は確認できませんでした。空席状態は変更せず、"
        "次回の定期実行で再確認します。",
        flush=True,
    )
    return None


def run_once() -> int:
    # workflow_dispatchをメンテ時間に押してもe5489へはアクセスしない。
    if in_maintenance():
        print(
            f"[{now_text()}] 01:30-05:30 JST はメンテナンス時間のため確認しません。"
        )
        return 0

    targets = build_targets()
    state = load_state()

    checked = 0
    unavailable_to_check = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()

        # 同じURLの2対象（デラックス/シングルツイン）でも、
        # A寝台/B寝台を確実に判定するため対象ごとに確認する。
        try:
            for index, target in enumerate(targets):
                name = target["name"]
                url = target["url"]

                print(f"[{now_text()}] CHECK: {name}", flush=True)

                statuses = load_target_page_with_retry(page, target)
                if statuses is None:
                    unavailable_to_check += 1
                else:
                    checked += 1
                    available = is_available(statuses)
                    status_text = format_statuses(statuses)
                    print(f"  -> {status_text}", flush=True)

                    entry = state.setdefault(name, {})
                    was_notified = bool(entry.get("ntfy_notified", False))

                    if available:
                        if not was_notified:
                            try:
                                send_ntfy(name, statuses, url)
                                print("  -> ntfy通知送信", flush=True)
                                entry["ntfy_notified"] = True
                            except Exception as exc:
                                # ntfy URL等をPublicログに出さない。
                                print(
                                    f"  -> ntfy送信失敗 ({type(exc).__name__})。"
                                    "次回再試行します。",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                entry["ntfy_notified"] = False
                    else:
                        # ×へ戻ったら、次の空席発生時に再通知できるようリセット。
                        entry["ntfy_notified"] = False

                    entry["available"] = available
                    entry["statuses"] = statuses
                    entry["last_checked"] = now_text()
                    save_state(state)

                if index < len(targets) - 1:
                    time.sleep(2)

        finally:
            page.close()
            context.close()
            browser.close()

    print(
        f"[{now_text()}] SUMMARY: 確認成功={checked}, "
        f"今回確認不能={unavailable_to_check}",
        flush=True,
    )

    # 一時的な確認不能はAction自体をFailureにしない。
    # 5分後の次回スケジュールで再試行する。
    return 0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--test-ntfy",
        action="store_true",
        help="e5489へアクセスせずntfyだけテスト",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.test_ntfy:
        send_test_ntfy()
        print("ntfyテスト通知を送信しました。")
        return 0
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
