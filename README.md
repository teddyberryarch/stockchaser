# tebestck_bot — 레버리지 스윙 추적 (GitHub Actions, 비용 0)

야후 시세로 하이닉스/샌디스크 본주를 5분마다 보고, 10거래일 고점 대비 낙폭이
-10/-20/-30% 닿으면 텔레그램으로 "○○○ 얼마어치 팔고/사라" 알림.
GitHub Actions cron으로 돌아서 서버 상주 없음 → 비용 0. 자동주문 없음(알림만).

## 구성
- main.py        : 한 번 실행 → 체크 → 알림 → data.json/state.json 저장 → 종료
- holdings.json  : 보유값(주수·레버평가액). 거래하면 여기만 고치면 됨(웹/직접)
- .github/workflows/check.yml : 5분마다 main.py 실행
- index.html     : GitHub Pages 대시보드. data.json 읽어서 카드로 표시
- state.json/data.json : 봇이 자동 생성·커밋 (직접 안 건드림)

## 셋업
1) Secrets (repo → Settings → Secrets and variables → Actions → New secret)
   - TG_TOKEN    : 텔레그램 봇 토큰
   - TG_CHAT_ID  : 7958777842
2) Actions 권한 (repo → Settings → Actions → General → Workflow permissions
   → "Read and write permissions" 체크) — 봇이 state/data 커밋하려면 필요
3) Pages (repo → Settings → Pages → Source: main, 폴더 /(root))
   → https://teddyberryarch.github.io/stockchaser/ 에서 대시보드
4) 첫 실행: Actions 탭 → "tebestck check" → Run workflow (수동) 로 바로 테스트

## 거래하면
holdings.json 의 spot_shares / lev_ref / spot_ref 만 갱신.
- spot_shares : 현물 주수
- lev_ref     : 현재 레버 평가액(현재가×주수)
- spot_ref    : lev_ref 적은 시점의 본주가 (레버 추정 기준점)

## 메모
- 야후는 15~20분 지연. 단계 알림 용도엔 무방.
- 샌디스크는 달러 기준 판정(환율 노이즈 제거). 비중%는 통화 무관 동일.
- cron은 5분 주기지만 GitHub Actions 큐 사정으로 몇 분 더 늦을 수 있음(무방).
