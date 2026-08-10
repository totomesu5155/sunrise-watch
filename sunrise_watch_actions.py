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
TOP_URL = "https://e5489.jr-odekake.net/e5489/cspc/CBTopMenuPC?screenId=CP6102"
ENTRY_URL = "https://e5489.jr-odekake.net/e5489/cspc/CBTrainEntryBackPC"
STATE_FILE = Path(os.getenv("STATE_FILE", ".state/sunrise_state.json"))
MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 15


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
