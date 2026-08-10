from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, time as dt_time
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

JST = ZoneInfo("Asia/Tokyo")
MAINTENANCE_START = dt_time(1, 30)
MAINTENANCE_END = dt_time(5, 30)

STATE_FILE = Path(os.getenv("STATE_FILE", ".state/sunrise_state.json"))
AVAILABLE_MARKS = {"○", "△", "あり"}


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


def send_ntfy(name: str, statuses: dict[str, str], url: str) -> None:
    ntfy_url = require_env("NTFY_TOPIC_URL")
    trip_label = require_env("TRIP_LABEL")
    status_text = format_statuses(statuses)

    title = f"サンライズ出雲 空席: {name}"
    message = (
        f"{trip_label}\n"
        f"{name}: {status_text}\n"
        "今すぐe5489を確認してください。"
    )

    req = urllib.request.Request(
        ntfy_url,
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": "urgent",
            "Tags": "rotating_light,train",
            "Click": url,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(f"ntfy HTTP {response.status}")


def send_test_ntfy() -> None:
    ntfy_url = require_env("NTFY_TOPIC_URL")
    req = urllib.request.Request(
        ntfy_url,
        data="GitHub Actions からの ntfy テスト通知です。".encode("utf-8"),
        method="POST",
        headers={
            "Title": "サンライズ監視 GitHub Actions テスト",
            "Priority": "high",
            "Content-Type": "text/plain; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(f"ntfy HTTP {response.status}")


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

    if "ご案内" in title and any(p in body for p in congestion_phrases):
        raise RuntimeError("e5489が混雑・案内ページを返しました")

    if "入力・選択しなおしてください" in body:
        raise RuntimeError("e5489から入力・選択しなおしの案内が返りました")

    if "メンテナンス" in body and "経路・設備選択" not in title:
        raise RuntimeError("e5489のメンテナンス案内が返りました")

    # 正常タイトルを優先。タイトルが変わった場合は本文のサンライズでも確認する。
    if "経路・設備選択" not in title and "サンライズ" not in body:
        raise RuntimeError(f"検索結果ページを確認できませんでした (title={title!r})")


def run_once() -> int:
    # workflow_dispatchをメンテ時間に押してもe5489へはアクセスしない。
    if in_maintenance():
        print(f"[{now_text()}] 01:30-05:30 JST はメンテナンス時間のため確認しません。")
        return 0

    targets = build_targets()
    state = load_state()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()

        # 同じURLは1回の実行中に再アクセスしない。
        loaded_url: str | None = None
        loaded_ok = False

        try:
            for index, target in enumerate(targets):
                name = target["name"]
                url = target["url"]

                print(f"[{now_text()}] CHECK: {name}", flush=True)

                try:
                    if loaded_url != url or not loaded_ok:
                        page.goto(
                            url,
                            wait_until="domcontentloaded",
                            timeout=45_000,
                        )
                        page.wait_for_timeout(1200)
                        validate_page(page)
                        loaded_url = url
                        loaded_ok = True

                    statuses = extract_equipment_status(
                        page,
                        target["equipment"],
                    )
                    available = is_available(statuses)
                    status_text = format_statuses(statuses)
                    print(f"  -> {status_text}", flush=True)

                    entry = state.setdefault(name, {})
                    was_notified = bool(entry.get("ntfy_notified", False))

                    if available:
                        if not was_notified:
                            send_ntfy(name, statuses, url)
                            print("  -> ntfy通知送信", flush=True)
                            entry["ntfy_notified"] = True
                    else:
                        # ×へ戻ったら、次の空席発生時に再通知できるようリセット。
                        entry["ntfy_notified"] = False

                    entry["available"] = available
                    entry["statuses"] = statuses
                    entry["last_checked"] = now_text()
                    save_state(state)

                except Exception as exc:
                    # URLやSecretの値はログに出さない。
                    print(
                        f"[{now_text()}] ERROR [{name}]: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    loaded_url = None
                    loaded_ok = False

                if index < len(targets) - 1:
                    next_url = targets[index + 1]["url"]
                    if next_url != url:
                        time.sleep(2)

        finally:
            page.close()
            context.close()
            browser.close()

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
