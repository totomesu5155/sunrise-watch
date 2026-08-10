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

# JR側にアクセスしない時間
MAINTENANCE_START = dt_time(1, 30)
MAINTENANCE_END = dt_time(5, 30)

# 必ずトップ画面から入り、「新規予約」を押す
TOP_URL = "https://e5489.jr-odekake.net/e5489/cssp/CBTopMenuSP"

STATE_FILE = Path(os.getenv("STATE_FILE", ".state/sunrise_state.json"))

MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 15

POSITIVE_ALTS = {"空席あり", "空席残りわずか"}
NEGATIVE_ALT = "残席なし"


class TemporaryPageError(RuntimeError):
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


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def current_path(page) -> str:
    try:
        return urllib.parse.urlsplit(page.url).path
    except Exception:
        return ""


def page_diag(page) -> str:
    try:
        title = page.title()
        body = norm(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return "page=unreadable"

    return (
        f"title={title!r}, path={current_path(page)!r}, "
        f"top={'トップメニュー' in title or 'トップメニュー' in body}, "
        f"entry={'日時・発着駅選択' in title or '日時・発着駅選択' in body}, "
        f"route={'経路・設備選択' in title or '経路・設備選択' in body}, "
        f"change={'列車の変更' in title or '列車の変更' in body}"
    )


def is_guide_or_error(page) -> bool:
    title = page.title()
    body = norm(page.locator("body").inner_text(timeout=10000))
    return (
        "ご案内" in title
        or "処理中にエラーが発生しました" in body
        or "入力・選択しなおしてください" in body
        or "アクセスが集中" in body
        or "大変混み合" in body
    )


# ------------------------------------------------------------
# 状態保存
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# ntfy
# ------------------------------------------------------------

def ntfy_root_topic() -> tuple[str, str]:
    url = require_env("NTFY_TOPIC_URL")
    p = urllib.parse.urlsplit(url)
    topic = urllib.parse.unquote(p.path.strip("/"))

    if not p.scheme or not p.netloc or not topic:
        raise RuntimeError("NTFY_TOPIC_URL の形式が不正です")

    root = urllib.parse.urlunsplit(
        (p.scheme, p.netloc, "/", "", "")
    )
    return root, topic


def publish_ntfy(payload: dict) -> None:
    root, topic = ntfy_root_topic()
    body = dict(payload)
    body["topic"] = topic

    req = urllib.request.Request(
        root,
        data=json.dumps(
            body,
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(
                f"ntfy HTTP {response.status}"
            )


def send_ntfy(stage: str, details: str) -> None:
    publish_ntfy({
        "title": "サンライズ出雲 空席あり",
        "message": (
            f"{require_env('TRIP_LABEL')}\n"
            f"{stage}\n"
            f"{details}\n"
            "e5489を確認してください。"
        ),
        "priority": 5,
        "tags": ["rotating_light", "train"],
        "click": TOP_URL,
    })


def send_test_ntfy() -> None:
    publish_ntfy({
        "title": "サンライズ監視 GitHub Actions テスト",
        "message": "新規予約ボタン経由版のntfyテスト通知です。",
        "priority": 4,
    })


# ------------------------------------------------------------
# 1. トップ → 新規予約
# ------------------------------------------------------------

def open_top_and_click_new_reservation(page) -> None:
    page.goto(
        TOP_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )
    page.wait_for_timeout(1200)

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "e5489トップがご案内/エラー"
        )

    # 添付された実ページでは、
    # <p class="new-home-index-navigation__ttl">新規予約</p>
    # を含む<a>が新規予約ボタン。
    new_text = page.get_by_text(
        "新規予約",
        exact=True,
    )

    clicked = False

    if new_text.count() > 0:
        try:
            anchor = new_text.first.locator(
                "xpath=ancestor::a[1]"
            )
            if anchor.count() > 0:
                anchor.click()
            else:
                new_text.first.click()

            clicked = True
            print(
                "  -> トップ画面の「新規予約」をクリック",
                flush=True,
            )
        except Exception:
            clicked = False

    # サイト側JSのクリック処理が取れない場合だけ、
    # 添付HTMLで確認できた formTrainSimpleEntry をPOSTする。
    # 内部URLへのGET直リンクはしない。
    if not clicked:
        form = page.locator(
            'form[name="formTrainSimpleEntry"]'
        )

        if form.count() == 0:
            raise TemporaryPageError(
                "トップ画面に新規予約ボタン/フォームが見つからない"
            )

        form.evaluate(
            "form => form.submit()"
        )
        print(
            "  -> 新規予約フォームをPOST送信",
            flush=True,
        )

    page.wait_for_timeout(1500)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "新規予約を押した後にご案内/エラー"
        )

    print(
        f"  -> 新規予約遷移先: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 2. CBTrainSimpleEntrySP → 「駅名を入力」タブ
# ------------------------------------------------------------

def move_to_station_name_entry(page) -> None:
    # すでにCBTrainEntrySPなら何もしない
    if (
        page.locator(
            "#entry-departure-station"
        ).count() > 0
        and page.locator(
            "#entry-arrival-station"
        ).count() > 0
    ):
        return

    # SimpleEntry画面で「駅名を入力」タブを押す
    tab = page.get_by_text(
        "駅名を入力",
        exact=True,
    )

    if tab.count() == 0:
        raise TemporaryPageError(
            "「駅名を入力」タブが見つからない"
        )

    tab.first.click()

    page.wait_for_timeout(1200)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "「駅名を入力」タブ遷移後にご案内/エラー"
        )

    if (
        page.locator(
            "#entry-departure-station"
        ).count() == 0
        or page.locator(
            "#entry-arrival-station"
        ).count() == 0
    ):
        raise TemporaryPageError(
            "日時・発着駅選択画面を確認できない"
        )

    print(
        f"  -> 「駅名を入力」へ移動: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 3. 検索条件
# 添付HTMLからname/id/valueを固定
# ------------------------------------------------------------

def fill_search_conditions(page) -> None:
    depart = require_env("DEPART_STATION")
    arrive = require_env("ARRIVE_STATION")
    travel_date = require_env("TRAVEL_DATE").replace(
        "-", ""
    )
    hour = require_env("DEPART_HOUR").zfill(2)
    minute = require_env(
        "DEPART_MINUTE"
    ).zfill(2)

    page.locator(
        "#entry-departure-station"
    ).fill(depart)

    page.locator(
        "#entry-arrival-station"
    ).fill(arrive)

    page.locator(
        'select[name="inputDate"]'
    ).select_option(value=travel_date)

    page.locator(
        'select[name="inputHour"]'
    ).select_option(value=hour)

    page.locator(
        'select[name="inputMinute"]'
    ).select_option(value=minute)

    # 出発
    page.locator(
        'input[name="inputType"][value="0"]'
    ).check()

    # 一度も乗り換えしない
    page.locator(
        'input[name="inputSearchType"][value="1"]'
    ).check()

    # 新幹線OFF
    shinkansen = page.locator(
        "#reserrve-shinkansen"
    )
    if shinkansen.is_checked():
        shinkansen.uncheck()

    # 特急・急行／快速ON
    limited = page.locator(
        "#reserrve-not-shinkansen"
    )
    if not limited.is_checked():
        limited.check()

    print(
        "  -> 東京→出雲市 / 9月3日 / "
        "21:10 / 乗換なし / 新幹線OFF",
        flush=True,
    )


def submit_search(page) -> None:
    button = page.locator(
        "button.decide-button"
    )

    if button.count() == 0:
        # 表示文字でもフォールバック
        button = page.get_by_text(
            "検索する",
            exact=False,
        )

    if button.count() == 0:
        raise TemporaryPageError(
            "「検索する（新規予約）」ボタンが見つからない"
        )

    button.first.click()

    page.wait_for_timeout(1800)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "検索後にご案内/エラー"
        )

    if page.locator(
        "table.seat-status-table"
    ).count() == 0:
        raise TemporaryPageError(
            "経路・設備選択の空席表が見つからない"
        )

    print(
        f"  -> 検索結果: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 4. 最初の経路・設備選択
#
# 凡例の「空席あり」「残席なし」を誤検出しないよう
# table.seat-status-table 内だけを読む。
# ------------------------------------------------------------

def read_initial_route_status(page) -> dict:
    table = page.locator(
        "table.seat-status-table"
    ).first

    rows = table.locator("tr")
    results = []

    for i in range(rows.count()):
        row = rows.nth(i)

        imgs = row.locator(
            'img[alt="空席あり"], '
            'img[alt="空席残りわずか"], '
            'img[alt="残席なし"]'
        )

        if imgs.count() == 0:
            continue

        label = norm(
            row.inner_text()
        )

        alts = []
        for j in range(imgs.count()):
            alt = imgs.nth(j).get_attribute(
                "alt"
            )
            if alt:
                alts.append(alt)

        results.append({
            "label": label,
            "alts": alts,
        })

    if not results:
        raise TemporaryPageError(
            "最初の空席状態を取得できない"
        )

    positives = []
    total_marks = 0
    negative_marks = 0

    for item in results:
        for alt in item["alts"]:
            total_marks += 1
            if alt in POSITIVE_ALTS:
                positives.append(
                    f"{item['label']}:{alt}"
                )
            elif alt == NEGATIVE_ALT:
                negative_marks += 1

    return {
        "rows": results,
        "positives": positives,
        "all_negative": (
            total_marks > 0
            and negative_marks == total_marks
        ),
        "total": total_marks,
    }


# ------------------------------------------------------------
# 5. 「この列車を変更」→「後の列車」
# ------------------------------------------------------------

def open_later_trains(page) -> None:
    change = page.get_by_text(
        "この列車を変更",
        exact=True,
    )

    if change.count() == 0:
        raise TemporaryPageError(
            "「この列車を変更」が見つからない"
        )

    change.first.click()
    page.wait_for_timeout(500)

    later = page.locator(
        "button.change-next-train-button"
    )

    if later.count() == 0:
        later = page.get_by_text(
            "後の列車",
            exact=True,
        )

    if later.count() == 0:
        raise TemporaryPageError(
            "「後の列車」が見つからない"
        )

    later.first.click()

    page.wait_for_timeout(1500)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "後の列車へ移動後にご案内/エラー"
        )

    if page.locator(
        'button[aria-controls="train-1"]'
    ).count() == 0:
        raise TemporaryPageError(
            "列車変更画面の候補1～3を確認できない"
        )

    print(
        f"  -> 後の列車: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 6. train-1 / train-2 / train-3 の＋を開いて空席確認
# ------------------------------------------------------------

def candidate_name_from_li(li, fallback: str) -> str:
    txt = norm(li.inner_text())

    m = re.search(
        r"特急サンライズ出雲（[^）]+）",
        txt,
    )

    if m:
        return m.group(0)

    return fallback


def read_three_later_trains(page) -> dict:
    positives = []
    details = []
    checked = 0

    for index in range(1, 4):
        panel_id = f"train-{index}"

        button = page.locator(
            f'button[aria-controls="{panel_id}"]'
        )

        panel = page.locator(
            f"#{panel_id}"
        )

        if button.count() == 0 or panel.count() == 0:
            raise TemporaryPageError(
                f"{panel_id} が見つからない"
            )

        # ＋（開閉する）を押す。
        # 既に開いている場合はそのまま読む。
        expanded = (
            button.first.get_attribute(
                "aria-expanded"
            )
            or ""
        ).lower()

        if expanded != "true":
            button.first.click()
            page.wait_for_timeout(350)

        li = panel.first.locator(
            "xpath=ancestor::li[1]"
        )

        if li.count() > 0:
            name = candidate_name_from_li(
                li.first,
                panel_id,
            )
        else:
            name = panel_id

        imgs = panel.first.locator(
            'img[alt="空席あり"], '
            'img[alt="空席残りわずか"], '
            'img[alt="残席なし"]'
        )

        if imgs.count() == 0:
            raise TemporaryPageError(
                f"{name} の空席画像を取得できない"
            )

        alts = []

        for j in range(imgs.count()):
            alt = imgs.nth(j).get_attribute(
                "alt"
            )
            if alt:
                alts.append(alt)

        checked += 1
        details.append(
            f"{name}:{'/'.join(alts)}"
        )

        for alt in alts:
            if alt in POSITIVE_ALTS:
                positives.append(
                    f"{name}:{alt}"
                )

        print(
            f"  -> {name}: {' / '.join(alts)}",
            flush=True,
        )

    return {
        "checked": checked,
        "details": details,
        "positives": positives,
    }


# ------------------------------------------------------------
# 1巡回
# ------------------------------------------------------------

def perform_check(page) -> tuple[bool, str]:
    # 必ずトップから正規遷移
    open_top_and_click_new_reservation(page)

    # SimpleEntry → 駅名入力
    move_to_station_name_entry(page)

    # 条件入力
    fill_search_conditions(page)
    submit_search(page)

    # 最初の検索結果
    initial = read_initial_route_status(
        page
    )

    print(
        f"  -> 初回空席マーク数={initial['total']}, "
        f"空席あり={len(initial['positives'])}",
        flush=True,
    )

    if initial["positives"]:
        return (
            True,
            "初回検索: "
            + " / ".join(
                initial["positives"]
            ),
        )

    if not initial["all_negative"]:
        raise TemporaryPageError(
            "初回結果が「すべて残席なし」ではなく、"
            "○/△もないため判定保留"
        )

    print(
        "  -> 初回はすべて残席なし。"
        "「この列車を変更」→「後の列車」へ",
        flush=True,
    )

    # 後の列車
    open_later_trains(page)

    later = read_three_later_trains(
        page
    )

    if later["positives"]:
        return (
            True,
            "後の列車: "
            + " / ".join(
                later["positives"]
            ),
        )

    return (
        False,
        "初回は全て残席なし。後の列車3候補も○/△なし。 "
        + " / ".join(later["details"]),
    )


def run_once() -> int:
    if in_maintenance():
        print(
            f"[{now_text()}] 01:30-05:30 JST は監視停止。"
            "e5489にはアクセスしません。",
            flush=True,
        )
        return 0

    state = load_state()
    was_notified = bool(
        state.get(
            "ntfy_notified",
            False,
        )
    )

    available = None
    details = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={
                "width": 430,
                "height": 1600,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Mobile Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            for attempt in range(
                1,
                MAX_ATTEMPTS + 1,
            ):
                try:
                    print(
                        f"[{now_text()}] "
                        f"CHECK {attempt}/{MAX_ATTEMPTS}",
                        flush=True,
                    )

                    available, details = (
                        perform_check(page)
                    )
                    break

                except Exception as exc:
                    print(
                        f"  -> 確認不能: "
                        f"{type(exc).__name__}: {exc}; "
                        f"{page_diag(page)}",
                        file=sys.stderr,
                        flush=True,
                    )

                    if attempt < MAX_ATTEMPTS:
                        print(
                            f"  -> {RETRY_WAIT_SECONDS}秒後に"
                            "トップからやり直します",
                            flush=True,
                        )
                        time.sleep(
                            RETRY_WAIT_SECONDS
                        )

        finally:
            page.close()
            context.close()
            browser.close()

    if available is None:
        print(
            f"[{now_text()}] SUMMARY: 今回確認不能。"
            "前回状態は変更しません。",
            flush=True,
        )
        return 0

    if available:
        print(
            f"[{now_text()}] 空席検出: {details}",
            flush=True,
        )

        if not was_notified:
            try:
                stage = (
                    "後の列車で空席を検出"
                    if details.startswith(
                        "後の列車"
                    )
                    else "最初の検索結果で空席を検出"
                )

                send_ntfy(
                    stage,
                    details,
                )

                state[
                    "ntfy_notified"
                ] = True

                print(
                    "  -> ntfy通知送信",
                    flush=True,
                )

            except Exception as exc:
                print(
                    f"  -> ntfy送信失敗 "
                    f"({type(exc).__name__})。"
                    "次回再送します。",
                    file=sys.stderr,
                    flush=True,
                )

                state[
                    "ntfy_notified"
                ] = False

    else:
        print(
            f"[{now_text()}] 空席なし: {details}",
            flush=True,
        )

        # ×へ戻ったら、次に○/△が出た際に再通知
        state[
            "ntfy_notified"
        ] = False

    state["available"] = available
    state["details"] = details
    state["last_checked"] = now_text()

    save_state(state)

    print(
        f"[{now_text()}] SUMMARY: "
        f"available={available}, "
        f"notified="
        f"{state.get('ntfy_notified', False)}",
        flush=True,
    )

    return 0


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-ntfy",
        action="store_true",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.test_ntfy:
        send_test_ntfy()
        print(
            "ntfyテスト通知を送信しました。"
        )
        return 0

    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secret 縺梧悴險ｭ螳壹〒縺�: {name}")
    return value


def now_jst() -> datetime:
    return datetime.now(JST)


def now_text() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def in_maintenance() -> bool:
    t = now_jst().time().replace(tzinfo=None)
    return MAINTENANCE_START <= t < MAINTENANCE_END


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def current_path(page) -> str:
    try:
        return urllib.parse.urlsplit(page.url).path
    except Exception:
        return ""


def page_diag(page) -> str:
    try:
        title = page.title()
        body = norm(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return "page=unreadable"

    return (
        f"title={title!r}, path={current_path(page)!r}, "
        f"top={'繝医ャ繝励Γ繝九Η繝ｼ' in title or '繝医ャ繝励Γ繝九Η繝ｼ' in body}, "
        f"entry={'譌･譎ゅ�逋ｺ逹鬧�∈謚�' in title or '譌･譎ゅ�逋ｺ逹鬧�∈謚�' in body}, "
        f"route={'邨瑚ｷｯ繝ｻ險ｭ蛯咎∈謚�' in title or '邨瑚ｷｯ繝ｻ險ｭ蛯咎∈謚�' in body}, "
        f"change={'蛻苓ｻ翫�螟画峩' in title or '蛻苓ｻ翫�螟画峩' in body}"
    )


def is_guide_or_error(page) -> bool:
    title = page.title()
    body = norm(page.locator("body").inner_text(timeout=10000))
    return (
        "縺疲｡亥�" in title
        or "蜃ｦ逅�ｸｭ縺ｫ繧ｨ繝ｩ繝ｼ縺檎匱逕溘＠縺ｾ縺励◆" in body
        or "蜈･蜉帙�驕ｸ謚槭＠縺ｪ縺翫＠縺ｦ縺上□縺輔＞" in body
        or "繧｢繧ｯ繧ｻ繧ｹ縺碁寔荳ｭ" in body
        or "螟ｧ螟画ｷｷ縺ｿ蜷�" in body
    )


# ------------------------------------------------------------
# 迥ｶ諷倶ｿ晏ｭ�
# ------------------------------------------------------------

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


# ------------------------------------------------------------
# ntfy
# ------------------------------------------------------------

def ntfy_root_topic() -> tuple[str, str]:
    url = require_env("NTFY_TOPIC_URL")
    p = urllib.parse.urlsplit(url)
    topic = urllib.parse.unquote(p.path.strip("/"))

    if not p.scheme or not p.netloc or not topic:
        raise RuntimeError("NTFY_TOPIC_URL 縺ｮ蠖｢蠑上′荳肴ｭ｣縺ｧ縺�")

    root = urllib.parse.urlunsplit(
        (p.scheme, p.netloc, "/", "", "")
    )
    return root, topic


def publish_ntfy(payload: dict) -> None:
    root, topic = ntfy_root_topic()
    body = dict(payload)
    body["topic"] = topic

    req = urllib.request.Request(
        root,
        data=json.dumps(
            body,
            ensure_ascii=False,
        ).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8"
        },
    )

    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status >= 400:
            raise RuntimeError(
                f"ntfy HTTP {response.status}"
            )


def send_ntfy(stage: str, details: str) -> None:
    publish_ntfy({
        "title": "繧ｵ繝ｳ繝ｩ繧､繧ｺ蜃ｺ髮ｲ 遨ｺ蟶ｭ縺ゅｊ",
        "message": (
            f"{require_env('TRIP_LABEL')}\n"
            f"{stage}\n"
            f"{details}\n"
            "e5489繧堤｢ｺ隱阪＠縺ｦ縺上□縺輔＞縲�"
        ),
        "priority": 5,
        "tags": ["rotating_light", "train"],
        "click": TOP_URL,
    })


def send_test_ntfy() -> None:
    publish_ntfy({
        "title": "繧ｵ繝ｳ繝ｩ繧､繧ｺ逶｣隕� GitHub Actions 繝�せ繝�",
        "message": "譁ｰ隕丈ｺ育ｴ��繧ｿ繝ｳ邨檎罰迚医�ntfy繝�せ繝磯夂衍縺ｧ縺吶�",
        "priority": 4,
    })


# ------------------------------------------------------------
# 1. 繝医ャ繝� 竊� 譁ｰ隕丈ｺ育ｴ�
# ------------------------------------------------------------

def open_top_and_click_new_reservation(page) -> None:
    page.goto(
        TOP_URL,
        wait_until="domcontentloaded",
        timeout=45000,
    )
    page.wait_for_timeout(1200)

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "e5489繝医ャ繝励′縺疲｡亥�/繧ｨ繝ｩ繝ｼ"
        )

    # 豺ｻ莉倥＆繧後◆螳溘�繝ｼ繧ｸ縺ｧ縺ｯ縲�
    # <p class="new-home-index-navigation__ttl">譁ｰ隕丈ｺ育ｴ�</p>
    # 繧貞性繧<a>縺梧眠隕丈ｺ育ｴ��繧ｿ繝ｳ縲�
    new_text = page.get_by_text(
        "譁ｰ隕丈ｺ育ｴ�",
        exact=True,
    )

    clicked = False

    if new_text.count() > 0:
        try:
            anchor = new_text.first.locator(
                "xpath=ancestor::a[1]"
            )
            if anchor.count() > 0:
                anchor.click()
            else:
                new_text.first.click()

            clicked = True
            print(
                "  -> 繝医ャ繝礼判髱｢縺ｮ縲梧眠隕丈ｺ育ｴ�阪ｒ繧ｯ繝ｪ繝�け",
                flush=True,
            )
        except Exception:
            clicked = False

    # 繧ｵ繧､繝亥�JS縺ｮ繧ｯ繝ｪ繝�け蜃ｦ逅�′蜿悶ｌ縺ｪ縺��ｴ蜷医□縺代�
    # 豺ｻ莉路TML縺ｧ遒ｺ隱阪〒縺阪◆ formTrainSimpleEntry 繧単OST縺吶ｋ縲�
    # 蜀�ΚURL縺ｸ縺ｮGET逶ｴ繝ｪ繝ｳ繧ｯ縺ｯ縺励↑縺��
    if not clicked:
        form = page.locator(
            'form[name="formTrainSimpleEntry"]'
        )

        if form.count() == 0:
            raise TemporaryPageError(
                "繝医ャ繝礼判髱｢縺ｫ譁ｰ隕丈ｺ育ｴ��繧ｿ繝ｳ/繝輔か繝ｼ繝�縺瑚ｦ九▽縺九ｉ縺ｪ縺�"
            )

        form.evaluate(
            "form => form.submit()"
        )
        print(
            "  -> 譁ｰ隕丈ｺ育ｴ�ヵ繧ｩ繝ｼ繝�繧単OST騾∽ｿ｡",
            flush=True,
        )

    page.wait_for_timeout(1500)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "譁ｰ隕丈ｺ育ｴ�ｒ謚ｼ縺励◆蠕後↓縺疲｡亥�/繧ｨ繝ｩ繝ｼ"
        )

    print(
        f"  -> 譁ｰ隕丈ｺ育ｴ��遘ｻ蜈�: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 2. CBTrainSimpleEntrySP 竊� 縲碁ｧ�錐繧貞�蜉帙阪ち繝�
# ------------------------------------------------------------

def move_to_station_name_entry(page) -> None:
    # 縺吶〒縺ｫCBTrainEntrySP縺ｪ繧我ｽ輔ｂ縺励↑縺�
    if (
        page.locator(
            "#entry-departure-station"
        ).count() > 0
        and page.locator(
            "#entry-arrival-station"
        ).count() > 0
    ):
        return

    # SimpleEntry逕ｻ髱｢縺ｧ縲碁ｧ�錐繧貞�蜉帙阪ち繝悶ｒ謚ｼ縺�
    tab = page.get_by_text(
        "鬧�錐繧貞�蜉�",
        exact=True,
    )

    if tab.count() == 0:
        raise TemporaryPageError(
            "縲碁ｧ�錐繧貞�蜉帙阪ち繝悶′隕九▽縺九ｉ縺ｪ縺�"
        )

    tab.first.click()

    page.wait_for_timeout(1200)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=15000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "縲碁ｧ�錐繧貞�蜉帙阪ち繝夜�遘ｻ蠕後↓縺疲｡亥�/繧ｨ繝ｩ繝ｼ"
        )

    if (
        page.locator(
            "#entry-departure-station"
        ).count() == 0
        or page.locator(
            "#entry-arrival-station"
        ).count() == 0
    ):
        raise TemporaryPageError(
            "譌･譎ゅ�逋ｺ逹鬧�∈謚樒判髱｢繧堤｢ｺ隱阪〒縺阪↑縺�"
        )

    print(
        f"  -> 縲碁ｧ�錐繧貞�蜉帙阪∈遘ｻ蜍�: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 3. 讀懃ｴ｢譚｡莉ｶ
# 豺ｻ莉路TML縺九ｉname/id/value繧貞崋螳�
# ------------------------------------------------------------

def fill_search_conditions(page) -> None:
    depart = require_env("DEPART_STATION")
    arrive = require_env("ARRIVE_STATION")
    travel_date = require_env("TRAVEL_DATE").replace(
        "-", ""
    )
    hour = require_env("DEPART_HOUR").zfill(2)
    minute = require_env(
        "DEPART_MINUTE"
    ).zfill(2)

    page.locator(
        "#entry-departure-station"
    ).fill(depart)

    page.locator(
        "#entry-arrival-station"
    ).fill(arrive)

    page.locator(
        'select[name="inputDate"]'
    ).select_option(value=travel_date)

    page.locator(
        'select[name="inputHour"]'
    ).select_option(value=hour)

    page.locator(
        'select[name="inputMinute"]'
    ).select_option(value=minute)

    # 蜃ｺ逋ｺ
    page.locator(
        'input[name="inputType"][value="0"]'
    ).check()

    # 荳蠎ｦ繧ゆｹ励ｊ謠帙∴縺励↑縺�
    page.locator(
        'input[name="inputSearchType"][value="1"]'
    ).check()

    # 譁ｰ蟷ｹ邱唹FF
    shinkansen = page.locator(
        "#reserrve-shinkansen"
    )
    if shinkansen.is_checked():
        shinkansen.uncheck()

    # 迚ｹ諤･繝ｻ諤･陦鯉ｼ丞ｿｫ騾欅N
    limited = page.locator(
        "#reserrve-not-shinkansen"
    )
    if not limited.is_checked():
        limited.check()

    print(
        "  -> 譚ｱ莠ｬ竊貞�髮ｲ蟶� / 9譛�3譌･ / "
        "21:10 / 荵玲鋤縺ｪ縺� / 譁ｰ蟷ｹ邱唹FF",
        flush=True,
    )


def submit_search(page) -> None:
    button = page.locator(
        "button.decide-button"
    )

    if button.count() == 0:
        # 陦ｨ遉ｺ譁�ｭ励〒繧ゅヵ繧ｩ繝ｼ繝ｫ繝舌ャ繧ｯ
        button = page.get_by_text(
            "讀懃ｴ｢縺吶ｋ",
            exact=False,
        )

    if button.count() == 0:
        raise TemporaryPageError(
            "縲梧､懃ｴ｢縺吶ｋ�域眠隕丈ｺ育ｴ�ｼ峨阪�繧ｿ繝ｳ縺瑚ｦ九▽縺九ｉ縺ｪ縺�"
        )

    button.first.click()

    page.wait_for_timeout(1800)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "讀懃ｴ｢蠕後↓縺疲｡亥�/繧ｨ繝ｩ繝ｼ"
        )

    if page.locator(
        "table.seat-status-table"
    ).count() == 0:
        raise TemporaryPageError(
            "邨瑚ｷｯ繝ｻ險ｭ蛯咎∈謚槭�遨ｺ蟶ｭ陦ｨ縺瑚ｦ九▽縺九ｉ縺ｪ縺�"
        )

    print(
        f"  -> 讀懃ｴ｢邨先棡: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 4. 譛蛻昴�邨瑚ｷｯ繝ｻ險ｭ蛯咎∈謚�
#
# 蜃｡萓九�縲檎ｩｺ蟶ｭ縺ゅｊ縲阪梧ｮ句ｸｭ縺ｪ縺励阪ｒ隱､讀懷�縺励↑縺�ｈ縺�
# table.seat-status-table 蜀�□縺代ｒ隱ｭ繧縲�
# ------------------------------------------------------------

def read_initial_route_status(page) -> dict:
    table = page.locator(
        "table.seat-status-table"
    ).first

    rows = table.locator("tr")
    results = []

    for i in range(rows.count()):
        row = rows.nth(i)

        imgs = row.locator(
            'img[alt="遨ｺ蟶ｭ縺ゅｊ"], '
            'img[alt="遨ｺ蟶ｭ谿九ｊ繧上★縺�"], '
            'img[alt="谿句ｸｭ縺ｪ縺�"]'
        )

        if imgs.count() == 0:
            continue

        label = norm(
            row.inner_text()
        )

        alts = []
        for j in range(imgs.count()):
            alt = imgs.nth(j).get_attribute(
                "alt"
            )
            if alt:
                alts.append(alt)

        results.append({
            "label": label,
            "alts": alts,
        })

    if not results:
        raise TemporaryPageError(
            "譛蛻昴�遨ｺ蟶ｭ迥ｶ諷九ｒ蜿門ｾ励〒縺阪↑縺�"
        )

    positives = []
    total_marks = 0
    negative_marks = 0

    for item in results:
        for alt in item["alts"]:
            total_marks += 1
            if alt in POSITIVE_ALTS:
                positives.append(
                    f"{item['label']}:{alt}"
                )
            elif alt == NEGATIVE_ALT:
                negative_marks += 1

    return {
        "rows": results,
        "positives": positives,
        "all_negative": (
            total_marks > 0
            and negative_marks == total_marks
        ),
        "total": total_marks,
    }


# ------------------------------------------------------------
# 5. 縲後％縺ｮ蛻苓ｻ翫ｒ螟画峩縲坂�縲悟ｾ後�蛻苓ｻ翫�
# ------------------------------------------------------------

def open_later_trains(page) -> None:
    change = page.get_by_text(
        "縺薙�蛻苓ｻ翫ｒ螟画峩",
        exact=True,
    )

    if change.count() == 0:
        raise TemporaryPageError(
            "縲後％縺ｮ蛻苓ｻ翫ｒ螟画峩縲阪′隕九▽縺九ｉ縺ｪ縺�"
        )

    change.first.click()
    page.wait_for_timeout(500)

    later = page.locator(
        "button.change-next-train-button"
    )

    if later.count() == 0:
        later = page.get_by_text(
            "蠕後�蛻苓ｻ�",
            exact=True,
        )

    if later.count() == 0:
        raise TemporaryPageError(
            "縲悟ｾ後�蛻苓ｻ翫阪′隕九▽縺九ｉ縺ｪ縺�"
        )

    later.first.click()

    page.wait_for_timeout(1500)

    try:
        page.wait_for_load_state(
            "domcontentloaded",
            timeout=20000,
        )
    except Exception:
        pass

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "蠕後�蛻苓ｻ翫∈遘ｻ蜍募ｾ後↓縺疲｡亥�/繧ｨ繝ｩ繝ｼ"
        )

    if page.locator(
        'button[aria-controls="train-1"]'
    ).count() == 0:
        raise TemporaryPageError(
            "蛻苓ｻ雁､画峩逕ｻ髱｢縺ｮ蛟呵｣�1��3繧堤｢ｺ隱阪〒縺阪↑縺�"
        )

    print(
        f"  -> 蠕後�蛻苓ｻ�: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 6. train-1 / train-2 / train-3 縺ｮ�九ｒ髢九＞縺ｦ遨ｺ蟶ｭ遒ｺ隱�
# ------------------------------------------------------------

def candidate_name_from_li(li, fallback: str) -> str:
    txt = norm(li.inner_text())

    m = re.search(
        r"迚ｹ諤･繧ｵ繝ｳ繝ｩ繧､繧ｺ蜃ｺ髮ｲ��[^�云+��",
        txt,
    )

    if m:
        return m.group(0)

    return fallback


def read_three_later_trains(page) -> dict:
    positives = []
    details = []
    checked = 0

    for index in range(1, 4):
        panel_id = f"train-{index}"

        button = page.locator(
            f'button[aria-controls="{panel_id}"]'
        )

        panel = page.locator(
            f"#{panel_id}"
        )

        if button.count() == 0 or panel.count() == 0:
            raise TemporaryPageError(
                f"{panel_id} 縺瑚ｦ九▽縺九ｉ縺ｪ縺�"
            )

        # �具ｼ磯幕髢峨☆繧具ｼ峨ｒ謚ｼ縺吶�
        # 譌｢縺ｫ髢九＞縺ｦ縺�ｋ蝣ｴ蜷医�縺昴�縺ｾ縺ｾ隱ｭ繧縲�
        expanded = (
            button.first.get_attribute(
                "aria-expanded"
            )
            or ""
        ).lower()

        if expanded != "true":
            button.first.click()
            page.wait_for_timeout(350)

        li = panel.first.locator(
            "xpath=ancestor::li[1]"
        )

        if li.count() > 0:
            name = candidate_name_from_li(
                li.first,
                panel_id,
            )
        else:
            name = panel_id

        imgs = panel.first.locator(
            'img[alt="遨ｺ蟶ｭ縺ゅｊ"], '
            'img[alt="遨ｺ蟶ｭ谿九ｊ繧上★縺�"], '
            'img[alt="谿句ｸｭ縺ｪ縺�"]'
        )

        if imgs.count() == 0:
            raise TemporaryPageError(
                f"{name} 縺ｮ遨ｺ蟶ｭ逕ｻ蜒上ｒ蜿門ｾ励〒縺阪↑縺�"
            )

        alts = []

        for j in range(imgs.count()):
            alt = imgs.nth(j).get_attribute(
                "alt"
            )
            if alt:
                alts.append(alt)

        checked += 1
        details.append(
            f"{name}:{'/'.join(alts)}"
        )

        for alt in alts:
            if alt in POSITIVE_ALTS:
                positives.append(
                    f"{name}:{alt}"
                )

        print(
            f"  -> {name}: {' / '.join(alts)}",
            flush=True,
        )

    return {
        "checked": checked,
        "details": details,
        "positives": positives,
    }


# ------------------------------------------------------------
# 1蟾｡蝗�
# ------------------------------------------------------------

def perform_check(page) -> tuple[bool, str]:
    # 蠢�★繝医ャ繝励°繧画ｭ｣隕城�遘ｻ
    open_top_and_click_new_reservation(page)

    # SimpleEntry 竊� 鬧�錐蜈･蜉�
    move_to_station_name_entry(page)

    # 譚｡莉ｶ蜈･蜉�
    fill_search_conditions(page)
    submit_search(page)

    # 譛蛻昴�讀懃ｴ｢邨先棡
    initial = read_initial_route_status(
        page
    )

    print(
        f"  -> 蛻晏屓遨ｺ蟶ｭ繝槭�繧ｯ謨ｰ={initial['total']}, "
        f"遨ｺ蟶ｭ縺ゅｊ={len(initial['positives'])}",
        flush=True,
    )

    if initial["positives"]:
        return (
            True,
            "蛻晏屓讀懃ｴ｢: "
            + " / ".join(
                initial["positives"]
            ),
        )

    if not initial["all_negative"]:
        raise TemporaryPageError(
            "蛻晏屓邨先棡縺後後☆縺ｹ縺ｦ谿句ｸｭ縺ｪ縺励阪〒縺ｯ縺ｪ縺上�"
            "笳�/笆ｳ繧ゅ↑縺�◆繧∝愛螳壻ｿ晉蕗"
        )

    print(
        "  -> 蛻晏屓縺ｯ縺吶∋縺ｦ谿句ｸｭ縺ｪ縺励�"
        "縲後％縺ｮ蛻苓ｻ翫ｒ螟画峩縲坂�縲悟ｾ後�蛻苓ｻ翫阪∈",
        flush=True,
    )

    # 蠕後�蛻苓ｻ�
    open_later_trains(page)

    later = read_three_later_trains(
        page
    )

    if later["positives"]:
        return (
            True,
            "蠕後�蛻苓ｻ�: "
            + " / ".join(
                later["positives"]
            ),
        )

    return (
        False,
        "蛻晏屓縺ｯ蜈ｨ縺ｦ谿句ｸｭ縺ｪ縺励ょｾ後�蛻苓ｻ�3蛟呵｣懊ｂ笳�/笆ｳ縺ｪ縺励� "
        + " / ".join(later["details"]),
    )


def run_once() -> int:
    if in_maintenance():
        print(
            f"[{now_text()}] 01:30-05:30 JST 縺ｯ逶｣隕門●豁｢縲�"
            "e5489縺ｫ縺ｯ繧｢繧ｯ繧ｻ繧ｹ縺励∪縺帙ｓ縲�",
            flush=True,
        )
        return 0

    state = load_state()
    was_notified = bool(
        state.get(
            "ntfy_notified",
            False,
        )
    )

    available = None
    details = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={
                "width": 430,
                "height": 1600,
            },
            user_agent=(
                "Mozilla/5.0 "
                "(Linux; Android 13; Pixel 7) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0.0.0 "
                "Mobile Safari/537.36"
            ),
        )

        page = context.new_page()

        try:
            for attempt in range(
                1,
                MAX_ATTEMPTS + 1,
            ):
                try:
                    print(
                        f"[{now_text()}] "
                        f"CHECK {attempt}/{MAX_ATTEMPTS}",
                        flush=True,
                    )

                    available, details = (
                        perform_check(page)
                    )
                    break

                except Exception as exc:
                    print(
                        f"  -> 遒ｺ隱堺ｸ崎�: "
                        f"{type(exc).__name__}: {exc}; "
                        f"{page_diag(page)}",
                        file=sys.stderr,
                        flush=True,
                    )

                    if attempt < MAX_ATTEMPTS:
                        print(
                            f"  -> {RETRY_WAIT_SECONDS}遘貞ｾ後↓"
                            "繝医ャ繝励°繧峨ｄ繧顔峩縺励∪縺�",
                            flush=True,
                        )
                        time.sleep(
                            RETRY_WAIT_SECONDS
                        )

        finally:
            page.close()
            context.close()
            browser.close()

    if available is None:
        print(
            f"[{now_text()}] SUMMARY: 莉雁屓遒ｺ隱堺ｸ崎�縲�"
            "蜑榊屓迥ｶ諷九�螟画峩縺励∪縺帙ｓ縲�",
            flush=True,
        )
        return 0

    if available:
        print(
            f"[{now_text()}] 遨ｺ蟶ｭ讀懷�: {details}",
            flush=True,
        )

        if not was_notified:
            try:
                stage = (
                    "蠕後�蛻苓ｻ翫〒遨ｺ蟶ｭ繧呈､懷�"
                    if details.startswith(
                        "蠕後�蛻苓ｻ�"
                    )
                    else "譛蛻昴�讀懃ｴ｢邨先棡縺ｧ遨ｺ蟶ｭ繧呈､懷�"
                )

                send_ntfy(
                    stage,
                    details,
                )

                state[
                    "ntfy_notified"
                ] = True

                print(
                    "  -> ntfy騾夂衍騾∽ｿ｡",
                    flush=True,
                )

            except Exception as exc:
                print(
                    f"  -> ntfy騾∽ｿ｡螟ｱ謨� "
                    f"({type(exc).__name__})縲�"
                    "谺｡蝗槫�騾√＠縺ｾ縺吶�",
                    file=sys.stderr,
                    flush=True,
                )

                state[
                    "ntfy_notified"
                ] = False

    else:
        print(
            f"[{now_text()}] 遨ｺ蟶ｭ縺ｪ縺�: {details}",
            flush=True,
        )

        # ﾃ励∈謌ｻ縺｣縺溘ｉ縲∵ｬ｡縺ｫ笳�/笆ｳ縺悟�縺滄圀縺ｫ蜀埼夂衍
        state[
            "ntfy_notified"
        ] = False

    state["available"] = available
    state["details"] = details
    state["last_checked"] = now_text()

    save_state(state)

    print(
        f"[{now_text()}] SUMMARY: "
        f"available={available}, "
        f"notified="
        f"{state.get('ntfy_notified', False)}",
        flush=True,
    )

    return 0


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-ntfy",
        action="store_true",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.test_ntfy:
        send_test_ntfy()
        print(
            "ntfy繝�せ繝磯夂衍繧帝∽ｿ｡縺励∪縺励◆縲�"
        )
        return 0

    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
def now_jst() -> datetime:
    return datetime.now(JST)


def now_text() -> str:
    return now_jst().strftime("%Y-%m-%d %H:%M:%S JST")


def in_maintenance() -> bool:
    t = now_jst().time().replace(tzinfo=None)
    return MAINTENANCE_START <= t < MAINTENANCE_END


def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


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
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# ---------- ntfy ----------

def ntfy_root_topic() -> tuple[str, str]:
    url = require_env("NTFY_TOPIC_URL")
    p = urllib.parse.urlsplit(url)
    topic = urllib.parse.unquote(p.path.strip("/"))
    if not p.scheme or not p.netloc or not topic:
        raise RuntimeError("NTFY_TOPIC_URL の形式が不正です")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/", "", "")), topic


def publish_ntfy(payload: dict) -> None:
    root, topic = ntfy_root_topic()
    body = dict(payload)
    body["topic"] = topic
    req = urllib.request.Request(
        root,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        if r.status >= 400:
            raise RuntimeError(f"ntfy HTTP {r.status}")


def send_ntfy(stage: str, details: str) -> None:
    publish_ntfy({
        "title": "サンライズ出雲 空席あり",
        "message": f"{require_env('TRIP_LABEL')}\n{stage}\n{details}\ne5489を確認してください。",
        "priority": 5,
        "tags": ["rotating_light", "train"],
        "click": ENTRY_URL,
    })


def send_test_ntfy() -> None:
    publish_ntfy({
        "title": "サンライズ監視 GitHub Actions テスト",
        "message": "検索フォーム版からのntfyテスト通知です。",
        "priority": 4,
    })


# ---------- ページ操作 ----------

def page_diag(page) -> str:
    try:
        title = page.title()
        body = norm(page.locator("body").inner_text(timeout=5000))
    except Exception:
        return "page=unreadable"
    return (
        f"title={title!r}, entry={'日時・発着駅選択' in body}, "
        f"route={'経路・設備選択' in body or '経路・設備選択' in title}, "
        f"sunrise={'サンライズ' in body}, change={'この列車を変更' in body}"
    )


def is_guide_or_error(page) -> bool:
    title = page.title()
    body = norm(page.locator("body").inner_text(timeout=10000))
    return (
        "ご案内" in title
        or "処理中にエラーが発生しました" in body
        or "入力・選択しなおしてください" in body
        or "アクセスが集中" in body
        or "大変混み合" in body
    )


def goto_entry(page) -> None:
    # 直リンクだけだとセッション無しの「ご案内」になることがあるので先にトップを開く。
    page.goto(TOP_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(800)
    page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1200)
    if is_guide_or_error(page):
        raise TemporaryPageError("検索入力画面でご案内/エラー")
    body = norm(page.locator("body").inner_text(timeout=10000))
    if "日時・発着駅選択" not in body and "発着駅の指定" not in body:
        raise TemporaryPageError("検索入力画面を確認できない")


def fill_stations(page, depart: str, arrive: str) -> None:
    inputs = page.locator('input[type="text"]:visible, input:not([type]):visible')
    if inputs.count() < 2:
        raise RuntimeError("発駅・着駅入力欄が見つかりません")
    inputs.nth(0).fill(depart)
    inputs.nth(1).fill(arrive)


def select_option_matching(page, regexes: list[str]) -> str:
    selects = page.locator("select:visible")
    for i in range(selects.count()):
        sel = selects.nth(i)
        opts = sel.locator("option")
        for j in range(opts.count()):
            op = opts.nth(j)
            label = norm(op.inner_text())
            if any(re.search(rx, label) for rx in regexes):
                value = op.get_attribute("value")
                if value is not None:
                    sel.select_option(value=value)
                else:
                    sel.select_option(label=label)
                return label
    raise RuntimeError(f"select候補が見つかりません: {regexes}")


def set_control(page, typ: str, text: str, desired: bool) -> None:
    ok = page.evaluate(
        """
        ({typ,text,desired}) => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim();
          for (const el of document.querySelectorAll(`input[type="${typ}"]`)) {
            if (el.disabled) continue;
            let s='';
            if (el.id) {
              const lab=document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
              if (lab) s+=' '+norm(lab.innerText||lab.textContent);
            }
            const pl=el.closest('label');
            if (pl) s+=' '+norm(pl.innerText||pl.textContent);
            let p=el.parentElement;
            for(let i=0;i<4&&p;i++,p=p.parentElement) s+=' '+norm(p.innerText||p.textContent);
            if (norm(s).includes(text)) {
              if (el.checked!==desired) el.click();
              if (el.checked!==desired) {
                el.checked=desired;
                el.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return el.checked===desired;
            }
          }
          return false;
        }
        """,
        {"typ": typ, "text": text, "desired": desired},
    )
    if not ok:
        raise RuntimeError(f"操作項目を設定できません: {text}")


def click_text(page, text: str) -> None:
    els = page.locator('button:visible,a:visible,input[type="button"]:visible,input[type="submit"]:visible')
    for i in range(els.count()):
        el = els.nth(i)
        label = norm(
            el.get_attribute("value")
            or el.get_attribute("aria-label")
            or el.get_attribute("title")
            or el.inner_text()
        )
        if text in label:
            el.click()
            return
    txt = page.get_by_text(text, exact=False)
    if txt.count():
        txt.first.click()
        return
    raise RuntimeError(f"クリック対象が見つかりません: {text}")


def fill_search_form(page) -> None:
    depart = require_env("DEPART_STATION")
    arrive = require_env("ARRIVE_STATION")
    dt = datetime.strptime(require_env("TRAVEL_DATE"), "%Y-%m-%d")
    hour = int(require_env("DEPART_HOUR"))
    minute = int(require_env("DEPART_MINUTE"))

    fill_stations(page, depart, arrive)
    d = select_option_matching(page, [rf"{dt.month}\s*月\s*{dt.day}\s*日"])
    h = select_option_matching(page, [rf"^{hour}\s*時$", rf"^{hour}$"])
    m = select_option_matching(page, [rf"^{minute:02d}\s*分$", rf"^{minute}\s*分$", rf"^{minute:02d}$", rf"^{minute}$"])

    set_control(page, "radio", "出発", True)
    set_control(page, "radio", "一度も乗り換えしない", True)
    set_control(page, "checkbox", "新幹線を利用", False)
    set_control(page, "checkbox", "特急・急行", True)
    print(f"  -> 条件設定: {d} {h}{m}", flush=True)


def submit_search(page) -> None:
    click_text(page, "検索する")
    page.wait_for_timeout(1800)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    if is_guide_or_error(page):
        raise TemporaryPageError("検索後にご案内/エラー")
    body = norm(page.locator("body").inner_text(timeout=10000))
    if "経路・設備選択" not in body and "サンライズ" not in body:
        raise TemporaryPageError("検索結果を確認できない")


# ---------- 空席 ----------

def scan_availability(page) -> dict:
    return page.evaluate(
        """
        () => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim();
          const vis=el=>{const c=getComputedStyle(el),r=el.getBoundingClientRect();return c.display!=='none'&&c.visibility!=='hidden'&&r.width>0&&r.height>0};
          const pos=[]; let x=0; let neg=false;
          for(const el of document.querySelectorAll('*')){
            if(!vis(el)) continue;
            const vals=[];
            for(const n of el.childNodes) if(n.nodeType===Node.TEXT_NODE&&norm(n.textContent)) vals.push(norm(n.textContent));
            for(const a of ['alt','title','aria-label','value']){const v=el.getAttribute&&el.getAttribute(a);if(v) vals.push(norm(v));}
            for(const t of vals){
              if(t==='○'||t==='〇'||t==='△'||t.includes('空席あり')||t.includes('残りわずか')) pos.push(t);
              if(t.includes('満席')||t.includes('選択不可')) neg=true;
              if(t==='×'||t==='✕'||t==='✖') x++;
            }
          }
          const body=norm(document.body.innerText||document.body.textContent||'');
          if(body.includes('この経路は選択できません')||body.includes('選択不可')) neg=true;
          return {positive:[...new Set(pos)].slice(0,20), xCount:x, negative:neg};
        }
        """
    )


def positive_details(a: dict) -> str:
    return " / ".join(a.get("positive") or [])


def definitely_no_space(page, a: dict) -> bool:
    if a.get("positive"):
        return False
    if a.get("negative"):
        return True
    body = norm(page.locator("body").inner_text(timeout=10000))
    return ("サンライズ" in body and int(a.get("xCount", 0)) > 0)


def click_change_and_later(page) -> None:
    click_text(page, "この列車を変更")
    page.wait_for_timeout(700)
    click_text(page, "後の列車")
    page.wait_for_timeout(1500)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=20000)
    except Exception:
        pass
    if is_guide_or_error(page):
        raise TemporaryPageError("後の列車でご案内/エラー")


def expand_three_plus(page) -> int:
    clicked = 0
    for _ in range(3):
        els = page.locator('button:visible,a:visible,input[type="button"]:visible,input[type="submit"]:visible')
        choice = None
        for i in range(els.count()):
            el = els.nth(i)
            info = el.evaluate(
                """
                el=>{const n=s=>(s||'').replace(/\s+/g,' ').trim();let p=el,s='';for(let i=0;i<5&&p;i++,p=p.parentElement)s+=' '+n(p.innerText||p.textContent);const r=el.getBoundingClientRect();return{label:n(el.innerText||el.value||el.getAttribute('aria-label')||el.getAttribute('title')||''),context:n(s).slice(0,900),y:Math.round(r.y)}}
                """
            )
            lab, ctx = info["label"], info["context"]
            is_plus = lab in {"+", "＋"} or "開く" in lab or "展開" in lab or "詳細" in lab
            if not is_plus:
                continue
            if re.search(r"検索条件|ご案内|凡例|記号の説明", ctx):
                continue
            if not re.search(r"サンライズ|特急|発|着|\d{1,2}:\d{2}", ctx):
                continue
            choice = el
            break
        if choice is None:
            break
        choice.click()
        clicked += 1
        page.wait_for_timeout(500)
    return clicked


def perform_check(page) -> tuple[bool, str]:
    goto_entry(page)
    fill_search_form(page)
    submit_search(page)

    first = scan_availability(page)
    print(f"  -> 初回: positive={len(first['positive'])}, x={first['xCount']}, negative={first['negative']}", flush=True)
    if first["positive"]:
        return True, f"初回検索: {positive_details(first)}"
    if not definitely_no_space(page, first):
        raise TemporaryPageError("初回結果を空席あり/なしに確定できない")

    print("  -> 初回は空席なし。後の列車へ", flush=True)
    click_change_and_later(page)
    opened = expand_three_plus(page)
    print(f"  -> 後の列車: ＋を{opened}個展開", flush=True)

    later = scan_availability(page)
    print(f"  -> 後の列車: positive={len(later['positive'])}, x={later['xCount']}, negative={later['negative']}", flush=True)
    if later["positive"]:
        return True, f"後の列車（＋{opened}個）: {positive_details(later)}"
    return False, f"初回×、後の列車＋{opened}個を確認、○/△なし"


def run_once() -> int:
    if in_maintenance():
        print(f"[{now_text()}] 01:30-05:30 JST は監視停止。e5489にはアクセスしません。", flush=True)
        return 0

    state = load_state()
    was_notified = bool(state.get("ntfy_notified", False))
    available = None
    details = ""

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="ja-JP", timezone_id="Asia/Tokyo", viewport={"width":1280,"height":1800})
        page = context.new_page()
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    print(f"[{now_text()}] CHECK {attempt}/{MAX_ATTEMPTS}", flush=True)
                    available, details = perform_check(page)
                    break
                except Exception as exc:
                    print(f"  -> 確認不能: {type(exc).__name__}: {exc}; {page_diag(page)}", file=sys.stderr, flush=True)
                    if attempt < MAX_ATTEMPTS:
                        print(f"  -> {RETRY_WAIT_SECONDS}秒後に巡回全体を再試行", flush=True)
                        time.sleep(RETRY_WAIT_SECONDS)
        finally:
            page.close(); context.close(); browser.close()

    if available is None:
        print(f"[{now_text()}] SUMMARY: 今回確認不能。状態は変更しません。", flush=True)
        return 0

    if available:
        print(f"[{now_text()}] 空席検出: {details}", flush=True)
        if not was_notified:
            try:
                stage = "後の列車で空席を検出" if details.startswith("後の列車") else "最初の検索結果で空席を検出"
                send_ntfy(stage, details)
                print("  -> ntfy通知送信", flush=True)
                state["ntfy_notified"] = True
            except Exception as exc:
                print(f"  -> ntfy送信失敗 ({type(exc).__name__})。次回再送します。", file=sys.stderr, flush=True)
                state["ntfy_notified"] = False
    else:
        print(f"[{now_text()}] 空席なし: {details}", flush=True)
        state["ntfy_notified"] = False

    state["available"] = available
    state["details"] = details
    state["last_checked"] = now_text()
    save_state(state)
    print(f"[{now_text()}] SUMMARY: available={available}, notified={state.get('ntfy_notified', False)}", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--test-ntfy", action="store_true")
    args = p.parse_args()
    if args.test_ntfy:
        send_test_ntfy(); print("ntfyテスト通知を送信しました。"); return 0
    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
