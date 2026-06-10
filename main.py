# -*- coding: utf-8 -*-
"""
tebestck_bot — 레버리지 스윙 추적 봇 (yfinance 버전)
- 야후 파이낸스로 본주(하이닉스/샌디스크)만 트래킹. 키·계좌 불필요.
- 단계 판정: 본주의 10거래일 고점 대비 낙폭 (정확)
- 교체금액: 레버 평가액을 본주 낙폭의 2배로 근사 (추정 → 토스에서 확인)
- 단계 바뀌면 텔레그램 알림. 자동주문 X. 본장 기준.

★ 토큰/chat_id는 환경변수(.env / Railway Variables)로.
"""

import os, json, time
import yfinance as yf

# ── 환경변수 ──────────────────────────────────────────────
TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

STATE_FILE = "state.json"
CHECK_INTERVAL = 300            # 5분
SET = {"baseline": 30, "t1": 10, "t2": 20, "t3": 30}   # -t1→40, -t2→50, -t3→60

# ── 보유/종목 (★ 거래 때마다 주수·레버평가액만 갱신) ─────────
#   lev_ref     : 최근 레버 평가액(현재가×주수). 통화는 ccy 기준.
#   spot_ref    : 그 lev_ref 적었을 때의 본주 가격 (레버 추정 기준점).
#   둘은 가끔/거래 시 같이 갱신하면 추정이 정확해짐.
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

# ── 상태 ──────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(st):
    try:
        with open(STATE_FILE, "w") as f: json.dump(st, f)
    except Exception as e: print("save_state:", e)
STATE = load_state()

# ── 텔레그램 ──────────────────────────────────────────────
import requests
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

# ── 야후 시세: 본주 현재가 + 10거래일 고점 ──────────────────
def quote(yticker):
    t = yf.Ticker(yticker)
    # 현재가: fast_info 우선, 실패 시 최근 종가
    cur = None
    try:
        cur = float(t.fast_info["last_price"])
    except Exception:
        pass
    hist = t.history(period="1mo", interval="1d")
    if hist is None or hist.empty:
        raise RuntimeError(f"{yticker} 데이터 없음")
    if cur is None:
        cur = float(hist["Close"].iloc[-1])
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
    # 레버 평가액 근사: 본주가 ref 대비 r 변하면 레버는 ~2r (2배 추종)
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

def run_check():
    for name, c in POOLS.items():
        try:
            r = evaluate(name, c)
        except Exception as ex:
            print(f"[{name}] 실패:", ex); continue
        key = f"tier_{name}"; prev = STATE.get(key, 0)
        if r["tier"] != prev:
            tg_send(alert_text(r, prev)); STATE[key] = r["tier"]; save_state(STATE)
        print(f"[{name}] dd={r['dd']:+.1f}% tier={r['tier']} lev~{r['lev_pct']:.0f}%")

def handle_commands():
    offset = STATE.get("tg_offset", 0)
    for u in tg_updates(offset):
        offset = u["update_id"] + 1
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if text.startswith("/status"): tg_send(status_text())
        elif text.startswith("/start"): tg_send("✅ tebestck 봇 작동 중. /status 로 확인.")
        elif text.startswith("/help"): tg_send("/status 현재 상태\n단계 바뀌면 자동 알림.")
    STATE["tg_offset"] = offset; save_state(STATE)

def main():
    tg_send("✅ tebestck 봇 시작됨.\n" + status_text())
    last = 0
    while True:
        handle_commands()
        if time.time() - last >= CHECK_INTERVAL:
            run_check(); last = time.time()

if __name__ == "__main__":
    main()
