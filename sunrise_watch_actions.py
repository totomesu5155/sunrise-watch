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

SCRIPT_VERSION = "2026-08-11-mobile-table-v2"

# JR側にアクセスしない時間
MAINTENANCE_START = dt_time(1, 30)
MAINTENANCE_END = dt_time(5, 30)

# 必ずトップ画面から入り、「新規予約」を押す
TOP_URL = "https://e5489.jr-odekake.net/e5489/cssp/CBTopMenuSP"

STATE_FILE = Path(os.getenv("STATE_FILE", ".state/sunrise_state.json"))

# e5489は同じ条件でも一時的な「ご案内」を返すことがあるため、
# 1巡回内で複数回試す。
# 1巡回内で最大10回。成功した時点で終了するが、失敗時は10回まで粘る
MAX_ATTEMPTS = max(1, int(os.getenv("SUNRISE_MAX_ATTEMPTS", "10")))

# 失敗時の段階的な待ち時間（秒）
RETRY_BACKOFF_SECONDS = (10, 15, 20, 30, 45, 60, 75, 90, 105)
# 失敗時の最大待ち時間上限（120秒）
MAX_BACKOFF_SECONDS = 120

# 10回リトライを許容しつつ、1巡回は最大15分で必ず打ち切る
MAX_RUN_SECONDS = int(os.getenv("SUNRISE_MAX_RUN_SECONDS", "900"))

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

    if dep_tag == "input" and arr_tag == "input":
        print(
            f"  -> 駅名入力画面を確認: {current_path(page)}",
            flush=True,
        )
        return

    if dep_tag != "select" or arr_tag != "select":
        raise TemporaryPageError(
            f"発着駅要素の形式が想定外: depart={dep_tag}, arrive={arr_tag}"
        )

    form = page.locator('form[name="formTrainEntry"]')

    if form.count() == 0:
        raise TemporaryPageError(
            "駅名入力へ移る formTrainEntry が見つからない"
        )

    print(
        "  -> 「駅名を入力」へ正規POST遷移",
        flush=True,
    )

    try:
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=20000,
        ):
            form.first.evaluate("form => form.submit()")
    except Exception:
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
# 3. 検索条件入力
# ------------------------------------------------------------

def fill_search_conditions(page) -> None:
    depart = require_env("DEPART_STATION")
    arrive = require_env("ARRIVE_STATION")
    travel_date = require_env("TRAVEL_DATE").replace("-", "")
    hour = require_env("DEPART_HOUR").zfill(2)
    minute = require_env("DEPART_MINUTE").zfill(2)

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

    page.locator('select[name="inputDate"]').select_option(value=travel_date)
    page.locator('select[name="inputHour"]').select_option(value=hour)
    page.locator('select[name="inputMinute"]').select_option(value=minute)

    # 出発
    page.locator('input[name="inputType"][value="0"]').check()

    search_toggle = page.locator('button[aria-controls="search-method"]')

    if search_toggle.count() == 0:
        raise TemporaryPageError(
            "乗り換え設定の開閉ボタンが見つかりません"
        )

    expanded = (
        search_toggle.first.get_attribute("aria-expanded") or ""
    ).lower()

    if expanded != "true":
        search_toggle.first.click()
        page.wait_for_timeout(300)

    no_transfer = page.locator('input[name="inputSearchType"][value="1"]')

    if no_transfer.count() == 0:
        raise TemporaryPageError(
            "「一度も乗り換えしない」のラジオボタンが見つかりません"
        )

    try:
        no_transfer.check(timeout=5000)
    except Exception:
        label = no_transfer.locator("xpath=ancestor::label[1]")
        if label.count() == 0:
            raise
        label.click(force=True)

    if not no_transfer.is_checked():
        raise TemporaryPageError(
            "「一度も乗り換えしない」を選択できませんでした"
        )

    train_section = page.locator("#without-connection")

    if train_section.count() == 0:
        raise TemporaryPageError(
            "「利用列車」セクションが見つかりません"
        )

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
    button = page.locator("button.decide-button")

    if button.count() == 0:
        button = page.get_by_text("検索する", exact=False)

    if button.count() == 0:
        raise TemporaryPageError(
            "「検索する（新規予約）」ボタンが見つからない"
        )

    print("  -> 「検索する」をクリック", flush=True)

    try:
        with page.expect_navigation(
            wait_until="domcontentloaded",
            timeout=15000,
        ):
            button.first.click(timeout=8000)
    except Exception:
        # navigationイベントを取り逃しても、遷移済みなら続行する。
        page.wait_for_timeout(800)

    print(
        f"  -> 検索クリック後: title={page.title()!r}, "
        f"path={current_path(page)!r}",
        flush=True,
    )

    if is_guide_or_error(page):
        raise TemporaryPageError("検索後にご案内/エラー")

    # 保存された実際のスマホ版HTMLでは、
    # car-grouping-list の直下は dt/dd ではなく table.seat-status-table。
    # 「サンライズ出雲」の列車ブロック内に B寝台/A寝台の行が出るまで待つ。
    try:
        page.wait_for_function(
            """() => {
                const routes = Array.from(
                    document.querySelectorAll(
                        'ol.route-train-list > li.route-train-list__line.express'
                    )
                );

                const route = routes.find(li => {
                    const title = (
                        li.querySelector('.route-train-list__train')
                        ?.textContent || ''
                    );
                    return title.includes('サンライズ出雲');
                });

                if (!route) return false;

                const rows = Array.from(
                    route.querySelectorAll(
                        'table.seat-status-table tr'
                    )
                );

                return rows.some(tr =>
                    tr.querySelector('img[alt="B寝台"]')
                ) && rows.some(tr =>
                    tr.querySelector('img[alt="A寝台"]')
                );
            }""",
            timeout=12000,
        )
    except Exception:
        body = norm(
            page.locator("body").inner_text(timeout=5000)
        )
        raise TemporaryPageError(
            "経路・設備選択ページには到達したが、"
            "サンライズ出雲の寝台テーブルを確認できない"
            f" (sunrise={'サンライズ出雲' in body}, "
            f"B寝台={'B寝台' in body}, "
            f"A寝台={'A寝台' in body})"
        )

    print(
        f"  -> 検索結果画面を確認: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 4. 最初の経路・設備選択
# ------------------------------------------------------------

def read_initial_route_status(page) -> dict:
    """
    スマホ版(cssp)の保存HTMLで確認した実構造に合わせる。

    ol.route-train-list
      li ... 特急サンライズ出雲
        dl.car-grouping-list
          table.seat-status-table
            tr 普通 ...
            tr B寝台 禁煙 ...
            tr B寝台 喫煙 ...
            tr A寝台 禁煙 ...
            tr A寝台 喫煙 ...

    画面上は「普通 / B寝台 / A寝台」に見えるが、
    DOMは dt/dd ではなく table/tr なので、行単位で判定する。
    """

    result = page.evaluate(
        """() => {
            const STATUS = new Set([
                '空席あり',
                '空席残りわずか',
                '残席なし'
            ]);

            const normalize = s =>
                (s || '').replace(/\\s+/g, ' ').trim();

            const routes = Array.from(
                document.querySelectorAll(
                    'ol.route-train-list > li.route-train-list__line.express'
                )
            );

            const route = routes.find(li => {
                const title = normalize(
                    li.querySelector('.route-train-list__train')
                    ?.textContent
                );
                return title.includes('サンライズ出雲')
                    && li.querySelector('table.seat-status-table tr');
            });

            if (!route) {
                return {
                    routeFound: false,
                    title: '',
                    rows: []
                };
            }

            const title = normalize(
                route.querySelector('.route-train-list__train')
                ?.textContent
            );

            // 同じ列車ブロック内に空tableがある場合があるため、
            // trを持つtableだけを見る。
            const tables = Array.from(
                route.querySelectorAll('table.seat-status-table')
            ).filter(t => t.querySelector('tr'));

            const table = tables[0] || null;

            if (!table) {
                return {
                    routeFound: true,
                    tableFound: false,
                    title,
                    rows: []
                };
            }

            const rows = Array.from(
                table.querySelectorAll('tr')
            ).map(tr => {
                let kind = '';

                if (tr.querySelector('img[alt="B寝台"]')) {
                    kind = 'B寝台';
                } else if (tr.querySelector('img[alt="A寝台"]')) {
                    kind = 'A寝台';
                } else {
                    const carType = normalize(
                        tr.querySelector('.car-type')?.textContent
                    );
                    if (carType.includes('普通')) {
                        kind = '普通';
                    } else {
                        kind = carType;
                    }
                }

                const statusAlts = Array.from(
                    tr.querySelectorAll('td img[alt]')
                )
                .map(img => img.getAttribute('alt'))
                .filter(alt => STATUS.has(alt));

                return {
                    kind,
                    text: normalize(tr.textContent),
                    statusAlts
                };
            });

            return {
                routeFound: true,
                tableFound: true,
                title,
                tableCount: tables.length,
                rows
            };
        }"""
    )

    rows = result.get("rows") or []

    diag = " / ".join(
        f"{row.get('kind') or '(不明)'}:"
        f"{','.join(row.get('statusAlts') or []) or 'markなし'}"
        for row in rows
    )

    print(
        "  -> スマホ版テーブル解析: "
        f"title={result.get('title')!r}, "
        f"tables={result.get('tableCount', 0)}, "
        f"rows={len(rows)} [{diag}]",
        flush=True,
    )

    if not result.get("routeFound"):
        raise TemporaryPageError(
            "サンライズ出雲の列車ブロックを取得できない"
        )

    if not result.get("tableFound"):
        raise TemporaryPageError(
            "サンライズ出雲のseat-status-tableを取得できない"
        )

    ordinary_rows = []
    sleeper_rows = []

    for row in rows:
        kind = norm(row.get("kind"))
        alts = [
            alt for alt in (row.get("statusAlts") or [])
            if alt in POSITIVE_ALTS or alt == NEGATIVE_ALT
        ]

        if not alts:
            continue

        item = {
            "label": kind,
            "alts": alts,
        }

        if kind in {"B寝台", "A寝台"}:
            sleeper_rows.append(item)
        else:
            ordinary_rows.append(item)

    b_rows = [
        item for item in sleeper_rows
        if item["label"] == "B寝台"
    ]
    a_rows = [
        item for item in sleeper_rows
        if item["label"] == "A寝台"
    ]

    if not b_rows or not a_rows:
        raise TemporaryPageError(
            "B寝台/A寝台の空席行を取得できない"
            f" (B={len(b_rows)}, A={len(a_rows)})"
        )

    if ordinary_rows:
        ordinary_text = " / ".join(
            f"{item['label']}:{'/'.join(item['alts'])}"
            for item in ordinary_rows
        )
        print(
            f"  -> 参考（通知対象外）: {ordinary_text}",
            flush=True,
        )

    positives = []
    total_marks = 0
    negative_marks = 0

    for item in sleeper_rows:
        for alt in item["alts"]:
            total_marks += 1

            if alt in POSITIVE_ALTS:
                positives.append(
                    f"{item['label']}:{alt}"
                )
            elif alt == NEGATIVE_ALT:
                negative_marks += 1

    sleeper_text = " / ".join(
        f"{item['label']}:{'/'.join(item['alts'])}"
        for item in sleeper_rows
    )

    print(
        f"  -> 寝台判定: {sleeper_text}",
        flush=True,
    )

    return {
        "rows": sleeper_rows,
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
    change = page.get_by_text("この列車を変更", exact=True)

    if change.count() == 0:
        raise TemporaryPageError(
            "「この列車を変更」が見つからない"
        )

    change.first.click()

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

    later = page.locator("button.change-next-train-button")

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
                ("REQ", request.method, p.path, field_names)
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

            location = response.headers.get("location", "")
            location_path = ""
            if location:
                try:
                    location_path = urllib.parse.urlsplit(
                        urllib.parse.urljoin(response.url, location)
                    ).path
                except Exception:
                    location_path = "(unparsed)"

            network_events.append(
                ("RES", response.status, p.path, location_path)
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
            page.wait_for_timeout(1500)

        page.wait_for_timeout(1000)

    finally:
        try:
            page.remove_listener("request", on_request)
            page.remove_listener("response", on_response)
        except Exception:
            pass

    for event in network_events:
        if event[0] == "REQ":
            _, method, path, fields = event
            print(
                f"     NET REQ {method} {path} fields={fields}",
                flush=True,
            )
        else:
            _, status, path, location_path = event
            suffix = f" -> {location_path}" if location_path else ""
            print(
                f"     NET RES {status} {path}{suffix}",
                flush=True,
            )

    if is_guide_or_error(page):
        try:
            body = norm(page.locator("body").inner_text(timeout=5000))
            codes = re.findall(r"\b\d{6,10}\b", body)
            code_text = ",".join(codes[:3]) if codes else "none"
        except Exception:
            code_text = "unknown"

        raise TemporaryPageError(
            f"後の列車クリック後にご案内/エラー (code={code_text})"
        )

    if page.locator('button[aria-controls="train-1"]').count() == 0:
        raise TemporaryPageError(
            "列車変更画面の候補1～3を確認できない"
        )

    print(
        f"  -> 後の列車へ移動成功: {current_path(page)}",
        flush=True,
    )


# ------------------------------------------------------------
# 6. train-1 / train-2 / train-3 の確認
# ------------------------------------------------------------

def candidate_name_from_li(li, fallback: str) -> str:
    txt = norm(li.inner_text())
    m = re.search(r"特急サンライズ出雲（[^）]+）", txt)
    if m:
        return m.group(0)
    return fallback


def read_three_later_trains(page) -> dict:
    positives = []
    details = []
    checked = 0

    for index in range(1, 4):
        panel_id = f"train-{index}"
        button = page.locator(f'button[aria-controls="{panel_id}"]')
        panel = page.locator(f"#{panel_id}")

        if button.count() == 0 or panel.count() == 0:
            raise TemporaryPageError(f"{panel_id} が見つからない")

        expanded = (
            button.first.get_attribute("aria-expanded") or ""
        ).lower()

        if expanded != "true":
            button.first.click()
            page.wait_for_timeout(350)

        li = panel.first.locator("xpath=ancestor::li[1]")

        if li.count() > 0:
            name = candidate_name_from_li(li.first, panel_id)
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
            alt = imgs.nth(j).get_attribute("alt")
            if alt:
                alts.append(alt)

        checked += 1
        details.append(f"{name}:{'/'.join(alts)}")

        for alt in alts:
            if alt in POSITIVE_ALTS:
                positives.append(f"{name}:{alt}")

        print(f"  -> {name}: {' / '.join(alts)}", flush=True)

    return {
        "checked": checked,
        "details": details,
        "positives": positives,
    }


# ------------------------------------------------------------
# 1巡回処理
# ------------------------------------------------------------

def perform_check(page) -> tuple[bool, str]:
    open_top_and_click_new_reservation(page)
    move_to_station_name_entry(page)
    fill_search_conditions(page)
    submit_search(page)

    initial = read_initial_route_status(page)

    print(
        f"  -> 初回寝台マーク数={initial['total']}, "
        f"寝台空席あり={len(initial['positives'])}",
        flush=True,
    )

    if initial["positives"]:
        return (
            True,
            "初回検索: " + " / ".join(initial["positives"]),
        )

    if not initial["all_negative"]:
        raise TemporaryPageError(
            "初回結果が「すべて残席なし」ではなく、○/△もないため判定保留"
        )

    print(
        "  -> 初回はすべて残席なし。「この列車を変更」→「後の列車」へ",
        flush=True,
    )

    open_later_trains(page)
    later = read_three_later_trains(page)

    if later["positives"]:
        return (
            True,
            "後の列車: " + " / ".join(later["positives"]),
        )

    return (
        False,
        "初回は全て残席なし。後の列車3候補も○/△なし。 "
        + " / ".join(later["details"]),
    )


def run_once() -> int:
    print(
        f"[{now_text()}] SCRIPT_VERSION={SCRIPT_VERSION}",
        flush=True,
    )

    if in_maintenance():
        print(
            f"[{now_text()}] 01:30-05:30 JST は監視停止。"
            "e5489にはアクセスしません。",
            flush=True,
        )
        return 0

    state = load_state()
    was_notified = bool(state.get("ntfy_notified", False))

    available = None
    details = ""
    errors: list[str] = []
    run_started = time.monotonic()
    attempts_used = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        device = pw.devices["Galaxy S24"]

        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts_used = attempt

                context = browser.new_context(
                    **device,
                    locale="ja-JP",
                    timezone_id="Asia/Tokyo",
                )
                page = context.new_page()
                # 1操作が長時間ぶら下がらないよう上限を設定。
                page.set_default_timeout(10000)
                page.set_default_navigation_timeout(15000)

                try:
                    print(
                        f"[{now_text()}] "
                        f"CHECK {attempt}/{MAX_ATTEMPTS} (新しいセッション)",
                        flush=True,
                    )

                    available, details = perform_check(page)

                    print(f"  -> CHECK {attempt} 完遂", flush=True)
                    break

                except Exception as exc:
                    error_text = f"{type(exc).__name__}: {exc}"
                    errors.append(error_text)

                    print(
                        f"  -> 確認不能: {error_text}; {page_diag(page)}",
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
                
                # バックオフ計算（段階的に伸ばしつつ、最大120秒でキャップ）
                base_wait = RETRY_BACKOFF_SECONDS[
                    min(attempt - 1, len(RETRY_BACKOFF_SECONDS) - 1)
                ]
                
                # ジッター（0～5秒の揺らぎ）を加算し、上限120秒に抑える
                wait_seconds = min(
                    float(MAX_BACKOFF_SECONDS),
                    base_wait + random.uniform(0, 5)
                )

                # 全体実行制限時間を超える場合は次回定期実行に回す
                if elapsed + wait_seconds >= MAX_RUN_SECONDS:
                    print(
                        "  -> この巡回の再試行時間上限に近づいたため、"
                        "次回定期実行に回します",
                        flush=True,
                    )
                    break

                print(
                    f"  -> セッションを破棄しました。"
                    f"{wait_seconds:.0f}秒後にトップから"
                    f"新しいセッションで再試行します "
                    f"(次回 {attempt + 1}/{MAX_ATTEMPTS})",
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
            print("  -> 今回の失敗: " + " | ".join(errors), flush=True)

        if os.getenv("SUNRISE_SIGNAL_EXIT", "0") == "1":
            return 2
        return 0

    if available:
        print(f"[{now_text()}] 空席検出: {details}", flush=True)

        if not was_notified:
            try:
                stage = (
                    "後の列車で空席を検出"
                    if details.startswith("後の列車")
                    else "最初の検索結果で空席を検出"
                )

                send_ntfy(stage, details)
                state["ntfy_notified"] = True
                print("  -> ntfy通知送信", flush=True)

            except Exception as exc:
                print(
                    f"  -> ntfy送信失敗 ({type(exc).__name__})。"
                    "次回再送します。",
                    file=sys.stderr,
                    flush=True,
                )
                state["ntfy_notified"] = False

    else:
        print(f"[{now_text()}] 空席なし: {details}", flush=True)
        state["ntfy_notified"] = False

    state["available"] = available
    state["details"] = details
    state["last_checked"] = now_text()

    save_state(state)

    print(
        f"[{now_text()}] SUMMARY: "
        f"available={available}, "
        f"notified={state.get('ntfy_notified', False)}, "
        f"attempts={attempts_used}",
        flush=True,
    )

    return 0


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-ntfy", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.test_ntfy:
        send_test_ntfy()
        print("ntfyテスト通知を送信しました。")
        return 0

    return run_once()


if __name__ == "__main__":
    raise SystemExit(main())
