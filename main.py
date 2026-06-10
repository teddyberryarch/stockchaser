# -*- coding: utf-8 -*-
"""
tebestck_bot — 레버리지 스윙 추적 봇 (GitHub Actions cron 버전)
- 한 번 실행 → 시세 체크 → 단계 변화 시 텔레그램 알림 → data.json/state.json 저장 → 종료
- GitHub Actions가 5분마다 이걸 실행. 서버 상주 없음 → 비용 0.
- 야후로 본주(하이닉스/샌디스크)만 트래킹. 키·계좌 불필요.

환경변수(Actions Secrets):
  TG_TOKEN, TG_CHAT_ID
보유값은 holdings.json 에서 읽음(웹/수동 편집 가능). 없으면 아래 기본값 사용.
"""

import os, json, time
import yfinance as yf
import requests

TG_TOKEN   = os.environ["TG_TOKEN"]
TG_CHAT_ID = os.environ["TG_CHAT_ID"]

STATE_FILE    = "state.json"      # 마지막 단계 기억 (repo에 커밋됨)
DATA_FILE     = "data.json"       # 대시보드가 읽을 현황 (repo에 커밋됨)
HOLDINGS_FILE = "holdings.json"   # 보유값 (웹/수동 편집)

SET = {"baseline": 30, "t1": 10, "t2": 20, "t3": 30}

# 기본 보유값 (holdings.json 없을 때만 사용)
DEFAULT_HOLDINGS = {
    "hynix": {
        "label": "하이닉스", "yf": "000660.KS", "ccy": "KRW",
        "spot_name": "SK하이닉스", "lev_name": "TIGER 하이닉스 레버",
        "spot_shares": 13, "min_action": 2_000_000, "taxable": False,
        "lev_ref": 18_145_960, "spot_ref": 2_084_000,
    },
    "sandisk": {
        "label": "샌디스크", "yf": "SNDK", "ccy": "USD",
        "spot_name": "샌디스크", "lev_name": "TRADR 샌디스크 2배",
        "spot_shares": 11, "min_action": 1_500, "taxable": True,
        "lev_ref": 6_888, "spot_ref": 1_646.54,
    },
}

def read_json(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

POOLS = read_json(HOLDINGS_FILE, DEFAULT_HOLDINGS)
STATE = read_json(STATE_FILE, {})

def tg_send(text):
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
    except Exception as e:
        print("tg_send:", e)

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
    delta = pool * target / 100 - lev_est
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

def main():
    snapshot = {}
    for name, c in POOLS.items():
        try:
            r = evaluate(name, c)
        except Exception as ex:
            print(f"[{name}] 실패:", ex)
            snapshot[name] = {"label": c.get("label", name), "error": str(ex)}
            continue
        snapshot[name] = r
        key = f"tier_{name}"; prev = STATE.get(key, 0)
        if r["tier"] != prev:
            tg_send(alert_text(r, prev))
            STATE[key] = r["tier"]
        print(f"[{name}] dd={r['dd']:+.1f}% tier={r['tier']} lev~{r['lev_pct']:.0f}%")

    # 대시보드용 현황 저장 (한글 라벨 포함)
    snapshot["_ts"] = int(time.time())
    snapshot["_tier_name"] = TIER_NAME
    write_json(DATA_FILE, snapshot)
    write_json(STATE_FILE, STATE)

if __name__ == "__main__":
    main()
