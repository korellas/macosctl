# macosctl — 작업 규칙

launchd LaunchDaemon으로 도는 상시 서비스를 선언·등록·제어·진단하는 도구.
macOS 단일 머신용. 의존성은 Python 표준 라이브러리뿐이다 (3.11+, `tomllib`).

서비스 얹는 법은 [README.md](README.md), 설계 근거는 [docs/design.md](docs/design.md).

## 절대 규칙

- **`sbin/macosctl-helper`는 패키지를 import하지 않는다.** NOPASSWD sudo로 root 실행되므로
  사용자 쓰기 가능한 경로를 하나도 읽으면 안 된다. 읽는 순간 그 파일을 고칠 수
  있는 프로세스가 곧 무비밀번호 root를 얻는다.
- **plist 생성 출력 바이트를 바꾸지 않는다.** 서비스 plist가 한 바이트라도 달라지면
  전 서비스가 재기동되고 모델 가중치 재로딩으로 수 분간 서빙이 멎는다.
  예외는 `com.korellas.log-rotate` 하나뿐 (레포 경로를 내장하므로).
- `sudo macosctl apply` 실행 전엔
  항상 `--dry-run`으로 변경 0건(또는 예상한 변경만)인지 먼저 확인한다.
- **`launchctl`을 직접 부르지 않는다.** 제어는 전부 `macosctl` 동사를 지난다.
  직접 호출은 감사 기록이 남지 않고 인가와 인벤토리를 우회한다.

## 에이전트 규칙 — 명령 고르기

의미론의 정본은 `bin/macosctl`의 help 문자열이다. 모르면 `macosctl`(인자 없이)
또는 `macosctl <command> --help`를 먼저 읽는다. README/docs는 그 투영이다.

**runtime/boot 제어는 `sudo` 없이 실행한다.** CLI가 내부에서 NOPASSWD sudo로
`macosctl-helper`만 권한 상승시킨다. 서비스 실행 계정이 root인지와는 별개다.
`macosctl stop web worker`처럼 여러 이름을 지정할 수 있다.
설치와 실제 `apply`에는 `sudo`가 필요하다.

| 바꾸려는 축 | 명령 | 다른 축 | apply |
|---|---|---|---|
| runtime (지금) | `start` `stop` `restart` | boot 설정 보존 | 불필요 |
| boot policy (재부팅 뒤) | `enable` `disable` (`--now`로 함께) | 지금 상태 보존 | 불필요 |
| 정의 (plist) | `link` `unlink` `edit` `mask` `unmask` | — | **필요** |

**runtime/boot 제어에는 apply가 필요 없다.** `apply`는 선언에서 생성되는 plist가
바뀐 서비스만 재기동하므로, 런처 스크립트나 런처가 읽는 설정을 고친 경우엔
`macosctl restart <name>`이 정답이다.

## 테스트

pytest 없이 돈다.

```bash
for f in tests/test_*.py; do printf '%-28s ' "$f"; python3 "$f" 2>&1 | tail -1; done
```

**새 테스트는 파일 끝 러너 블록 *앞*에 둔다.** 뒤에 두면 정의 전에 `globals()`를
순회해 조용히 실행되지 않는다.

`launchctl print` 반환코드: `0`=존재, `113`=없음, 그 밖=오류. 테스트 페이크는
`113`을 써야 한다 — 임의의 non-zero는 fail-closed로 30초 폴링된다.
