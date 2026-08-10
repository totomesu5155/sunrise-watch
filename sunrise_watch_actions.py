from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
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

MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 15


class TemporaryCheckError(RuntimeError):
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


def status_from_text(text: str) -> str | None:
    t = normalize_text(text)
    if not t:
        return None

    if "残りわずか" in t or "△" in t:
        return "△"
    if "空席あり" in t or "○" in t or "〇" in t:
        return "○"
    if "満席" in t or "選択不可" in t or "×" in t or "✕" in t or "✖" in t:
        return "×"
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


def extract_from_table(page, equipment: str) -> dict[str, str] | None:
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
        for cell in normalized[equipment_index + 1:]:
            st = status_from_text(cell)
            if st is not None:
                statuses.append(st)

        if statuses:
            result = {"空席": statuses[0]}
            if len(statuses) >= 2:
                result["空席2"] = statuses[1]
            return result

    return None


def collect_card_candidates(page, equipment: str) -> list[dict]:
    """
    tableではないカード型画面から空席状態候補を探す。
    close/remove系の×ボタンは除外する。
    """
    return page.evaluate(
        """
        (equipment) => {
          const norm = s => (s || '').replace(/\\s+/g, ' ').trim();

          const visible = el => {
            const cs = getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };

          const ownText = el => {
            let s = '';
            for (const n of el.childNodes) {
              if (n.nodeType === Node.TEXT_NODE) s += ' ' + n.textContent;
            }
            for (const a of ['alt', 'title', 'aria-label', 'value']) {
              const v = el.getAttribute && el.getAttribute(a);
              if (v) s += ' ' + v;
            }
            return norm(s);
          };

          const classify = t => {
            if (!t) return null;
            if (t.includes('残りわずか') || t === '△') return '△';
            if (t.includes('空席あり') || t === '○' || t === '〇') return '○';
            if (t.includes('満席') || t.includes('選択不可') ||
                t === '×' || t === '✕' || t === '✖') return '×';
            return null;
          };

          const badToken =
            /(close|remove|delete|cancel|clear|dismiss|閉じ|削除|取消)/i;

          const out = [];

          for (const el of document.querySelectorAll('*')) {
            if (!visible(el)) continue;

            const t = ownText(el);
            const status = classify(t);
            if (!status) continue;

            let p = el;
            let context = '';
            let hasEquipment = false;
            let badInteractive = false;
            let depth = 0;

            while (p && depth < 8) {
              const ptxt = norm(p.innerText || p.textContent || '');
              const meta = [
                p.tagName || '',
                p.id || '',
                p.className || '',
                p.getAttribute && p.getAttribute('aria-label') || '',
                p.getAttribute && p.getAttribute('title') || ''
              ].join(' ');

              if (badToken.test(meta)) badInteractive = true;

              if ((p.tagName === 'BUTTON' || p.tagName === 'A') &&
                  (t === '×' || t === '✕' || t === '✖')) {
                badInteractive = true;
              }

              if (ptxt.includes(equipment)) {
                hasEquipment = true;
                context = ptxt.slice(0, 500);
                break;
              }

              p = p.parentElement;
              depth++;
            }

            if (!hasEquipment || badInteractive) continue;

            let score = 0;
            if (t.includes('空席あり') || t.includes('残りわずか') ||
                t.includes('満席') || t.includes('選択不可')) score += 100;
            if (['○','〇','△','×','✕','✖'].includes(t)) score += 60;
            if (context.includes('禁煙') || context.includes('喫煙')) score += 15;
            if (context.includes('個室')) score += 10;

            out.push({
              status,
              text: t.slice(0, 80),
              score,
              context: context.slice(0, 220)
            });
          }

          out.sort((a, b) => b.score - a.score);

          const seen = new Set();
          return out.filter(x => {
            const k = x.status + '|' + x.text + '|' + x.context;
            if (seen.has(k)) return false;
            seen.add(k);
            return true;
          }).slice(0, 10);
        }
        """,
        equipment,
    )


def extract_equipment_status(page, equipment: str) -> dict[str, str]:
    # 1. 旧table形式
    table_result = extract_from_table(page, equipment)
    if table_result:
        return table_result

    # 2. カード型DOM
    candidates = collect_card_candidates(page, equipment)
    if candidates:
        return {"空席": candidates[0]["status"]}

    # 3. e5489の経路そのものが「選択不可」なら空席なし扱い
    body = normalize_text(page.locator("body").inner_text(timeout=10_000))
    if "この経路は選択できません" in body or "選択不可" in body:
        return {"空席": "×"}

    # 4. 選択可能な操作ボタンがある場合は空席あり扱い
    selectable = page.evaluate(
        """
        () => Array.from(
          document.querySelectorAll(
            'button, input[type=button], input[type=submit], a'
          )
        ).some(el => {
          const t = (
            (el.innerText || el.value || el.getAttribute('aria-label') || '') + ''
          ).replace(/\\s+/g, ' ').trim();

          const cs = getComputedStyle(el);
          const r = el.getBoundingClientRect();
          const vis =
            cs.display !== 'none' &&
            cs.visibility !== 'hidden' &&
            r.width > 0 &&
            r.height > 0;

          return vis && /^(選択|選択する|予約|申し込む)$/.test(t);
        })
        """
    )

    if selectable:
        return {"空席": "○"}

    raise RuntimeError(f"{equipment} の空席状態を取得できませんでした")


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
    ntfy_url = require_env("NTFY_TOPIC_URL")
    parts = urllib.parse.urlsplit(ntfy_url)
    topic = urllib.parse.unquote(parts.path.strip("/"))

    if not parts.scheme or not parts.netloc or not topic:
        raise RuntimeError("NTFY_TOPIC_URL の形式が不正です")

    root_url = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, "/", "", "")
    )
    return root_url, topic


def _publish_ntfy(payload: dict) -> None:
    root_url, topic = _ntfy_json_endpoint_and_topic()
    body = dict(payload)
    body["topic"] = topic

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
    try:
        title = page.title()
    except Exception:
        title = "(取得不能)"

    try:
        body = normalize_text(
            page.locator("body").inner_text(timeout=5_000)
        )
    except Exception:
        body = ""

    marks = []
    for mark in (
        "○", "〇", "△", "×", "✕", "✖",
        "選択不可", "空席あり", "満席",
    ):
        if mark in body:
            marks.append(mark)

    return (
        f"title={title!r}, "
        f"result_page={'経路・設備選択' in title}, "
        f"sunrise={'サンライズ' in body}, "
        f"A寝台={'A寝台' in body}, "
        f"B寝台={'B寝台' in body}, "
        f"marks={','.join(marks) or 'none'}"
    )


def validate_page(page) -> None:
    body = normalize_text(
        page.locator("body").inner_text(timeout=10_000)
    )
    title = page.title()

    if "ご案内" in title:
        raise TemporaryCheckError("一時的なご案内ページ")

    if "入力・選択しなおしてください" in body:
        raise TemporaryCheckError("入力・選択しなおしページ")

    if "メンテナンス" in body and "経路・設備選択" not in title:
        raise TemporaryCheckError("メンテナンス案内ページ")

    if "経路・設備選択" not in title and "サンライズ" not in body:
        raise TemporaryCheckError("検索結果ページを確認できない")


def check_url_group_with_retry(
    page,
    group: list[dict[str, str]],
) -> dict[str, dict[str, str] | None]:
    """
    同じURLを共有する対象は1回のページ取得でまとめて判定する。

    例:
      シングルデラックス(A寝台)
      シングルツイン(B寝台)
    は同じURLなので、正常ページを1回取得して両方読む。

    一時的な「ご案内」やDOM読取失敗時のみ最大2回まで再取得する。
    """
    url = group[0]["url"]
    results: dict[str, dict[str, str] | None] = {
        target["name"]: None for target in group
    }

    unresolved = {target["name"] for target in group}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            try:
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=45_000,
                )
            except Exception as exc:
                raise TemporaryCheckError(
                    f"ページ取得失敗 ({type(exc).__name__})"
                ) from None

            page.wait_for_timeout(1200)
            validate_page(page)

        except TemporaryCheckError as exc:
            print(
                f"  -> ページ確認不能 {attempt}/{MAX_ATTEMPTS}: "
                f"{exc}; {page_diagnostic(page)}",
                flush=True,
            )

            if attempt < MAX_ATTEMPTS:
                print(
                    f"  -> {RETRY_WAIT_SECONDS}秒待って同じURLを再試行します",
                    flush=True,
                )
                time.sleep(RETRY_WAIT_SECONDS)
                continue
            break

        # ページが正常なら、同じページから未解決の設備をまとめて読む。
        for target in group:
            name = target["name"]
            if name not in unresolved:
                continue

            equipment = target["equipment"]
            try:
                statuses = extract_equipment_status(page, equipment)
                results[name] = statuses
                unresolved.discard(name)

                if attempt > 1:
                    print(
                        f"  -> {name}: 再試行 {attempt} 回目で確認成功",
                        flush=True,
                    )

            except Exception:
                print(
                    f"  -> {name}: {equipment} の空席状態を取得できない "
                    f"({attempt}/{MAX_ATTEMPTS}); {page_diagnostic(page)}",
                    flush=True,
                )

        if not unresolved:
            break

        if attempt < MAX_ATTEMPTS:
            print(
                f"  -> 未確認 {len(unresolved)}件のため "
                f"{RETRY_WAIT_SECONDS}秒待って同じURLを再取得します",
                flush=True,
            )
            time.sleep(RETRY_WAIT_SECONDS)

    for name in sorted(unresolved):
        print(
            f"  -> {name}: 今回は確認できませんでした。"
            "空席状態は変更せず、次回の定期実行で再確認します。",
            flush=True,
        )

    return results


def group_targets_by_url(
    targets: list[dict[str, str]],
) -> list[list[dict[str, str]]]:
    """元の表示順を保ったまま、同じURLの対象を1グループにまとめる。"""
    groups: list[list[dict[str, str]]] = []
    index_by_url: dict[str, int] = {}

    for target in targets:
        url = target["url"]
        if url in index_by_url:
            groups[index_by_url[url]].append(target)
        else:
            index_by_url[url] = len(groups)
            groups.append([target])

    return groups


def run_once() -> int:
    if in_maintenance():
        print(
            f"[{now_text()}] 01:30-05:30 JST は"
            "メンテナンス時間のため確認しません。"
        )
        return 0

    targets = build_targets()
    groups = group_targets_by_url(targets)
    state = load_state()

    checked = 0
    unavailable = 0
    actual_page_access_groups = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
        )
        page = context.new_page()

        try:
            for group_index, group in enumerate(groups):
                names = " / ".join(t["name"] for t in group)

                print(
                    f"[{now_text()}] CHECK: {names}",
                    flush=True,
                )

                actual_page_access_groups += 1
                group_results = check_url_group_with_retry(
                    page,
                    group,
                )

                for target in group:
                    name = target["name"]
                    url = target["url"]
                    statuses = group_results[name]

                    if statuses is None:
                        unavailable += 1
                        continue

                    checked += 1
                    available = is_available(statuses)
                    status_text = format_statuses(statuses)

                    print(
                        f"  -> {name}: {status_text}",
                        flush=True,
                    )

                    entry = state.setdefault(name, {})
                    was_notified = bool(
                        entry.get("ntfy_notified", False)
                    )

                    if available:
                        if not was_notified:
                            try:
                                send_ntfy(
                                    name,
                                    statuses,
                                    url,
                                )
                                print(
                                    f"  -> {name}: ntfy通知送信",
                                    flush=True,
                                )
                                entry["ntfy_notified"] = True
                            except Exception as exc:
                                print(
                                    f"  -> {name}: ntfy送信失敗 "
                                    f"({type(exc).__name__})。"
                                    "次回再試行します。",
                                    file=sys.stderr,
                                    flush=True,
                                )
                                entry["ntfy_notified"] = False
                    else:
                        entry["ntfy_notified"] = False

                    entry["available"] = available
                    entry["statuses"] = statuses
                    entry["last_checked"] = now_text()
                    save_state(state)

                if group_index < len(groups) - 1:
                    time.sleep(2)

        finally:
            page.close()
            context.close()
            browser.close()

    print(
        f"[{now_text()}] SUMMARY: "
        f"確認成功={checked}, "
        f"今回確認不能={unavailable}, "
        f"URLグループ={actual_page_access_groups}",
        flush=True,
    )

    return 0


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument(
        "--test-ntfy",
        action="store_true",
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
