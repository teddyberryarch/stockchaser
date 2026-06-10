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
        "label": "하이닉스", "yf": "000660.KS", "lev_yf": "0195S0.KS", "ccy": "KRW",
        "spot_name": "SK하이닉스", "lev_name": "TIGER 하이닉스 레버",
        "spot_shares": 13, "lev_shares": 916, "min_action": 2_000_000, "taxable": False,
        "t1": 10, "t2": 20, "t3": 30,
    },
    "sandisk": {
        "label": "샌디스크", "yf": "SNDK", "lev_yf": "SNXX", "ccy": "USD",
        "spot_name": "샌디스크", "lev_name": "TRADR 샌디스크 2배",
        "spot_shares": 11, "lev_shares": 280, "min_action": 1_500, "taxable": True,
        "t1": 15, "t2": 30, "t3": 40,
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
    hist = t.history(period="3mo", interval="1d")   # 60거래일 ~ 3개월
    if hist is None or hist.empty:
        raise RuntimeError(f"{yticker} 데이터 없음")
    if cur is None: cur = float(hist["Close"].iloc[-1])
    roll_high = float(hist["High"].tail(60).max())  # 60거래일 rolling 최고가
    return cur, roll_high

def usdkrw():
    """USD/KRW 환율. 실패 시 보수적 기본값."""
    try:
        t = yf.Ticker("KRW=X")
        try: return float(t.fast_info["last_price"])
        except Exception: pass
        h = t.history(period="5d", interval="1d")
        return float(h["Close"].iloc[-1])
    except Exception as e:
        print("usdkrw 실패, 기본값 사용:", e)
        return 1380.0

# 경고 단계(고점 대비 더 깊은 낙폭) — 비중은 60% 유지, 경고만
WARN_LEVELS = [40, 50, 60]  # -40/-50/-60%

# ── 사이클 모델 ──────────────────────────────────────────────
# 하락: 사이클고점 대비 -t1/-t2/-t3 → 레버 40/50/60 단계적으로 키움
# 상승: 안 줄임(60% 유지, 다 먹음)
# 리셋: 사이클고점 +RESET_PCT% 도달 → 레버 30% 한 방에 복귀, 그 가격이 새 사이클고점
RESET_PCT = 30  # 전고점 대비 +30% 도달 시 리셋

TIER_NAME = {0: "정상", 1: "폭락 1단계", 2: "폭락 2단계", 3: "폭락 3단계", 9: "리셋"}

def tier_from_drop(dd, t1, t2, t3):
    """사이클고점 대비 낙폭 → (단계, 목표비중). 키우는 방향 전용."""
    e = 1e-6
    if dd <= -t3 + e: return 3, 60
    if dd <= -t2 + e: return 2, 50
    if dd <= -t1 + e: return 1, 40
    return 0, 30

def fmt(a, ccy):
    return f"{round(a):,}원" if ccy == "KRW" else f"${a:,.0f}"

def evaluate(name, c, fx=1.0, st=None):
    """st = 사이클 상태 {cyc_high(원화), target, maxed}. 갱신해 돌려줌.
    하이브리드: 평소(기본30%)엔 60일 rolling 고점 자동 추적,
    폭락 키우기 시작하면 그 고점 고정 → +30% 리셋까지 60% 유지 → 리셋 후 다시 rolling.
    판정·표시 모두 원화 기준(달러 종목은 환율 곱해 원화로 통일)."""
    spot_p_raw, roll_high_raw = quote(c["yf"])     # 원래 통화(달러/원)
    lev_p_raw, _ = quote(c["lev_yf"])
    krw = fx if c["ccy"] == "USD" else 1.0

    # 전부 원화로 환산해서 판정 (어차피 원화로 보고 거래)
    spot_p    = spot_p_raw * krw
    roll_high = roll_high_raw * krw
    lev_p     = lev_p_raw * krw

    st = dict(st or {})
    target = st.get("target", 30)
    maxed  = st.get("maxed", False)
    # 사이클 고점: 폭락 키우기 전(기본 상태)엔 rolling 고점 자동 추적.
    # 키우기 시작(target>30 or maxed)하면 고정된 값 유지.
    ramping = (target > 30) or maxed
    if not ramping:
        cyc_high = roll_high            # 자동: 60일 최고가 (신고가 갱신 자동 반영)
    else:
        cyc_high = st.get("cyc_high", roll_high)   # 고정값 유지

    dd = (spot_p - cyc_high) / cyc_high * 100 if cyc_high else 0   # 사이클고점 대비(원화)

    event = None          # "ramp" | "reset"
    tier = 0
    if not maxed:
        t, tgt = tier_from_drop(dd, c["t1"], c["t2"], c["t3"])
        tier = t
        if tgt > target:                 # 더 키우는 방향만 (오를 땐 안 줄임)
            if target == 30:
                cyc_high = roll_high     # 폭락 시작 순간의 rolling 고점을 고정
                dd = (spot_p - cyc_high) / cyc_high * 100 if cyc_high else 0
            target = tgt
            event = "ramp"
            if target >= 60:
                maxed = True             # 60% 풀충 → 리셋 대기
        # 오를 땐 target 유지 (절대 안 줄임)
    else:
        tier = 3
        if spot_p >= cyc_high * (1 + RESET_PCT / 100):   # 전고점 +30% 도달
            target = 30
            maxed = False
            event = "reset"
            tier = 0
            cyc_high = roll_high         # 리셋 후 다시 rolling으로

    # 평가 (전부 원화)
    spot_val = spot_p * c["spot_shares"]
    lev_est  = lev_p * c["lev_shares"]
    pool = spot_val + lev_est
    lev_pct = lev_est / pool * 100 if pool else 0
    min_action_krw = c["min_action"] * krw
    delta = pool * target / 100 - lev_est
    action = None
    if delta > min_action_krw:
        amt = min(delta, spot_val)
        action = {"dir": "up", "amt": amt, "sell": c["spot_name"], "buy": c["lev_name"]}
    elif delta < -min_action_krw:
        amt = min(-delta, lev_est)
        action = {"dir": "down", "amt": amt, "sell": c["lev_name"], "buy": c["spot_name"]}

    reset_price = cyc_high * (1 + RESET_PCT / 100)

    new_st = {"cyc_high": cyc_high, "target": target, "maxed": maxed}
    return {"label": c["label"], "ccy": c["ccy"], "taxable": c["taxable"],
            "spot_p": spot_p, "lev_p": lev_p, "cyc_high": cyc_high,
            "dd": dd, "krw": krw,
            "tier": tier, "target": target, "maxed": maxed, "event": event,
            "lev_pct": lev_pct, "action": action,
            "reset_price": reset_price, "reset_pct": RESET_PCT,
            "spot_shares": c["spot_shares"], "lev_shares": c["lev_shares"],
            "spot_p_krw": spot_p, "cyc_high_krw": cyc_high,
            "reset_price_krw": reset_price, "pool_krw": pool,
            "amt_krw": (action["amt"]) if action else 0}, new_st

def alert_text(r):
    ev = r["event"]
    if ev == "reset":
        return "\n".join([
            f"🟢 <b>{r['label']} 리셋 — 레버 60%→30%</b>",
            f"전고점 +{r['reset_pct']}% 도달 (본주 {fmt(r['spot_p_krw'],'KRW')})",
            f"레버 비중 약 {r['lev_pct']:.0f}% → 목표 30%로 차익실현",
            "",
            f"▶ {r['action']['sell']} {fmt(r['amt_krw'],'KRW')}어치 매도 → {r['action']['buy']} 매수" if r["action"] else "(조정 금액 작음)",
            "※ 본장에서. 이제 새 사이클 시작(이 가격이 새 기준 고점).",
        ])
    # ramp (폭락 단계 키움)
    L = [f"🔴 <b>{r['label']} {TIER_NAME[r['tier']]} — 레버 키움</b>",
         f"사이클고점 대비 {r['dd']:+.1f}% (본주 {fmt(r['spot_p_krw'],'KRW')})",
         f"레버 약 {r['lev_pct']:.0f}% → 목표 {r['target']}%"]
    a = r["action"]
    if a:
        tag = " · ⚠️22% 과세누적" if r["taxable"] else " · 비과세"
        amt = fmt(r["amt_krw"], "KRW")
        L += ["", f"▶ {a['sell']} {amt}어치 매도 → {a['buy']} 매수{tag}",
              "※ 본장에서 금액 기준. 교체금액은 추정이라 나무증권에서 확인."]
        if r["target"] >= 60:
            L.append(f"※ 60% 풀충. 이제 안 줄이고 유지 → 전고점 +{r['reset_pct']}%({fmt(r['reset_price_krw'],'KRW')}) 도달 시 리셋.")
    else:
        L.append("(조정 금액이 작아 액션 없음)")
    return "\n".join(L)

def main():
    fx = usdkrw()
    snapshot = {"_fx": fx}
    for name, c in POOLS.items():
        skey = f"cyc_{name}"
        prev_st = STATE.get(skey)
        try:
            r, new_st = evaluate(name, c, fx, prev_st)
        except Exception as ex:
            print(f"[{name}] 실패:", ex)
            snapshot[name] = {"label": c.get("label", name), "error": str(ex)}
            continue
        snapshot[name] = r
        # 이벤트(키움/리셋) 발생 시에만 알림
        if r["event"] in ("ramp", "reset"):
            tg_send(alert_text(r))
        STATE[skey] = new_st
        ph = "리셋대기(60%)" if r["maxed"] else f"단계{r['tier']}"
        print(f"[{name}] dd={r['dd']:+.1f}% target={r['target']}% {ph} "
              f"lev~{r['lev_pct']:.0f}% cyc_high={r['cyc_high']:.1f} ev={r['event']}")

    snapshot["_ts"] = int(time.time())
    snapshot["_tier_name"] = TIER_NAME
    snapshot["_reset_pct"] = RESET_PCT
    write_json(DATA_FILE, snapshot)
    write_json(STATE_FILE, STATE)

if __name__ == "__main__":
    main()
