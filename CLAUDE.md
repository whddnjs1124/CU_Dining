# CLAUDE.md

Columbia Dining 대시보드. dining.columbia.edu를 30분마다 스크레이핑해서 정적 JSON으로 굽고, 한 페이지 대시보드로 보여준다.

기획은 `docs/PRD.md`, 설계와 실패 모드는 `docs/HLD.md`.

## 구조

```
index.html                  앱 전체 (HTML + CSS + JS 인라인, 빌드 없음)
dining.json                 스크레이퍼 산출물, 커밋됨
scrape.py                   Playwright 스크레이퍼 (+ --recon 모드)
test_scrape.py              파서 회귀 검증
fixtures/globals.json       마지막으로 캡처한 원본 페이로드 (스크레이프마다 갱신)
.github/workflows/scrape.yml
```

## 데이터가 어디서 오는가

DOM을 파싱하지 않는다. 모든 dining 페이지가 **인라인 JS 전역변수 3개**로 전체 데이터셋을 실어 보낸다:

| 전역변수 | 내용 |
|---|---|
| `dining_nodes` | 로케이션 16곳: 이름, 경로, 좌표, `crowd_id`, `open_hours_fields` |
| `dining_terms` | 분류 사전: `types`(끼니), `stations`, `dietary_prefs`, `ingredients` |
| `menu_data` | 향후 7일치 메뉴 전체 (모든 로케이션 통합) |

그래서 **한 페이지 로드가 전체 스크레이프다.** 16번 돌 필요 없다. 이건 그들의 AngularJS 앱이 먹는 것과 같은 JSON이라 CSS/마크업이 바뀌어도 안 깨진다.

## 규칙

**의존성을 늘리지 않는다.** 프론트는 프레임워크·번들러·CDN 스크립트·웹폰트 없이 `index.html` 한 파일이다. Python 쪽은 `playwright`, `beautifulsoup4`, `pytest`가 전부. 새 패키지를 추가하려면 그게 없으면 못 하는 일인지 먼저 따져볼 것.

**디자인은 해시계다.** 자세한 건 `docs/HLD.md`의 디자인 절. 요약: 황동색은 NOW 마커 전용(다른 데 쓰지 말 것), Columbia Blue는 창백한 Pantone 290이지 네이비가 아님, 리본 행 높이는 고정이라 `shortName()`이 이름을 두 줄 안에 넣어야 함.

**깨진 결과를 절대 커밋하지 않는다.** 스크레이프가 실패하거나 로케이션이 `MIN_LOCATIONS`(8) 미만이면 non-zero exit로 죽어라. 조금 오래된 정확한 데이터 > 방금 만든 반쪽 데이터. 프론트는 stale을 배너로 알리지만, 틀린 데이터는 알릴 방법이 없다.

**단, "직전 실행보다 적으면 실패" 같은 래칫은 쓰지 말 것.** 컬럼비아가 카페 하나를 없애는 순간 영구 잠금이 된다(16→15가 실패 → 아무것도 안 쓰임 → 다음 실행도 16과 비교 → 영원히 실패). 그리고 **"열린 곳이 있는가"로 검사하면 안 된다** — 방학엔 전부 닫히는 게 정상이다. 둘 다 `test_scrape.py`에 회귀 테스트로 박아뒀다.

**"하루"는 자정이 아니라 오전 6시에 시작한다.** JJ's Place는 새벽 10시까지 열고 학생은 새벽 2시에 검색한다. `statusOf()`의 `+2400` 랩어라운드와 리본의 `DIAL_FROM`/`dialHHMM()`이 같은 6am→6am 프레임을 쓴다. 한쪽만 고치면 새벽에 히어로가 빈 채로 뜬다.

**시간은 항상 `America/New_York`.** 서버(UTC)도 사용자 로컬도 아니다. Python은 `zoneinfo`, JS는 `Intl.DateTimeFormat`의 `timeZone` 옵션. 날짜 라이브러리 추가 금지.

**열림/닫힘은 프론트에서 계산한다.** `dining.json`에는 시간대만 담고 상태 문자열은 담지 않는다. JSON이 30분 stale해도 상태 표시는 정확해야 하니까.

**`index.html`의 `statusOf()`는 컬럼비아 앱의 `getOpenStatus()` 포팅이다.** HHMM 정수 연산, 심야 영업의 `+2400` 랩어라운드까지 의도적으로 동일하다. "더 깔끔하게" 리팩터하지 말 것 — 공식 사이트와 답이 갈리는 순간 신뢰를 잃는다. 단 하나 다른 점: `now`를 기기 로컬이 아니라 뉴욕 시각으로 잡는다 (그쪽 앱의 버그).

**로직을 고쳤으면 검증도 같이.** 파서는 `pytest`, 프론트 상태 계산은 `index.html?selftest`. 이 둘이 "컬럼비아가 뭔가 바꿨다"를 잡아내는 유일한 장치다.

**컬럼비아의 산문형 시간 안내는 계산 가능하면 출력하지 않는다.** 그쪽 데이터가 자기모순인 경우가 있다(Everett은 산문에 "8 a.m. - 3 p.m.", 구조화 데이터엔 14:00). 둘 다 보여주면 이 페이지가 틀린 것처럼 보인다. 상태를 뽑은 것과 같은 숫자로 시간대를 출력하고, 산문은 계산할 게 없을 때만.

**크롤은 예의 있게.** 전체 스크레이프가 페이지 로드 1건이라 30분 주기로도 하루 48요청뿐이다. 그보다 잦은 cron 금지. Cloudflare 뒤에 있는 남의 사이트다.

## 명령어

```bash
pip install -r requirements.txt && playwright install --with-deps chromium

python scrape.py --recon <url>   # XHR 덤프 + 렌더된 HTML을 fixtures/에 저장
python scrape.py                 # dining.json 생성
pytest                           # 파서 회귀 검증
python -m http.server 8000       # 프론트 확인
```

- `index.html?selftest` — 상태 계산·리본 좌표·이름 축약 어서션을 브라우저에서 실행.
- `index.html?now=2026-08-10T12:00:00-04:00` — 현재 시각 오버라이드. 여름방학엔 전부 닫혀 있으니 상태 전환을 보려면 필수.

## 지금 상태 (2026-08-08)

여름방학이라 **`menu_data`가 비어 있다.** 다이닝홀 전부 `Closed for Summer`, Blue Java 3곳만 평일 운영. 첫 Fall 메뉴 노드는 **2026-09-04**자다.

즉 시간/영업상태 파이프라인은 지금 검증 완료지만, **메뉴 렌더링은 9월 4일에 실물로 확인해야 한다.** `scrape.py`의 `build_item()`에 `ponytail:` 코멘트로 표시해 뒀다 — 끼니 항목의 필드명은 앱 JS의 필터 구현에서 역산한 것이라 실제와 다를 수 있다.

## 알아둘 것

dining.columbia.edu는 **Cloudflare JS 챌린지 뒤에 있다.** `curl`, `requests`, 서버 사이드 fetch는 전부 403 `Just a moment...`를 받는다. 헤드리스 브라우저가 아니면 데이터를 못 가져온다 — 스크레이퍼를 "가볍게" 만들겠다고 HTTP 클라이언트로 갈아끼우려는 시도는 실패한다.
