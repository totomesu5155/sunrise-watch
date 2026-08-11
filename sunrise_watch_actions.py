from __future__ import annotations

import argparse
import json
import os
import random
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

# e5489は同じ条件でも一時的な「ご案内」を返すことがあるため、
# 1巡回内で複数回試す。ただし5分周期を圧迫しないよう約3分半で打ち切る。
MAX_ATTEMPTS = max(1, int(os.getenv("SUNRISE_MAX_ATTEMPTS", "2")))
RETRY_BACKOFF_SECONDS = (25, 45, 75)
MAX_RUN_SECONDS = 240

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
    """
    CBTrainSimpleEntrySP と CBTrainEntrySP では同じ id
    #entry-departure-station / #entry-arrival-station が使われている。

    SimpleEntry側: <select>
    Entry側:       <input type="text">

    したがって「idが存在する」だけでは判定せず、tagNameまで確認する。
    SimpleEntryでは formTrainEntry をPOSTして「駅名を入力」画面へ移る。
    """
    dep = page.locator("#entry-departure-station")
    arr = page.locator("#entry-arrival-station")

    if dep.count() == 0 or arr.count() == 0:
        raise TemporaryPageError(
            "発駅・着駅の要素が見つからない"
        )

    dep_tag = dep.first.evaluate(
        "el => el.tagName.toLowerCase()"
    )
    arr_tag = arr.first.evaluate(
        "el => el.tagName.toLowerCase()"
    )

    # すでに「駅名を入力」画面（CBTrainEntrySP）
    if dep_tag == "input" and arr_tag == "input":
        print(
            f"  -> 駅名入力画面を確認: {current_path(page)}",
            flush=True,
        )
        return

    # 「新幹線かんたん予約」側では両方select。
    if dep_tag != "select" or arr_tag != "select":
        raise TemporaryPageError(
            f"発着駅要素の形式が想定外: depart={dep_tag}, arrive={arr_tag}"
        )

    # 保存HTMLで確認できた正規のPOSTフォーム。
    form = page.locator('form[name="formTrainEntry"]')

    if form.count() == 0:
        raise TemporaryPageError(
            "駅名入力へ移る formTrainEntry が見つからない"
        )

    print(
        "  -> 「駅名を入力」へ正規POST遷移",
        flush=True,
    )

    # 内部URLへのGET直アクセスはしない。
    # 現在のセッションを保ったまま formTrainEntry をPOSTする。
    try:
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=20000,
        ):
            form.first.evaluate("form => form.submit()")
    except Exception:
        # navigationイベントを取り逃した場合も、実際に遷移済みか確認する。
        page.wait_for_timeout(1200)

    if is_guide_or_error(page):
        raise TemporaryPageError(
            "駅名入力画面へのPOST後にご案内/エラー"
        )

    dep = page.locator("#entry-departure-station")
    arr = page.locator("#entry-arrival-station")

    if dep.count() == 0 or arr.count() == 0:
        raise TemporaryPageError(
            "日時・発着駅選択画面の発着駅入力欄が見つからない"
        )

    dep_tag = dep.first.evaluate(
        "el => el.tagName.toLowerCase()"
    )
    arr_tag = arr.first.evaluate(
        "el => el.tagName.toLowerCase()"
    )

    if dep_tag != "input" or arr_tag != "input":
        raise TemporaryPageError(
            f"駅名入力画面への遷移失敗: depart={dep_tag}, arrive={arr_tag}, "
            f"path={current_path(page)}"
        )

    print(
        f"  -> 「駅名を入力」へ移動成功: {current_path(page)}",
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

    dep = page.locator("#entry-departure-station")
    arr = page.locator("#entry-arrival-station")

    dep_tag = dep.first.evaluate("el => el.tagName.toLowerCase()")
    arr_tag = arr.first.evaluate("el => el.tagName.toLowerCase()")

    if dep_tag != "input" or arr_tag != "input":
        raise TemporaryPageError(
            f"駅名入力欄ではありません: depart={dep_tag}, arrive={arr_tag}"
        )

    dep.fill(depart)
    arr.fill(arrive)

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

    # 「乗り換え設定」は初期状態で折りたたまれており、
    # inputSearchType はDOM上に存在していても非表示。
    # 先に実画面の開閉ボタンを押してからチェックする。
    search_toggle = page.locator(
        'button[aria-controls="search-method"]'
    )

    if search_toggle.count() == 0:
        raise TemporaryPageError(
            "乗り換え設定の開閉ボタンが見つかりません"
        )

    expanded = (
        search_toggle.first.get_attribute("aria-expanded")
        or ""
    ).lower()

    if expanded != "true":
        search_toggle.first.click()
        page.wait_for_timeout(300)

    # 一度も乗り換えしない
    no_transfer = page.locator(
        'input[name="inputSearchType"][value="1"]'
    )

    if no_transfer.count() == 0:
        raise TemporaryPageError(
            "「一度も乗り換えしない」のラジオボタンが見つかりません"
        )

    # 展開後に通常のcheckを行う。
    # サイト側のclick/changeイベントも発火させる。
    try:
        no_transfer.check(timeout=5000)
    except Exception:
        # CSSでinput自体を不可視にしている場合はlabelをクリック。
        label = no_transfer.locator("xpath=ancestor::label[1]")
        if label.count() == 0:
            raise
        label.click(force=True)

    if not no_transfer.is_checked():
        raise TemporaryPageError(
            "「一度も乗り換えしない」を選択できませんでした"
        )

    # 「一度も乗り換えしない」を選ぶと、
    # #without-connection（利用列車）が表示される。
    # 実HTMLでは checkbox 自体の上に <span> が重なっているため、
    # Playwrightの uncheck()/check() だと span にクリックを遮られる。
    # そこで実際の画面操作と同じように label をクリックする。
    train_section = page.locator("#without-connection")

    if train_section.count() == 0:
        raise TemporaryPageError(
            "「利用列車」セクションが見つかりません"
        )

    # aria-hidden が false になるまで少し待つ
    try:
        page.wait_for_function(
            """() => {
                const el = document.querySelector('#without-connection');
                return el && el.getAttribute('aria-hidden') === 'false';
            }""",
            timeout=5000,
        )
    except Exception:
        pass

    # 新幹線OFF
    shinkansen = page.locator("#reserrve-shinkansen")
    if shinkansen.count() == 0:
        raise TemporaryPageError(
            "「新幹線を利用」のチェックボックスが見つかりません"
        )

    if shinkansen.is_checked():
        shinkansen.locator("xpath=ancestor::label[1]").click(force=True)
        page.wait_for_timeout(200)

    if shinkansen.is_checked():
        raise TemporaryPageError(
            "「新幹線を利用」をOFFにできませんでした"
        )

    # 特急・急行／快速ON
    limited = page.locator("#reserrve-not-shinkansen")
    if limited.count() == 0:
        raise TemporaryPageError(
            "「特急・急行／快速を利用」のチェックボックスが見つかりません"
        )

    if not limited.is_checked():
        limited.locator("xpath=ancestor::label[1]").click(force=True)
        page.wait_for_timeout(200)

    if not limited.is_checked():
        raise TemporaryPageError(
            "「特急・急行／快速を利用」をONにできませんでした"
        )

    print(
        "  -> 詳細検索を展開"
        " / 一度も乗り換えしない"
        " / 新幹線OFF"
        " / 特急・急行／快速ON",
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
    """
    「この列車を変更」→「後の列車」はサイト本来のボタンをそのままクリックする。

    ここでは勝手にCBChangeTrainSPへPOSTしない。
    代わりに、実際にブラウザが送った e5489 内の遷移だけを安全にログへ出す。
    URLのquery値やPOST値そのものは表示せず、pathとPOST項目名だけを表示する。
    """
    change = page.get_by_text(
        "この列車を変更",
        exact=True,
    )

    if change.count() == 0:
        raise TemporaryPageError(
            "「この列車を変更」が見つからない"
        )

    change.first.click()

    # ポップアップが開くまで待つ
    try:
        page.wait_for_function(
            """() => {
                const p = document.querySelector(
                    'section[id^="change-train-"][role="dialog"]'
                );
                return p && p.getAttribute('aria-hidden') === 'false';
            }""",
            timeout=5000,
        )
    except Exception:
        pass

    later = page.locator(
        "button.change-next-train-button"
    )

    if later.count() == 0:
        raise TemporaryPageError(
            "「後の列車」ボタンが見つからない"
        )

    print(
        "  -> 「後の列車」をサイト本来のボタンでクリック",
        flush=True,
    )

    network_events = []

    def on_request(request):
        try:
            p = urllib.parse.urlsplit(request.url)
            if not (
                p.netloc == "e5489.jr-odekake.net"
                and p.path.startswith("/e5489/cssp/")
            ):
                return

            field_names = []
            post_data = request.post_data or ""

            if post_data:
                try:
                    field_names = sorted({
                        k for k, _ in urllib.parse.parse_qsl(
                            post_data,
                            keep_blank_values=True,
                        )
                    })
                except Exception:
                    field_names = ["(parse-failed)"]

            network_events.append(
                (
                    "REQ",
                    request.method,
                    p.path,
                    field_names,
                )
            )
        except Exception:
            pass

    def on_response(response):
        try:
            p = urllib.parse.urlsplit(response.url)
            if not (
                p.netloc == "e5489.jr-odekake.net"
                and p.path.startswith("/e5489/cssp/")
            ):
                return

            location = response.headers.get(
                "location",
                "",
            )

            location_path = ""
            if location:
                try:
                    location_path = urllib.parse.urlsplit(
                        urllib.parse.urljoin(
                            response.url,
                            location,
                        )
                    ).path
                except Exception:
                    location_path = "(unparsed)"

            network_events.append(
                (
                    "RES",
                    response.status,
                    p.path,
                    location_path,
                )
            )
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    try:
        try:
            with page.expect_navigation(
                wait_until="domcontentloaded",
                timeout=20000,
            ):
                later.first.click()
        except Exception:
            # redirect連鎖やnavigationイベント取り逃しに備える
            page.wait_for_timeout(1500)

        # redirect後の最終ページが落ち着くまで少し待つ
        page.wait_for_timeout(1000)

    finally:
        try:
            page.remove_listener(
                "request",
                on_request,
            )
            page.remove_listener(
                "response",
                on_response,
            )
        except Exception:
            pass

    # 公開ログに出してよい情報のみ表示
    for event in network_events:
        if event[0] == "REQ":
            _, method, path, fields = event
            print(
                f"     NET REQ {method} {path} "
                f"fields={fields}",
                flush=True,
            )
        else:
            _, status, path, location_path = event
            suffix = (
                f" -> {location_path}"
                if location_path
                else ""
            )
            print(
                f"     NET RES {status} {path}{suffix}",
                flush=True,
            )

    if is_guide_or_error(page):
        try:
            body = norm(
                page.locator("body").inner_text(
                    timeout=5000
                )
            )
            codes = re.findall(
                r"\b\d{6,10}\b",
                body,
            )
            code_text = (
                ",".join(codes[:3])
                if codes
                else "none"
            )
        except Exception:
            code_text = "unknown"

        raise TemporaryPageError(
            "後の列車クリック後にご案内/エラー "
            f"(code={code_text})"
        )

    if page.locator(
        'button[aria-controls="train-1"]'
    ).count() == 0:
        raise TemporaryPageError(
            "列車変更画面の候補1～3を確認できない"
        )

    print(
        f"  -> 後の列車へ移動成功: {current_path(page)}",
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
    errors: list[str] = []
    run_started = time.monotonic()
    attempts_used = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True
        )

        # Playwright公式Samsung端末プロファイル。
        device = pw.devices["Galaxy S24"]

        try:
            for attempt in range(
                1,
                MAX_ATTEMPTS + 1,
            ):
                attempts_used = attempt

                # 重要:
                # e5489の「ご案内」後に同じCookie/セッションを使い回さない。
                # 毎回まっさらなBrowserContextを作る。
                context = browser.new_context(
                    **device,
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                )
                page = context.new_page()

                try:
                    print(
                        f"[{now_text()}] "
                        f"CHECK {attempt}/{MAX_ATTEMPTS} "
                        "(新しいセッション)",
                        flush=True,
                    )

                    available, details = (
                        perform_check(page)
                    )

                    print(
                        f"  -> CHECK {attempt} 完遂",
                        flush=True,
                    )
                    break

                except Exception as exc:
                    error_text = (
                        f"{type(exc).__name__}: {exc}"
                    )
                    errors.append(error_text)

                    print(
                        f"  -> 確認不能: "
                        f"{error_text}; "
                        f"{page_diag(page)}",
                        file=sys.stderr,
                        flush=True,
                    )

                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

                    try:
                        context.close()
                    except Exception:
                        pass

                if attempt >= MAX_ATTEMPTS:
                    break

                elapsed = time.monotonic() - run_started
                base_wait = RETRY_BACKOFF_SECONDS[
                    min(
                        attempt - 1,
                        len(RETRY_BACKOFF_SECONDS) - 1,
                    )
                ]

                # 全runが同じ秒数で再アクセスしないよう少しだけ揺らす。
                wait_seconds = (
                    base_wait
                    + random.uniform(0, 5)
                )

                # GitHub Actionsが次の5分周期へ食い込まないよう、
                # 約3分半を超えそうならこの巡回は終了。
                if (
                    elapsed
                    + wait_seconds
                    >= MAX_RUN_SECONDS
                ):
                    print(
                        "  -> この巡回の再試行時間上限に"
                        "近づいたため、次回定期実行に回します",
                        flush=True,
                    )
                    break

                print(
                    f"  -> セッションを破棄しました。"
                    f"{wait_seconds:.0f}秒後に"
                    "トップから新しいセッションで再試行します",
                    flush=True,
                )
                time.sleep(wait_seconds)

        finally:
            browser.close()

    if available is None:
        print(
            f"[{now_text()}] SUMMARY: 今回確認不能。"
            f"attempts={attempts_used}/{MAX_ATTEMPTS}。"
            "前回状態は変更しません。",
            flush=True,
        )

        if errors:
            print(
                "  -> 今回の失敗: "
                + " | ".join(errors),
                flush=True,
            )

        if os.getenv("SUNRISE_SIGNAL_EXIT", "0") == "1":
            return 2
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
        f"{state.get('ntfy_notified', False)}, "
        f"attempts={attempts_used}",
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
