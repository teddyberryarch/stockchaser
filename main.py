# -*- coding: utf-8 -*-
"""
tebestck_bot — 레버리지 스윙 추적 봇 + 웹 대시보드 (yfinance)
- 야후로 본주(하이닉스/샌디스크)만 트래킹. 키·계좌 불필요.
- 단계 판정: 본주 10거래일 고점 대비 낙폭 (정확)
- 교체금액: 레버 평가액을 본주 낙폭의 2배로 근사 (추정 → 토스 확인)
- 백그라운드 스레드: 5분마다 체크, 단계 바뀌면 텔레그램 알림 (자동주문 X)
- 메인: Flask 웹 대시보드 ( / 에서 현황, /api 로 JSON )

★ 토큰/chat_id는 환경변수(Railway Variables)로.
"""

import os, json, time, threading
import yfinance as yf
import requests
from flask import Flask, jsonify

# ── 환경변수 ──────────────────────────────────────────────
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]
PORT       = int(os.environ.get("PORT", "8080"))

STATE_FILE = "state.json"
CHECK_INTERVAL = 300
SET = {"baseline": 30, "t1": 10, "t2": 20, "t3": 30}

POOLS = {
    "hynix": {
        "label": "하이닉스", "yf": "000660.KS", "ccy": "KRW",
        "spot_name": "SK하이닉스", "lev_name": "TIGER 하이닉스 레버",
        "spot_shares": 17, "min_action": 2_000_000, "taxable": False,
        "lev_ref": 9_964_845, "spot_ref": 2_138_000,
    },
    "sandisk": {
        "label": "샌디스크", "yf": "SNDK", "ccy": "USD",
        "spot_name": "샌디스크", "lev_name": "TRADR 샌디스크 2배",
        "spot_shares": 11, "min_action": 1_500, "taxable": True,
        "lev_ref": 6_888, "spot_ref": 1_646.54,
    },
}

# ── 상태 + 캐시(대시보드가 매번 야후 안 때리게) ────────────
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(st):
    try:
        with open(STATE_FILE, "w") as f: json.dump(st, f)
    except Exception as e: print("save_state:", e)
STATE = load_state()
CACHE = {"ts": 0, "data": {}}   # 마지막 평가 결과

# ── 텔레그램 ──────────────────────────────────────────────
def tg_send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
    except Exception as e: print("tg_send:", e)
def tg_updates(offset):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                         params={"offset": offset, "timeout": 10}, timeout=20)
        return r.json().get("result", [])
    except Exception as e:
        print("tg_updates:", e); return []

# ── 야후 시세 ──────────────────────────────────────────────
def quote(yticker):
    t = yf.Ticker(yticker)
    cur = None
    try: cur = float(t.fast_info["last_price"])
    except Exception: pass
    hist = t.history(period="1mo", interval="1d")
    if hist is None or hist.empty:
        raise RuntimeError(f"{yticker} 데이터 없음")
    if cur is None: cur = float(hist["Close"].iloc[-1])
    high10 = float(hist["High"].tail(10).max())
    return cur, high10

# ── 로직 ──────────────────────────────────────────────────
def tier_target(dd):
    if dd <= -SET["t3"]: return 3, 60
    if dd <= -SET["t2"]: return 2, 50
    if dd <= -SET["t1"]: return 1, 40
    return 0, SET["baseline"]
TIER_NAME = {0: "정상", 1: "폭락 1단계", 2: "폭락 2단계", 3: "폭락 3단계"}

def fmt(a, ccy):
    return f"{round(a):,}원" if ccy == "KRW" else f"${a:,.0f}"

def evaluate(name, c):
    spot_p, high10 = quote(c["yf"])
    dd = (spot_p - high10) / high10 * 100 if high10 else 0
    tier, target = tier_target(dd)
    spot_val = spot_p * c["spot_shares"]
    r = (spot_p / c["spot_ref"] - 1) if c["spot_ref"] else 0
    lev_est = max(0.0, c["lev_ref"] * (1 + 2 * r))
    pool = spot_val + lev_est
    lev_pct = lev_est / pool * 100 if pool else 0
    target_val = pool * target / 100
    delta = target_val - lev_est
    action = None
    if delta > c["min_action"]:
        amt = min(delta, spot_val)
        action = {"dir": "up", "amt": amt, "sell": c["spot_name"], "buy": c["lev_name"]}
    elif delta < -c["min_action"]:
        amt = min(-delta, lev_est)
        action = {"dir": "down", "amt": amt, "sell": c["lev_name"], "buy": c["spot_name"]}
    return {"label": c["label"], "ccy": c["ccy"], "taxable": c["taxable"],
            "spot_p": spot_p, "high10": high10, "dd": dd,
            "tier": tier, "target": target, "lev_pct": lev_pct, "action": action}

def alert_text(r, prev):
    e = "🔴" if r["tier"] > prev else "🟢"
    arrow = "폭락 진입" if r["tier"] > prev else "회복"
    ccy = r["ccy"]
    L = [f"{e} <b>{r['label']} {TIER_NAME[r['tier']]} ({arrow})</b>",
         f"본주 {fmt(r['spot_p'], ccy)} · 10일고점 대비 {r['dd']:+.1f}%",
         f"레버 약 {r['lev_pct']:.0f}% → 목표 {r['target']}%"]
    a = r["action"]
    if a:
        tag = " · ⚠️22% 과세누적" if r["taxable"] else " · 비과세"
        L += ["", f"▶ {a['sell']} {fmt(a['amt'], ccy)}어치 매도",
              f"▶ {a['buy']} {fmt(a['amt'], ccy)}어치 매수",
              f"≈ {fmt(a['amt'], ccy)} 교체{tag}",
              "※ 본장에서 금액 기준으로. 교체금액은 추정이라 토스에서 확인."]
    else:
        L.append("(조정 금액이 작아 액션 없음)")
    return "\n".join(L)

def status_text():
    out = ["📊 <b>현재 상태</b>"]
    for name, c in POOLS.items():
        try:
            r = evaluate(name, c)
            out.append(f"\n<b>{r['label']}</b>  {TIER_NAME[r['tier']]}\n"
                       f"  본주 {fmt(r['spot_p'], r['ccy'])} / 고점 {fmt(r['high10'], r['ccy'])} ({r['dd']:+.1f}%)\n"
                       f"  레버 약 {r['lev_pct']:.0f}% → 목표 {r['target']}%")
        except Exception as ex:
            out.append(f"\n<b>{c['label']}</b>  조회 실패: {ex}")
    return "\n".join(out)

def refresh():
    """두 종목 평가 → 캐시 갱신 → 단계 변화 시 알림."""
    data = {}
    for name, c in POOLS.items():
        try:
            r = evaluate(name, c)
        except Exception as ex:
            print(f"[{name}] 실패:", ex); data[name] = {"error": str(ex), "label": c["label"]}; continue
        data[name] = r
        key = f"tier_{name}"; prev = STATE.get(key, 0)
        if r["tier"] != prev:
            tg_send(alert_text(r, prev)); STATE[key] = r["tier"]; save_state(STATE)
        print(f"[{name}] dd={r['dd']:+.1f}% tier={r['tier']} lev~{r['lev_pct']:.0f}%")
    CACHE["ts"] = time.time(); CACHE["data"] = data
    return data

def handle_commands():
    offset = STATE.get("tg_offset", 0)
    for u in tg_updates(offset):
        offset = u["update_id"] + 1
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if text.startswith("/status"): tg_send(status_text())
        elif text.startswith("/start"): tg_send("✅ tebestck 봇 작동 중. /status 로 확인.")
        elif text.startswith("/help"): tg_send("/status 현재 상태\n단계 바뀌면 자동 알림.")
    STATE["tg_offset"] = offset; save_state(STATE)

# ── 백그라운드 워커 (알림 루프) ────────────────────────────
def worker_loop():
    tg_send("✅ tebestck 봇 시작됨.\n" + status_text())
    last = 0
    while True:
        try:
            handle_commands()
            if time.time() - last >= CHECK_INTERVAL:
                refresh(); last = time.time()
        except Exception as e:
            print("worker_loop err:", e); time.sleep(5)

# ── Flask 웹 대시보드 ─────────────────────────────────────
app = Flask(__name__)

TIER_COLOR = {0: "#3fb950", 1: "#ffb454", 2: "#f0883e", 3: "#f6435b"}

@app.route("/api")
def api():
    # 캐시 5분 넘으면 갱신
    if time.time() - CACHE["ts"] > CHECK_INTERVAL:
        refresh()
    return jsonify({"ts": CACHE["ts"], "data": CACHE["data"]})

@app.route("/")
def index():
    if time.time() - CACHE["ts"] > CHECK_INTERVAL or not CACHE["data"]:
        refresh()
    cards = ""
    for name, r in CACHE["data"].items():
        if r.get("error"):
            cards += f"<div class='card'><div class='label'>{r['label']}</div><div class='err'>조회 실패: {r['error']}</div></div>"
            continue
        ccy = r["ccy"]; col = TIER_COLOR[r["tier"]]
        a = r["action"]
        if a:
            tax = "⚠️ 22% 과세누적" if r["taxable"] else "비과세"
            act = (f"<div class='act'><div class='actrow'>▶ {a['sell']} <b>{fmt(a['amt'],ccy)}</b>어치 매도</div>"
                   f"<div class='actrow'>▶ {a['buy']} <b>{fmt(a['amt'],ccy)}</b>어치 매수</div>"
                   f"<div class='actsum'>≈ {fmt(a['amt'],ccy)} 교체 · {tax}</div></div>")
        else:
            act = "<div class='act none'>조정 불필요 — 유지</div>"
        dd_col = "#3d7eff" if r["dd"] < 0 else "#f6435b"
        cards += f"""
        <div class='card'>
          <div class='top'>
            <div class='label'>{r['label']}</div>
            <div class='tier' style='color:{col};border-color:{col}'>{TIER_NAME[r['tier']]}</div>
          </div>
          <div class='grid'>
            <div><div class='k'>본주</div><div class='v'>{fmt(r['spot_p'],ccy)}</div></div>
            <div><div class='k'>10일고점</div><div class='v'>{fmt(r['high10'],ccy)}</div></div>
            <div><div class='k'>고점대비</div><div class='v' style='color:{dd_col}'>{r['dd']:+.1f}%</div></div>
          </div>
          <div class='lev'>레버 약 <b>{r['lev_pct']:.0f}%</b> → 목표 <b style='color:{col}'>{r['target']}%</b></div>
          {act}
        </div>"""
    ts = time.strftime("%H:%M:%S", time.localtime(CACHE["ts"])) if CACHE["ts"] else "-"
    return f"""<!doctype html><html lang='ko'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>tebestck 대시보드</title>
<meta http-equiv='refresh' content='60'>
<style>
*{{box-sizing:border-box;margin:0}}
body{{background:#0d1117;color:#e6edf3;font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;padding:20px 16px 40px}}
.wrap{{max-width:520px;margin:0 auto}}
h1{{font-size:24px;font-weight:600;margin-bottom:4px}}
.sub{{font-size:15px;color:#8b949e;margin-bottom:20px}}
.card{{background:#161b22;border-radius:16px;padding:18px;margin-bottom:16px}}
.top{{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}}
.label{{font-size:20px;font-weight:600}}
.tier{{font-size:15px;font-weight:600;border:1px solid;border-radius:8px;padding:4px 10px}}
.grid{{display:flex;gap:10px;padding:12px 0;border-top:1px solid #272d36;border-bottom:1px solid #272d36}}
.grid>div{{flex:1}}
.k{{font-size:15px;color:#8b949e}}
.v{{font-size:19px;font-weight:600;margin-top:2px}}
.lev{{font-size:17px;margin:14px 0 10px}}
.act{{background:#0d1117;border-radius:12px;padding:14px;font-size:17px}}
.act.none{{color:#8b949e}}
.actrow{{margin-bottom:6px}}
.actsum{{font-size:16px;color:#8b949e;margin-top:6px}}
.err{{color:#f6435b;font-size:17px;margin-top:8px}}
.foot{{font-size:15px;color:#8b949e;line-height:1.6;margin-top:6px}}
</style></head><body><div class='wrap'>
<h1>tebestck 대시보드</h1>
<div class='sub'>야후 시세 · 10거래일 고점 기준 · 갱신 {ts} (60초마다 자동)</div>
{cards}
<div class='foot'>교체금액은 레버 평가액을 본주 낙폭 2배로 근사한 추정값. 정확치는 토스에서 확인.<br>본장 기준 · 자동주문 없음.</div>
</div></body></html>"""

# ── 실행: 워커는 스레드, Flask는 메인 ──────────────────────
if __name__ == "__main__":
    threading.Thread(target=worker_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
