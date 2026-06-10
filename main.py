# -*- coding: utf-8 -*-
"""
tebestck_bot — 레버리지 스윙 추적 봇
- 한국투자증권(KIS) Open API로 하이닉스/샌디스크 시세·10거래일 고점 자동 조회
- 종목별 독립 판정: 풀 내 레버 비중 30%(횡보) → 40/50/60%(폭락 -10/-20/-30%)
- 단계 바뀌면 텔레그램으로 "뭘 얼마 사고팔아라" 알림 (자동주문 X)

★ 키/시크릿은 절대 코드에 직접 넣지 말고 환경변수(.env / Railway Variables)로.
"""

import os
import json
import time
import datetime as dt
import requests

# ───────────────────────────────────────────────────────────
# 환경변수 (Railway Variables 또는 .env)
# ───────────────────────────────────────────────────────────
KIS_APP_KEY    = os.environ["KIS_APP_KEY"]
KIS_APP_SECRET = os.environ["KIS_APP_SECRET"]
TG_TOKEN       = os.environ["TG_TOKEN"]
TG_CHAT_ID     = os.environ["TG_CHAT_ID"]

KIS_BASE = "https://openapi.koreainvestment.com:9443"   # 실전. 모의투자면 openapivts...:29443
STATE_FILE = "state.json"
CHECK_INTERVAL = 300   # 시세 체크 주기(초). 5분.

# ───────────────────────────────────────────────────────────
# 트리거 설정 (전고점=최근 10거래일 고점 대비 낙폭)
# ───────────────────────────────────────────────────────────
SET = {"baseline": 30, "t1": 10, "t2": 20, "t3": 30}  # -t1→40%, -t2→50%, -t3→60%

# ───────────────────────────────────────────────────────────
# 보유 현황 (★ 거래할 때마다 여기 주수만 고치면 됨. 티커는 처음 한 번 확인해서 넣기)
#   - 하이닉스 풀: 전부 국장(KRW). 비과세.
#   - 샌디스크 풀: 전부 미장(USD). 매도차익 22% 과세 누적. 봇은 달러 기준으로 봐서 환율노이즈 제거.
# ───────────────────────────────────────────────────────────
POOLS = {
    "hynix": {
        "label": "하이닉스", "ccy": "KRW", "min_action": 2_000_000, "taxable": False,
        "spot": {"market": "dom", "code": "000660",  "shares": 17,  "name": "SK하이닉스"},
        "lev":  {"market": "dom", "code": "<TIGER레버_6자리코드>", "shares": 491, "name": "TIGER 하이닉스 레버"},
    },
    "sandisk": {
        "label": "샌디스크", "ccy": "USD", "min_action": 1_500, "taxable": True,
        "spot": {"market": "ovs", "excd": "NAS", "symb": "SNDK", "shares": 11, "name": "샌디스크"},
        "lev":  {"market": "ovs", "excd": "<NAS등>", "symb": "<TRADR_티커>", "shares": 280, "name": "TRADR 샌디스크 2배"},
    },
}

# ───────────────────────────────────────────────────────────
# 상태 저장 (마지막 알림 단계 / KIS 토큰 / 텔레그램 offset)
# ───────────────────────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(st):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(st, f)
    except Exception as e:
        print("save_state err:", e)

STATE = load_state()

# ───────────────────────────────────────────────────────────
# 텔레그램
# ───────────────────────────────────────────────────────────
def tg_send(text):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print("tg_send err:", e)

def tg_get_updates(offset):
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": offset, "timeout": 10}, timeout=20,
        )
        return r.json().get("result", [])
    except Exception as e:
        print("tg_get_updates err:", e)
        return []

# ───────────────────────────────────────────────────────────
# KIS 토큰 (24h 유효 → 캐시해서 재사용. 잦은 재발급은 제한 있음)
# ───────────────────────────────────────────────────────────
def kis_token():
    now = time.time()
    if STATE.get("kis_token") and STATE.get("kis_token_exp", 0) > now + 600:
        return STATE["kis_token"]
    r = requests.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={"grant_type": "client_credentials",
              "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET},
        timeout=15,
    )
    d = r.json()
    tok = d["access_token"]
    STATE["kis_token"] = tok
    STATE["kis_token_exp"] = now + int(d.get("expires_in", 86400))
    save_state(STATE)
    return tok

def kis_get(path, tr_id, params):
    h = {
        "authorization": f"Bearer {kis_token()}",
        "appkey": KIS_APP_KEY, "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id, "custtype": "P",
    }
    r = requests.get(f"{KIS_BASE}{path}", headers=h, params=params, timeout=15)
    d = r.json()
    if str(d.get("rt_cd", "0")) not in ("0", ""):
        raise RuntimeError(f"KIS {tr_id} 오류: {d.get('msg1')}")
    return d

# ── 국내주식 현재가 ──
def dom_price(code):
    d = kis_get("/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    return float(d["output"]["stck_prpr"])

# ── 국내주식 10거래일 고점 ──
def dom_high10(code):
    today = dt.date.today()
    start = today - dt.timedelta(days=25)
    d = kis_get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
                "FHKST03010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                 "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                 "FID_INPUT_DATE_2": today.strftime("%Y%m%d"),
                 "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    rows = sorted(d["output2"], key=lambda x: x["stck_bsop_date"])
    highs = [float(x["stck_hgpr"]) for x in rows if x.get("stck_hgpr")][-10:]
    return max(highs)

# ── 해외주식 현재가 ──
def ovs_price(excd, symb):
    d = kis_get("/uapi/overseas-price/v1/quotations/price",
                "HHDFS00000300",
                {"AUTH": "", "EXCD": excd, "SYMB": symb})
    return float(d["output"]["last"])

# ── 해외주식 10거래일 고점 ──
def ovs_high10(excd, symb):
    d = kis_get("/uapi/overseas-price/v1/quotations/dailyprice",
                "HHDFS76240000",
                {"AUTH": "", "EXCD": excd, "SYMB": symb,
                 "GUBN": "0", "BYMD": "", "MODP": "1"})
    rows = sorted(d["output2"], key=lambda x: x["xymd"])
    highs = [float(x["high"]) for x in rows if x.get("high")][-10:]
    return max(highs)

def get_price(leg):
    return dom_price(leg["code"]) if leg["market"] == "dom" else ovs_price(leg["excd"], leg["symb"])

def get_high10(leg):
    return dom_high10(leg["code"]) if leg["market"] == "dom" else ovs_high10(leg["excd"], leg["symb"])

# ───────────────────────────────────────────────────────────
# 핵심 로직
# ───────────────────────────────────────────────────────────
def tier_target(dd):
    if dd <= -SET["t3"]: return 3, 60
    if dd <= -SET["t2"]: return 2, 50
    if dd <= -SET["t1"]: return 1, 40
    return 0, SET["baseline"]

TIER_NAME = {0: "정상", 1: "폭락 1단계", 2: "폭락 2단계", 3: "폭락 3단계"}

def fmt(amount, ccy):
    if ccy == "KRW":
        return f"{round(amount):,}원"
    return f"${amount:,.0f}"

def evaluate(name, cfg):
    spot_p = get_price(cfg["spot"])
    high10 = get_high10(cfg["spot"])
    lev_p  = get_price(cfg["lev"])

    spot_val = spot_p * cfg["spot"]["shares"]
    lev_val  = lev_p  * cfg["lev"]["shares"]
    pool     = spot_val + lev_val
    lev_pct  = lev_val / pool * 100 if pool else 0
    dd       = (spot_p - high10) / high10 * 100 if high10 else 0
    tier, target = tier_target(dd)

    target_val = pool * target / 100
    delta = target_val - lev_val   # + 면 레버 더 사야

    action = None
    if delta > cfg["min_action"]:
        amt = min(delta, spot_val)
        action = {"dir": "up", "amt": amt,
                  "sell": cfg["spot"]["name"], "sell_sh": amt / spot_p,
                  "buy":  cfg["lev"]["name"],  "buy_sh":  amt / lev_p}
    elif delta < -cfg["min_action"]:
        amt = min(-delta, lev_val)
        action = {"dir": "down", "amt": amt,
                  "sell": cfg["lev"]["name"],  "sell_sh": amt / lev_p,
                  "buy":  cfg["spot"]["name"], "buy_sh":  amt / spot_p}

    return {"spot_p": spot_p, "high10": high10, "lev_p": lev_p,
            "pool": pool, "lev_pct": lev_pct, "dd": dd,
            "tier": tier, "target": target, "action": action, "ccy": cfg["ccy"],
            "taxable": cfg["taxable"], "label": cfg["label"]}

def alert_text(r, prev_tier):
    e = "🔴" if r["tier"] > prev_tier else "🟢"
    arrow = "폭락 진입" if r["tier"] > prev_tier else "회복"
    ccy = r["ccy"]
    lines = [
        f"{e} <b>{r['label']} {TIER_NAME[r['tier']]} ({arrow})</b>",
        f"현재가 {fmt(r['spot_p'], ccy)} · 10일고점 대비 {r['dd']:+.1f}%",
        f"레버 {r['lev_pct']:.0f}% → 목표 {r['target']}%",
    ]
    a = r["action"]
    if a:
        lines.append("")
        lines.append(f"▶ {a['sell']} {a['sell_sh']:.2f}주 매도")
        lines.append(f"▶ {a['buy']} {a['buy_sh']:.2f}주 매수")
        tag = " · ⚠️22% 과세누적" if r["taxable"] else " · 비과세"
        lines.append(f"≈ {fmt(a['amt'], ccy)} 교체{tag}")
        lines.append("※ 본장에서 시장가로 ①매도→②매수 빠르게.")
    else:
        lines.append("(조정 금액이 작아 액션 없음)")
    return "\n".join(lines)

def status_text():
    out = ["📊 <b>현재 상태</b>"]
    for name, cfg in POOLS.items():
        try:
            r = evaluate(name, cfg)
            out.append(
                f"\n<b>{r['label']}</b>  {TIER_NAME[r['tier']]}\n"
                f"  현재가 {fmt(r['spot_p'], r['ccy'])} / 고점 {fmt(r['high10'], r['ccy'])} ({r['dd']:+.1f}%)\n"
                f"  레버 {r['lev_pct']:.0f}% → 목표 {r['target']}%"
            )
        except Exception as ex:
            out.append(f"\n<b>{cfg['label']}</b>  조회 실패: {ex}")
    return "\n".join(out)

# ───────────────────────────────────────────────────────────
# 주기 체크 + 단계 변화 시에만 알림
# ───────────────────────────────────────────────────────────
def run_check():
    for name, cfg in POOLS.items():
        try:
            r = evaluate(name, cfg)
        except Exception as ex:
            print(f"[{name}] 조회 실패:", ex)
            continue
        key = f"tier_{name}"
        prev = STATE.get(key, 0)
        if r["tier"] != prev:
            tg_send(alert_text(r, prev))
            STATE[key] = r["tier"]
            save_state(STATE)
        print(f"[{name}] dd={r['dd']:+.1f}% tier={r['tier']} lev={r['lev_pct']:.0f}%")

# ───────────────────────────────────────────────────────────
# 텔레그램 명령 처리 (/status, /start, /help)
# ───────────────────────────────────────────────────────────
def handle_commands():
    offset = STATE.get("tg_offset", 0)
    for u in tg_get_updates(offset):
        offset = u["update_id"] + 1
        text = (u.get("message", {}).get("text") or "").strip().lower()
        if text.startswith("/status"):
            tg_send(status_text())
        elif text.startswith("/start"):
            tg_send("✅ tebestck 봇 작동 중. /status 로 현재 상태 확인.")
        elif text.startswith("/help"):
            tg_send("/status 현재 상태\n/help 도움말\n단계가 바뀌면 자동으로 알림이 와.")
    STATE["tg_offset"] = offset
    save_state(STATE)

# ───────────────────────────────────────────────────────────
# 메인 루프
# ───────────────────────────────────────────────────────────
def main():
    tg_send("✅ tebestck 봇 시작됨.\n" + status_text())
    last_check = 0
    while True:
        handle_commands()                 # 명령은 ~10초마다 응답 (long-poll)
        if time.time() - last_check >= CHECK_INTERVAL:
            run_check()                    # 시세 체크는 5분마다
            last_check = time.time()

if __name__ == "__main__":
    main()
