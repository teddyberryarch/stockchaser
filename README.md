# tebestck_bot — 레버리지 스윙 추적 봇

하이닉스/샌디스크 시세를 5분마다 보고, 10거래일 고점 대비 낙폭이
-10/-20/-30%에 닿으면 텔레그램으로 "현물 X주 팔고 레버 Y주 사라" 알림.
자동주문 없음(알림만). 본장 체결 기준.

## 1. 처음 한 번 채워야 할 것 (main.py POOLS)
- TIGER 하이닉스 레버 6자리 종목코드  ← 토스 종목정보에서 확인
- TRADR 샌디스크 2배 티커 + 거래소(EXCD: NAS 등)  ← 토스에서 확인
- 보유 주수(이미 입력됨, 거래 때마다 갱신)
- SK하이닉스 코드 000660 / 샌디스크 SNDK 는 입력 완료

## 2. 환경변수 (Railway Variables)
.env.example 참고. 키는 코드에 직접 넣지 말 것.
- KIS_APP_KEY / KIS_APP_SECRET : KIS Developers 신청 후 발급
- TG_TOKEN : BotFather 토큰 (노출됐던 건 /revoke 후 새 토큰)
- TG_CHAT_ID : 7958777842

## 3. KIS Developers
- apiportal.koreainvestment.com 에서 API 신청 (무료)
- '국내주식 기본시세/기간별시세' + '해외주식 기본시세/기간별시세' + 실시간시세 체크
- 발급된 APP KEY / SECRET 을 환경변수로

## 4. Railway 배포
- 새 프로젝트 → 이 폴더 푸시 → Variables에 위 4개 입력
- 서비스 타입: Worker (Procfile: worker)
- 로그에 [hynix]/[sandisk] dd/tier 찍히면 정상

## 5. 텔레그램 명령
- /status : 두 종목 현재 상태 즉시 확인
- /help

## 주의
- TR_ID/응답 필드는 KIS 문서 기준으로 작성. 혹시 필드명 다르면 그 부분만 수정.
- 샌디스크는 달러 기준으로 판정(환율 노이즈 제거). 비중%는 통화 무관 동일.
- 단계가 바뀔 때만 알림(5분마다 도배 안 함).
