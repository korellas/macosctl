# macosctl — 설계 계약

### D2. policy와 소유권

`sbin/macosctl-helper`가 기록하듯, `macosctl-helper`은 NOPASSWD sudo로
root 실행된다. 따라서 **사용자가 쓸 수 있는 것을 하나도 읽으면 안 된다** — 읽는 순간,
그 파일을 고칠 수 있는 프로세스가 곧 무비밀번호 root를 얻는다.

**D2-1.** 정책은 파일 하나 `/etc/macosctl/policy.json`이다. `root:wheel`, group·other
쓰기 비트 없음.

```json
{
  "schema": 1,
  "label_prefix": "com.korellas.",
  "label_exceptions": { "webtop": "com.webtop" },
  "service_user": "example",
  "service_users": {},
  "groups": ["infra", "mtplx", "mlx", "llm", "edge", "dashboard"]
}
```

**D2-2.** `macosctl-helper`은 `schema`와 label mapping(`label_prefix`, `label_exceptions`)만
검증하고 **모르는 키는 무시**한다. 엔진 쪽 키가 늘 때마다 macosctl-helper을 재배포해야 하거나
거부 사고가 나면 안 된다. 단 label mapping의 **부재·형식 오류는 fail-closed**를
유지한다. 엔진(validate/doctor/new)은 전체를 읽는다 — 원천이 갈라지면
"validate는 통과하는데 macosctl-helper은 거부"하는 판정 불일치가 생긴다.

**D2-3.** 읽기는 **fd 기반**이다. `/etc/macosctl`를 디렉터리 FD로 열고 `O_NOFOLLOW`로
policy를 한 번 연 뒤, **같은 fd에서** `fstat`으로 regular file · uid/gid ·
mode · link count · 크기 상한을 검증하고 그 fd로 읽는다. 경로로 두 번 접근하지
않는다. 검사한 inode와 읽은 inode가 다를 여지를 남기지 않는다.

**D2-4.** `label_prefix`는 **전용 패턴**으로 검증한다. macosctl-helper의
`NAME_PATTERN`(`^[a-z0-9-]{1,40}$`)은 점을 배제하므로 `com.korellas.`가 항상
불합격이다 — v2가 "같은 방식으로 검증한다"고 적었으나 문자 그대로 구현하면 macosctl-helper이
전면 거부 상태로 출고된다. 접두사 패턴은 역DNS 라벨 문법을 따르고 마침표로 끝난다.
`label_exceptions`의 **값들도** 완성된 라벨 문법으로 검증한다.

**D2-5.** `service_user`는 전역 기본 사용자의 정본이고, `service_users`는 서비스별
예외의 **유일한 정본**이다. 서비스 선언의 `user`는 정확한 서비스 이름과 요청 사용자가
root 소유 `service_users` 매핑과 모두 일치할 때만 허용한다. 매핑 부재·형식 오류·불일치,
존재하지 않는 서비스 매핑은 **명시적 치명 오류**로 거부한다. `user`가 없는 서비스는
기존처럼 `service_user`를 쓴다. `macosctl.toml`, 조각의 top-level·scaffold, 드롭인에는
`user`를 허용하지 않는다. plist의 `UserName`과 `HOME`은 이 정책 바인딩 결과로 계산한다.

**D2-6.** 소유권 배치를 못박는다.

| 경로 | 소유·권한 | 쓰는 쪽 |
|---|---|---|
| `/etc/macosctl` | `root:wheel 0755` | `install.sh` (root) |
| `/etc/macosctl/policy.json` | `root:wheel 0644` | `install.sh` (root) |
| `/etc/macosctl/macosctl.toml` (`[defaults]`) | 서비스 사용자 | 사람 |
| `/etc/macosctl/conf.d/` 및 하위 전체 | 서비스 사용자 | 사람·프로젝트 |
| `/var/db/macosctl/inventory` | `root:wheel 0644` | `macosctl apply` (root) |
| `/var/db/macosctl/state` | `root:wheel 0644` | `macosctl-helper` (root) |

`conf.d/`가 사용자 소유이므로 **`link`/`unlink`/`edit`/`mask`에 sudo가 필요 없다.**
마찰이 우회를 부른다는 원칙과 일치한다. 안전한 이유: apply의 신뢰 모델은 오늘의
사용자 소유 `services.toml`과 동일하고, D2-5의 정확한 service/user 인가와 라벨 패턴이 경계를
지킨다. **`macosctl-helper`의 소유권 검사는 `/etc/macosctl`와 `policy.json`에만 적용된다** —
macosctl-helper은 `conf.d`를 절대 읽지 않는다.

읽기를 열고 쓰기만 root로 막는 것은 인벤토리와 같은 규약이다 — 일반 사용자의
`macosctl status`/`doctor`/`--dry-run`이 통째로 실패하면 안 된다.

### D3. 물리 메모리는 감지한다

물리 메모리는 `sysctl -n hw.memsize`로 감지한다. 감지 실패 시에는 예산 경고만
건너뛴다.

### D4. 병합 의미론 — **가장 먼저 확정한다**

D5의 lifecycle도 D7의 스키마도 D14의 JSON 계약도 병합 결과의 *형태*를 소비한다.
D4가 흔들리면 셋 다 다시 쓴다.

**D4-1 정체성.** 정체성 키는 `name`이다. 드롭인이 `name` 또는 `label`을 바꾸는 것은
치명 오류다. 허용하면 구 라벨이 desired set에서 빠져 **은퇴(bootout, 서빙 중단)**
+ 새 라벨 생성이 된다.

**D4-2 계층.** 둘이다.

```
/etc/macosctl/
  macosctl.toml                                      [defaults]
  conf.d/
    30-ai.toml         -> ~/ai/services.toml    조각 — [[service]] 선언
    30-webtop.toml     -> ~/git/webtop/service.toml
    70-local.toml                               이 머신만의 조각 (드물다)
    webtop.d/
      70-port.toml                              드롭인 — 오버라이드
      90-mask.toml                              드롭인 — mask (D5-1)
```

**D4-3 순서.** 각 계층 내부는 파일명 사전순, 드롭인이 조각을 이긴다.

**D4-4 조각 간 충돌.** 조각 **간** 같은 `name` 재선언은 치명이며, 진단이 두 파일을
모두 지목한다. 오버라이드는 드롭인으로만 한다. (SMF가 같은 계층의 property 충돌을
last-wins로 처리하지 않고 서비스를 maintenance로 두는 것과 같은 취지 — 1R Codex)

**D4-5 필드 규칙.** **전부 replace**다. 스칼라도 리스트도 같다 — `exec`,
`depends_on`도 통째 교체. systemd의 리스트 누적 규약은 빌리지 않는다.

**D4-6 예외.** `env` 하나만 키 단위로 병합한다.

**D4-7 제거.** 명시적 `unset = ["env.FOO", "depends_on"]`. TOML에 null이 없으므로
빈 문자열 센티널(`KEY = ""`)은 쓰지 않는다 — 빈 값과 구분 불가다.

**D4-8 드롭인은 서비스를 만들 수 없다.** 대상 없는 드롭인은 **경고**이지 치명이
아니다. 치명으로 두면 `macosctl unlink` 후 apply가 영구히 막히는 교착이 난다(1R Fable):
서비스를 은퇴시킬 유일한 경로가 apply인데 그 apply가 드롭인 때문에 멎는다.
내용상 서비스를 만들 수 없으므로 조용한 생성 위험도 없다.

→ **강등의 대가를 doctor가 받는다.** 프로젝트 조각이 서비스 `name`을 바꾸면 그
서비스의 머신 드롭인이 통째로 고아가 되는데, 고아가 된 것이 **mask 드롭인이면
"이 머신에선 안 띄운다"는 결정이 조용히 풀린다** — 다음 apply가 새 이름의 서비스를
기동한다. apply의 경고 한 줄은 휘발된다. 따라서 **doctor에 `orphan-dropin`을 상시
항목으로 둔다**(드리프트로 exit 1). 경고는 즉시성, doctor는 지속성을 맡는다.
(3R Fable)

**이것이 닫는 것은 가시성 구멍이지 기동 자체가 아니다** (채점 Codex). 이름 변경 후 첫
apply에서 새 이름 서비스는 먼저 뜨고 doctor는 그 뒤에 exit 1을 낸다. 이름에 키잉된
mask의 본질이며 systemd도 같다 — "풀린 사실이 영구히 묻히는 것"을 막을 뿐 "풀리는 것"을
막지는 않는다. 수용하되 표현을 정확히 둔다.

**D4-9 조각 유실.** 조각을 **읽을 수 없으면** 치명이며 아무것도 바꾸지 않는다
("뺐다"와 "읽을 수 없다"를 구분한다). 심링크가 깨졌다고 그 프로젝트 서비스를 전부
은퇴시키면 안 된다. 단 **`macosctl unlink`와 `macosctl link --list`는 loader를 거치지 않고
`lstat`만으로 동작한다** — 그러지 않으면 복구 명령 자체가 같은 치명에 걸려 탈출구가
없다(1R Codex/Fable).

**D4-10 immutable merged model.** 모든 파일을 **한 번만 읽어** 하나의 불변 모델을
만든다. validate·render·`macosctl cat`·`macosctl manifest --json`이 같은 객체를 본다. 필드별
provenance(어느 파일이 이 값을 줬는가)를 보유한다.

**D4-11 스키마 버전.** 조각·드롭인·policy에 `schema = 1`. 모르는 버전은 치명.
조각이 남의 레포로 흩어진 **뒤에는** 포맷 변경이 전 레포 순회가 된다 — 지금 넣는
비용은 0이고 나중 비용은 크다.

**D4-12 파일명.** 숫자 접두사는 조각 `10-`~`40-`, 로컬 `60-`~`90-`. `macosctl link`가
접두사 없는 파일명에 `30-`을 부여하고, **대상 basename이 이미 있으면 거부**한다
(`--name`으로 지정). systemd의 같은-basename 규칙(상위 우선순위 디렉터리가 통째로
대체)은 병렬 검색 경로 전제라 단일 `conf.d`에는 빌려올 수 없다 — 거부가 맞다.

### D5. mask — 은퇴에서 분리한다

**D5-1 표현.** mask는 `conf.d/<name>.d/90-mask.toml`에 `managed = false`를 쓰는
것이다. **`90-mask.toml`은 예약 파일명이다.** `macosctl mask`는 canonical 내용
(`schema` + `managed = false` + 주석)만 쓰고, 그 형태가 아닌 기존 파일이 있으면
**덮어쓰지 않고 거부**한다. `macosctl unmask`도 canonical 내용이 아니면 삭제하지 않는다 —
사람이 손으로 쓴 다른 오버라이드를 mask 명령이 날리면 안 된다. (3R Codex)

**D5-2 plan 범주.** apply가 **별도 범주**로 다룬다. 은퇴와 정확히 다르다.

| | 은퇴 (정체성 말소) | mask (정체성 보존) |
|---|---|---|
| bootout | O | O |
| `enable` 복원 | O | **X** |
| plist 삭제 | O | O |
| 인벤토리 | 항목 제거 | **`lifecycle: "masked"`로 유지** |
| state 마크 | `state.clear` | **보존** |

근거: `enable` 복원과 기존 `state.clear`는 **진짜 은퇴엔 옳고 mask엔 정확히
반대**다. `macosctl disable X → macosctl mask X → macosctl unmask X`면 운영자가 내려둔 서비스가
자동 기동한다(2R Fable).

→ **전이.** masked 서비스의 **조각 자체가 unlink되어** 병합 결과에서도 사라지면,
그 항목은 masked → **은퇴(정체성 말소)로 전이**한다. plan 범주 판정이 매 apply마다
병합 결과 존재 여부를 다시 보므로 구현 비용은 0이다. 이 문장이 없으면
`lifecycle=masked` 항목이 영원히 인벤토리에 남는 구현이 나온다. (3R Fable)

**D5-3 인가.** **`macosctl-helper`이 인벤토리의 `lifecycle`을 읽는다.** masked 항목에는
plist가 없으므로 현행 인가 사슬(멤버십 → plist 존재 → sha256 일치)을 통과할 수
없다 — 그대로 두면 stop/disable조차 "plist가 없다"로 거부된다(3R 양쪽이 독립적으로
지적, 실측 확인).

| 동사 | masked일 때 | 이유 |
|---|---|---|
| `start` | **거부** | mask의 존재 이유 |
| `restart` | **거부** | 같음 |
| `enable` | **거부** | 운영자의 boot 차단 의도를 보존한다 |
| `stop` | 허용 | 이미 내려가 있어 무해하고, 의도 기록은 남겨야 한다 |
| `disable` | 허용 | 권한을 낮추는 방향뿐이다 |

masked 항목은 해시 대조 대신 **부재를 확인한다.** "plist가 없으니 안전하다"는 논거를
쓰면서 검사를 *생략*만 하면 논거와 규칙이 어긋난다 — masked 구간에 **다른 root 절차가
같은 라벨의 plist·job을 설치하면**, NOPASSWD `macosctl stop/disable`이 svc가 설치하지 않은
데몬을 제어하게 된다. 권한 상승은 아니지만 08-15 스펙이 인가를 인벤토리로 바꾼 이유
(provenance)가 masked 구간에서만 다시 열리고, 서비스 거부 경로가 된다 (채점 양쪽).

따라서 masked 인가는 셋을 **확인**한다:

1. `lifecycle == "masked"` **이고** `sha256 == null` — 조합이 어긋나면 거부
2. 예상 plist 경로가 **실제로 부재**한다 — 존재하면 거부
3. 같은 라벨의 launchd job이 **없다** — 있으면 거부

2·3에 걸리면 정상 masked 상태가 아니다. 거부하고 doctor에 `masked-but-installed`로
보고한다. 그리고 masked 경로는 active용 `perform()`을 그대로 부르지 않고 **전용 분기로
분리**한다 — 두 상태의 전제가 다르므로 한 함수가 둘을 다루면 한쪽 가정이 조용히 샌다.

**masked 항목의 `sha256`은 `null`이다.** 마지막 활성 plist의 해시를 남기면 v2 리더
구현이 "그 해시를 검증해야 하나"에서 갈라진다. plist가 없으므로 검증할 대상도 없고,
`null`이 그 사실을 스키마 수준에서 말한다. (3R Fable)

**D5-4 발효 시점.** `macosctl mask`는 드롭인을 쓸 뿐이며 서비스는 계속 돈다. 출력에
`적용하려면 sudo macosctl apply`를 명시한다. `--now`는 범위에서 뺀다(YAGNI).

**D5-5 표기.** `macosctl status`에 `masked`를 추가하고, 조각의 `managed=false`
(프로젝트가 "아직 준비 안 됨"이라 선언한 것)와 구분해 표기한다. 판별은 provenance —
`managed=false`의 출처가 `<name>.d` 드롭인이면 masked다.

**D5-6 apply 출력.** `- 은퇴`와 `- 정지(mask)`를 구분한다. `macosctl mask` 한 번이
은퇴처럼 보이면 사람을 놀라게 한다.

**D5-7 조용한 부활 로깅.** `enable`이 실제로 disabled를 뒤집은 경우 로그를 남긴다.
apply가 조용히 되살리므로, state 유실 + 오버라이드 잔존이라는 이중
장애가 보이지 않는다. (2R Fable)

### D6. `working_directory`를 서비스 단위 선택 필드로 승격

서비스의 `working_directory`가 없으면 `[defaults]` 값을 쓴다.

### D7. 인벤토리 v2

**D7-1 스키마.**

```json
{
  "version": 2,
  "labels": {
    "com.korellas.gateway": {
      "sha256": "…", "source": "30-ai.toml", "lifecycle": "active"
    },
    "com.webtop": {
      "sha256": null, "source": "30-webtop.toml", "lifecycle": "masked"
    }
  }
}
```

`lifecycle` ∈ `active | masked`. `source`는 조각 파일명. `masked`의 `sha256`은
`null`(D5-3).

얻는 것 넷: 은퇴 사유를 설명할 수 있고(`30-ai.toml에서 사라졌다`), 은퇴와 mask를
구분할 수 있고, 조각을 읽지 못할 때 그 조각의 서비스만 정확히 보호할 수 있고,
`macosctl status`에 출처 열이 붙는다.

**D7-2 unknown version 거부.** fail-closed. v1(`{label: sha256}`)만 읽어 승격한다.

**D7-3 마이그레이션 시점.** **`is_noop` 조기 반환보다 앞에서**, apply writer 락
(D10) 안에서, 단독으로 수행한다. 무변경 apply도 마이그레이션은 한다. v2가
"다음 apply가 v2로 다시 쓴다"고 적었으나 `cmd_apply`의 no-op
조기 반환 때문에 성립하지 않았다(1R Codex, 실측).

**D7-4 내구성.** v1 백업(`/var/db/macosctl/inventory.v1.bak`)을 남기고, 임시파일 +
atomic rename + durable marker + 디렉터리 fsync.

**D7-5 배포 순서.** **v1·v2 양쪽을 읽는 `macosctl-helper`을 먼저 깔고, 그다음 apply가 v2를
쓴다.** 역순이면 구 macosctl-helper이 v2를 파싱하지 못해(값이 문자열→객체로 바뀌므로 해시
비교가 항상 불일치) **모든 제어가 막힌다.**

**D7-6 롤백 — 도구는 만들지 않는다.** 구판 `macosctl apply --adopt`가 이미 다운컨버터다.
설치된 plist 실물을 스캔해 v1 인벤토리를 새로 쓰며, 은퇴 대상까지
승계한다. 절차 순서를 못박는다:

```
① /var/db/macosctl/inventory.v1.bak 복원      ← 읽히는 v1을 즉시 만든다
② 구판 svc·macosctl-helper 설치                    ← 이 시점부터 제어가 산다
③ sudo macosctl apply --adopt                 ← 파일시스템 실물로 재승계 (정본)
```

두 리뷰어가 서로 다른 구간을 걱정했고 순서를 붙이면 충돌하지 않는다 — Codex는
"구 macosctl-helper을 깐 순간부터 제어가 죽는 구간"(①이 해소), Fable은 "복원된 인벤토리가
실제와 어긋나는 것"(③이 해소). **`.v1.bak`은 정본이 아니라 다리이고, 정본은
`--adopt`다.**

알려진 손실: 롤백하면 masked 정보가 사라진다. masked 서비스는 plist가 없어 adopt에도
잡히지 않는데, 그건 구판 semantics(`managed=false` 은퇴 상태)와 정합이라 수용한다.

**D7-7 macosctl-helper이 읽는 범위.** `source`는 읽지 않는다. 인가 근거는 멤버십 + `sha256`
+ `lifecycle`뿐이다.

### D8. `macosctl cat` — 병합 결과와 출처

`systemctl cat`을 그대로 가져온다.

```
$ macosctl cat webtop
# /etc/macosctl/macosctl.toml  [defaults]
working_directory = "/Users/example/project"
# /etc/macosctl/conf.d/30-webtop.toml
name        = "webtop"
label       = "com.webtop"
port        = 7777
# /etc/macosctl/conf.d/webtop.d/70-port.toml
port        = 7778          # ← 30-webtop.toml:port 를 덮음
```

`macosctl doctor`에 **오버라이드 현황** 섹션을 붙인다(`systemd-delta`의 자리). 드리프트가
아니라 정상 오버라이드지만, 조각이 여럿이면 "지금 무엇이 이기고 있나"를 모르는 것
자체가 사고의 씨앗이다. 드리프트가 0건이어도 이 섹션은 출력한다.

`macosctl edit <name>`은 `conf.d/<name>.d/70-local.toml`을 `$EDITOR`로 연다. 파일이 없으면
주석 달린 템플릿을 만들어 준다. D2-6에 따라 sudo가 필요 없다.

**`macosctl cat`은 항상 비특권으로만 돈다.** apply와 cat은 조각 경로에 절대 쓰지 않고,
오류·출력에 조각 **원문을 인용하지 않는다**(경로와 위치만). root가 읽은 내용이 새는
표면을 닫는다 — 조각이 사용자 쓰기 가능한 심링크이므로 root 전용 파일을 걸어
읽어내는 경로를 원천 차단한다. (1R Fable)

### D9. `macosctl link` / `macosctl unlink`

```
macosctl link <path> [--name N]   조각을 conf.d에 심링크로 물린다
macosctl unlink <name>            conf.d의 심링크만 제거한다
macosctl link --list              물린 조각과 각각의 서비스 수
```

D2-6에 따라 셋 다 **sudo가 필요 없다.**

`link`는 물리기 전에 검증한다: 파일이 읽히는가, `schema`가 아는 버전인가,
`[[service]]`(과 `[scaffold]`)만 있는가, 병합했을 때 D4-4 충돌이 없는가, 대상
basename이 비어 있는가. 통과하지 못하면 심링크를 만들지 않는다 — 등록은 되는데
apply가 거부하는 상태를 만들면 사람은 다시 우회한다.

**`unlink`는 대상 파일을 절대 지우지 않는다.** `os.path.islink()`를 확인하고 심링크가
아니면 거부하며, 테스트로 고정한다. `systemctl disable`이 링크된 유닛을 지우는
사고를 그대로 피한다. 또한 D4-9에 따라 **loader를 거치지 않고 `lstat`만으로**
동작한다 — 깨진 링크에서도 탈출구가 있어야 한다.

`unlink`는 고아가 될 드롭인을 나열하고 경고한다. `--purge-dropins`로 함께 지울 수
있게 하되 **기본은 남긴다** — 조용한 데이터 손실을 만들지 않는다(2R Codex).

`link`/`unlink`가 매니페스트를 즉시 반영하지는 않는다. 반영은 `macosctl apply`다.
`kubectl`의 `--prune`이 파괴적 작업의 의도 전달을 위해 여전히 필수인 것과 같은
이유로, 삭제는 항상 별도의 명시적 명령을 지난다.

### D10. 락 — 둘로 나눈다

apply는 **서비스 단위** 공유 락을 사용한다. 그것만으로는 부족하다.

**두 apply가 서로 다른 인벤토리 스냅샷을 들고 교차 실행하면, 마지막 전체 쓰기가
상대의 `sha256`/`lifecycle`을 덮는다**(3R Codex). 반대로 apply 전체를 서비스 락으로
감싸면 서비스당 최대 30초 bootout 대기 동안 macosctl-helper이 블록돼 `macosctl stop`이 분 단위로
멎는다(2R Fable). 서로 반대 방향의 요구다. 락을 둘로 나누면 둘 다 만족한다.

| 락 | 보유 구간 | 경합 상대 |
|---|---|---|
| `/var/db/macosctl/lock` | 서비스 **하나**의 제어 구간 | `macosctl-helper` ↔ `apply` |
| `/var/db/macosctl/apply.lock` | `load → migration → plan → execute/adopt` **전체** | `apply` ↔ `apply` |

`macosctl-helper`은 서비스 단위 락에서만 경합하므로 분 단위로 멎지 않고, apply끼리는 완전히
직렬화된다. 획득 순서는 항상 `apply.lock` → `lock`이다(역순 금지 — 교착).

### D11. macosctl namespace 컷오버

`sudo ./install.sh --migrate-namespace`를 명시적으로 실행한다. 일반 설치나 dry-run은 구 namespace를 삭제하지 않는다.

이식 순서는 고정한다.

1. `/etc/svc`를 검증해 `/etc/macosctl`로 복사하고 `svc.toml`만 `macosctl.toml`로 이름을 바꾼다.
2. `/var/db/svc`를 `/var/db/macosctl`로 byte-identical 복사한다.
3. `/usr/local/sbin/macosctl-helper`와 `/etc/sudoers.d/macosctl`을 게시한다.
4. `/usr/local/bin/macosctl`을 새 레포의 `bin/macosctl`에 연결한다.
5. 새 namespace에서 `doctor`, `status --json`, `apply --dry-run`을 모두 통과시킨다.
6. 새 config/state가 구 트리와 동일하고 구 integration이 사전 검증한 inode일 때만 `/usr/local/bin/svc`, `/usr/local/sbin/svcctl`, `/etc/sudoers.d/svcctl`, `/etc/svc`, `/var/db/svc`를 제거한다.

새 destination이 다르거나, parent가 symlink이거나, source에 비정상 inode가 있거나, 어느 게이트든 실패하면 구 namespace를 그대로 둔 채 중단한다. migration tool 자체는 구 트리를 절대 삭제하지 않는다. 실제 제거 권한은 installer의 명시적 `--migrate-namespace` 경로에만 있다.

`tools/stage-config.py --out /tmp/macosctl-staging`과 installer `--dry-run --migrate-namespace`는 비특권·무변경 검증이다. launchd label, 서비스 plist bytes, 서비스 로그 경로는 namespace 변경 대상이 아니다.

원복은 구 namespace가 남아 있는 게이트 실패 시 새 integration만 제거하면 된다. Step 6까지 성공한 뒤 문제가 생기면 컷오버 직전 백업한 구 config/state/integration을 복원하고 구 CLI로 `apply --adopt`한다.

### D12. 은퇴의 범위 — 로그는 남긴다

dpkg의 remove/purge 구분을 따른다. 은퇴는 **bootout + `enable` 복원 + plist
제거 + 인벤토리에서 삭제**까지다. 다음은 건드리지 않는다:

- `~/Library/Logs/services/<name>.{out,err}.log` — 사고 조사에 필요하다
- 서비스 자신의 상태(모델 가중치, DB, 세션 캐시)

`macosctl purge`는 만들지 않는다. YAGNI — 필요하면 사람이 지운다. README에 명시.

### D13. 프로젝트 계약

README에 못박는다. 다른 레포가 macosctl에 서비스를 얹으려면 이 다섯을 지켜야 한다.

1. **런처는 foreground로 살아 있어야 한다.** 즉시 종료하면 launchd가 '죽었다'로 읽고
   KeepAlive가 무한 재기동한다.
2. **선언한 포트를 바인드해야 한다.** `macosctl status`의 running 판정이 리스너 소유권
   (PID 조상 관계, `SystemState.is_descendant`)이라서다.
3. **label은 policy의 접두사를 따른다.** 예외는 root 소유 `policy.json`에만 등록된다.
4. **조각에는 `schema`, `[[service]]`, 선택적 `[scaffold]`만.** `[defaults]`는
   머신 것이다. 서비스의 `user`는 root 소유 policy의 정확한 매핑이 있을 때만 쓴다(D2-5).
5. **`name`은 조각 하나에서만 선언한다**(D4-4). 남의 서비스를 고치려면 드롭인이다.

사용 절차:

```bash
cd ~/git/webtop
macosctl new webtop --port 7777 --group dashboard   # 블록 초안을 찍는다
$EDITOR service.toml                           # 사람이 검토해 확정
macosctl link ~/git/webtop/service.toml             # conf.d/30-webtop.toml (sudo 불필요)
macosctl apply --dry-run
sudo macosctl apply

# 이후
macosctl status / macosctl doctor / macosctl cat webtop
macosctl restart webtop
macosctl edit webtop        # 이 머신에서만 오버라이드 (남의 레포를 안 건드린다)
macosctl mask webtop        # 이 머신에서만 정지 → sudo macosctl apply 로 발효
```

### D14. `macosctl manifest --json` — 계약을 지금 고정한다

webtop은 `--services-manifest`로 **단일 TOML 파일**을 읽고 **mtime 기준으로 캐시**한다
. 병합이 도입되면 그것으로는
부족하고, D11-4가 그 파일에서 `[defaults]`를 들어낸다. UI 변경 전체는 미뤄도
**데이터 계약은 지금** 고정한다(1R Codex).

**필드는 webtop이 실제로 소비하는 것에서 나왔다.** 실측: `ServiceDef`가 쓰는 것은
`name` `label` `port` `group` `mem_budget` `depends_on` 여섯이고, `exec`·`env`는
소스 주석이 *"ignored here"*라고 명시한다. 그 여섯에 `managed`·`lifecycle`·`source`·
`working_directory`를 더한다.

```json
{
  "schema": 1,
  "defaults": { "log_dir": "/Users/example/Library/Logs/services",
                "throttle_seconds": 10 },
  "services": [
    { "name": "gateway", "label": "com.korellas.gateway", "port": 4000,
      "group": "edge", "managed": true, "lifecycle": "active",
      "mem_budget": null, "depends_on": ["database", "tracing"],
      "working_directory": "/Users/example/project", "source": "30-ai.toml" }
  ]
}
```

| 필드 | 타입 | nullable |
|---|---|---|
| `name` `label` `group` `source` `working_directory` | string | 아니오 |
| `port` | integer | 아니오 |
| `managed` | boolean | 아니오 |
| `lifecycle` | `"active"` \| `"masked"` | 아니오 |
| `mem_budget` | **integer (바이트)** | 예 |
| `depends_on` | string[] (빈 배열 가능) | 아니오 |

- **`mem_budget`은 바이트 정수다.** webtop 내부형이 이미 `Option<u64>`이고 `"44GB"`를
  `parse_size`로 변환한다 — 바이트로 내보내면 파서가 한쪽에서 사라진다(채점 양쪽).
- **`managed`와 `lifecycle`은 직교한다.** 조각이 `managed = false`로 선언한 것(프로젝트가
  "아직 준비 안 됨"이라 한 것)은 `managed:false, lifecycle:"active"`이고, mask 드롭인
  유래만 `lifecycle:"masked"`다. `managed:true` + `masked` 조합은 불가능하다.
- **`services` 배열은 병합 매니페스트 순서를 보존한다** (conf.d 파일명 사전순 → 파일 내
  선언 순서). webtop이 이 순서를 표시·부팅 순서로 쓴다(채점 Codex).
- **`exec`와 `env`는 노출하지 않는다** — 대시보드가 쓰지 않고, 노출하면 런처 인자와
  환경변수가 UI 경로로 새는 표면이 생긴다.
- 출력은 **결정론적**이다. 타임스탬프를 넣지 않는다(재현 가능한 diff).
- `stdout`은 JSON **뿐**이다. 진단·경고는 전부 `stderr`.
- exit code: `0` 성공, `2` 검증 치명(아무것도 출력하지 않음), `1` 그 밖의 오류.
- `schema`가 오르면 webtop이 거부하고 사람에게 알린다.

**전달 방식.** webtop이 mtime 캐시를 쓰므로 명령 실행으로 바꾸면 캐시 계약까지 새로
설계해야 한다. 그럴 필요가 없다 — `macosctl apply`가 끝날 때
**`/var/db/macosctl/manifest.json`(root:wheel 0644)을 함께 쓴다.** webtop은
`--services-manifest`가 가리키는 경로만 바꾸고 파서를 TOML→JSON으로 교체하면 되며,
mtime 캐시가 그대로 동작한다. `macosctl manifest --json`은 같은 내용을 stdout으로 내는
사람용 진입점이다.
