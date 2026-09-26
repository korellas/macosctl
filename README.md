# macosctl

macOS launchd 상시 서비스의 선언·등록·제어·드리프트 진단.

주 명령과 유일한 CLI 진입점은 `macosctl`이다. 구 `svc` 호환 alias는 두지 않는다.

**어느 레포든 자기 서비스를 선언해 이 머신에 얹을 수 있다.** 선언은 프로젝트
레포에 남고, 머신은 그것을 심링크로 물어 병합한다.

---

## 서비스 얹기

### 1. 조각을 만든다 — 프로젝트 레포 안에서

```bash
cd ~/git/내프로젝트
macosctl new 내서비스 --port 7777 --group dashboard
```

`macosctl new`는 **파일을 쓰지 않고 블록 초안만 출력한다.** 검토해서 레포에 저장한다:

```toml
# ~/git/내프로젝트/service.toml
schema = 1

[[service]]
name        = "내서비스"
label       = "com.korellas.내서비스"
port        = 7777
group       = "dashboard"
exec        = ["/Users/example/git/내프로젝트/run.sh", "foreground"]
depends_on  = []
```

### 2. 머신에 물린다

```bash
macosctl link ~/git/내프로젝트/service.toml     # sudo 불필요
```

`/etc/macosctl/conf.d/30-service.toml`이 원본을 가리키는 심링크로 생긴다.
**원본은 프로젝트 레포에 그대로 남는다.**

### 3. 반영한다

```bash
macosctl apply --dry-run     # 무엇이 바뀌는지 먼저 본다
sudo macosctl apply
```

일부 서비스만 반영하려면 검증은 전체 설정에 대해 유지하면서 `--service`를
반복한다. 예: `sudo macosctl apply --service web --service worker`. 선택하지 않은
서비스와 전역 applied manifest는 변경하지 않는다.

### 4. 이후

```bash
macosctl status                  # 사람이 읽는 반응형 표
macosctl status --json           # Webtop이 읽는 안정 JSON
macosctl restart 내서비스
macosctl logs 내서비스
```

---

## 서비스 제어 — quick reference

정본은 CLI다. `macosctl`을 인자 없이 실행하면 아래 선택 경계가 그대로 나오고,
`macosctl <command> --help`가 각 동사의 계약과 예제를 말해준다.

**runtime/boot 제어 명령 앞에는 `sudo`를 붙이지 않는다.** CLI가 내부에서
NOPASSWD sudo로 `macosctl-helper`만 호출한다. 서비스 프로세스의 실행 계정과
LaunchDaemon 제어 권한은 별개다. 설치된 sudoers 규칙이 호출자를 허용해야 하며,
설치와 실제 `apply`에는 `sudo`가 필요하다.

제어 명령은 여러 이름을 받는다. 예: `macosctl stop web worker`.
입력 순서대로 처리하고 중복 이름은 한 번만 실행한다. 이름은 실행 전에 모두
확인하며, 실행 중 실패하면 그 지점에서 멈추고 앞선 성공은 되돌리지 않는다.
옵션은 모든 대상에 적용하고 `--wait --timeout`의 상한은 서비스별로 적용한다.

| 하고 싶은 것 | 명령 | apply |
|---|---|---|
| 지금 띄운다 | `macosctl start <name>` | 불필요 |
| 지금 내린다 (재부팅하면 되살아남) | `macosctl stop <name>` | 불필요 |
| 다시 띄운다 (inactive여도 최종 active) | `macosctl restart <name>` | 불필요 |
| 재부팅 뒤에도 뜨게 (지금 상태 보존) | `macosctl enable <name>` | 불필요 |
| 재부팅 뒤에 안 뜨게 (지금 상태 보존) | `macosctl disable <name>` | 불필요 |
| boot 설정과 지금 상태를 함께 | `macosctl enable\|disable <name> --now` | 불필요 |
| 선언을 바꿨다 | `macosctl apply --dry-run` → `sudo macosctl apply` | **필요** |

`start`/`restart`에는 `--wait`(선언 포트를 잡을 때까지, `--timeout <초>`),
`restart`에는 `--force`(즉시 kill)와 `--recreate`(처음부터 재등록)가 있다.
둘은 서로 다른 복구 전략이라 함께 쓸 수 없다.

---

## 프로젝트가 지켜야 할 다섯

1. **런처는 foreground로 살아 있어야 한다.** 즉시 종료하면 launchd가 "죽었다"로
   읽고 `KeepAlive`가 무한 재기동한다.
2. **선언한 포트를 바인드해야 한다.** `macosctl status`의 running 판정이 리스너
   소유권(PID 조상 관계)이라서다.
3. **label은 policy의 접두사를 따른다** — `com.korellas.<name>`. 예외는 root 소유
   `/etc/macosctl/policy.json`에만 등록된다.
4. **조각에는 `schema`, `[[service]]`, 선택적 `[scaffold]`만.** `[defaults]`는
   머신 것이다. 서비스의 `user`는 root 소유 policy의 동일한 서비스 이름과 사용자
   매핑이 정확히 일치할 때만 쓸 수 있다.
5. **`name`은 조각 하나에서만 선언한다.** 남의 서비스를 고치려면 드롭인이다(아래).

`--group`은 `infra` `mtplx` `mlx` `llm` `edge` `dashboard` 중 하나 (표시용 분류).

서비스별 계정이 필요하면 `[[service]]`에 `user`를 선언하고 root 관리자가
`policy.json`의 `service_users`에 같은 서비스 이름과 사용자를 등록한다. 매핑이
없거나 이름·사용자가 다르면 설정 전체가 거부된다. `user`가 없는 서비스는 기존처럼
전역 `service_user`를 쓴다.

격리 계정이 머신 전역 로그 디렉터리에 접근할 수 없으면 서비스에 `log_dir`를
선언할 수 있다. 해당 서비스의 표준 출력과 오류 로그만 그 디렉터리로 이동하며,
생략한 서비스와 로그 회전 잡은 `[defaults].log_dir`를 계속 쓴다.

`process_type`은 `Background` `Standard` `Adaptive` `Interactive` 중 하나 (launchd
`ProcessType`과 동일). 생략 시 `Background`. CPU-바운드 멀티스레드 서비스는
`Standard`를 선언해야 한다 — `Background`는 QoS 때문에 E코어에만 갇혀, E코어
수보다 많은 스레드를 쓰는 서비스가 스핀락 기반 스레드풀에서 livelock에 빠질 수 있다.

---

## 남의 레포 건드리지 않고 바꾸기

프로젝트가 선언한 것을 **이 머신에서만** 다르게 하고 싶을 때.

```bash
macosctl edit 내서비스
```

`/etc/macosctl/conf.d/내서비스.d/70-local.toml`이 열린다. 여기 쓴 것이 조각을 이긴다.

```toml
schema = 1

[[service]]
name = "내서비스"
port = 7778          # 조각의 7777을 덮는다
```

무엇이 이기고 있는지는 언제든 볼 수 있다:

```bash
macosctl cat 내서비스
```

```
# /etc/macosctl/macosctl.toml [defaults]
working_directory = "/Users/example/project"
# /etc/macosctl/conf.d/30-내프로젝트.toml
port        = 7777
# /etc/macosctl/conf.d/내서비스.d/70-local.toml
port        = 7778          # ← 30-내프로젝트.toml:port 를 덮음
```

### 병합 규칙

- 조각(`conf.d/*.toml`) → 드롭인(`conf.d/<name>.d/*.toml`) 순, 각 계층 안에서는
  **파일명 사전순**. 나중 것이 이긴다
- **모든 필드가 replace**다 — 리스트(`exec`, `depends_on`)도 통째로 교체
- **`env`만 키 단위 병합**
- 제거는 명시적으로: `unset = ["env.FOO", "depends_on"]`
- 드롭인은 **서비스를 만들 수 없고** `name`·`label`을 바꿀 수 없다
- 파일명 접두사 관례: 조각 `10-`~`40-`, 머신 로컬 `60-`~`90-`

---

## 이 머신에서만 정지 — mask

```bash
macosctl mask 내서비스        # 드롭인을 쓴다
sudo macosctl apply           # 여기서 발효
```

**은퇴가 아니다.** plist는 내려가지만 인벤토리에 `lifecycle: "masked"`로 남고,
운영자가 남긴 `stop`/`disable` 의도도 보존된다. masked 상태에서 `macosctl start`는
거부된다.

```bash
macosctl unmask 내서비스 && sudo macosctl apply
```

조각의 `managed = false`와는 다르다 — 그건 **프로젝트가** "아직 준비 안 됐다"고
선언하는 것이고, mask는 **이 머신이** "여기선 안 쓴다"고 정하는 것이다.

---

## 떼어내기

```bash
macosctl unlink 내서비스      # conf.d의 심링크만 제거. 원본은 안 지운다
sudo macosctl apply           # 여기서 은퇴한다
```

은퇴는 bootout + `enable` 복원 + plist 제거 + 인벤토리에서 삭제까지다.
**로그(`~/Library/Logs/services/*.log`)와 서비스 자신의 상태(가중치·DB·캐시)는
건드리지 않는다.**

---

## 진단

```bash
macosctl doctor
```

세 가지를 대조한다 — **선언**(조각), **등록**(`/Library/LaunchDaemons` + launchd),
**실제**(리스너·프로세스 트리). 어긋나면 항목마다 이름이 붙어 나온다:
`rogue`(선언에 없는데 포트 점유), `not installed`, `declared-but-disabled`,
`orphan-dropin`, `masked-but-installed` 등. 드리프트가 없어도 오버라이드 현황은
출력한다.

```bash
macosctl status --json   # 실측 상태를 안정된 JSON으로 (Webtop용)
macosctl manifest --json # 병합된 선언을 안정된 JSON으로
```

---

## 명령

| | |
|---|---|
| `status` `doctor` `logs` `cat` | 관측 |
| `apply` `manifest` | reconcile |
| `link` `unlink` `edit` `new` | 조각 구성 |
| `mask` `unmask` | 이 머신에서 정지 |
| `start` `stop` `restart` `enable` `disable` | 제어 (root 래퍼 경유) |

`--config-root`로 설정 루트를 덮을 수 있다 (기본 `/etc/macosctl`; 스테이징 검증용).

---

## 파일 배치

| 경로 | 소유 | 무엇 |
|---|---|---|
| `/etc/macosctl/policy.json` | `root:wheel 0644` | 라벨 접두사, 실행 사용자, 그룹 |
| `/etc/macosctl/macosctl.toml` | 사용자 | `[defaults]` |
| `/etc/macosctl/conf.d/*.toml` | 사용자 | 프로젝트 조각 (보통 심링크) |
| `/etc/macosctl/conf.d/<name>.d/*.toml` | 사용자 | 머신 드롭인 |
| `/var/db/macosctl/inventory` | `root:wheel 0644` | 우리가 설치한 것의 기록 |
| `/var/db/macosctl/state` | `root:wheel 0644` | 운영자의 정지 의도 |
| `/usr/local/sbin/macosctl-helper` | `root:wheel 0755` | 제어 래퍼 (NOPASSWD sudo) |

`conf.d`가 사용자 소유이므로 `link`/`unlink`/`edit`/`mask`에 `sudo`가 필요 없다.
`macosctl-helper`은 `policy.json` 외에 **사용자가 쓸 수 있는 것을 하나도 읽지 않는다.**

---

## 설치

```bash
sudo ./install.sh --manifest /absolute/path/to/services.toml
```

`policy.json → macosctl.toml → conf.d → macosctl-helper/sudoers → CLI 심링크` 순으로 간다.
policy를 macosctl-helper보다 먼저 쓰는 것이 중요하다 — 역순이면 그 사이 제어가 전부 막힌다.

## 쉘 자동완성 (zsh)

```zsh
# ~/.zshrc
fpath=(~/git/macosctl/completions $fpath)
autoload -U compinit && compinit
```

서브커맨드와 `logs`/`cat`/`edit`/`unlink`/`mask`/`unmask`/`start`/`stop`/`restart`/
`enable`/`disable` 뒤의 서비스 이름 모두 탭으로 완성된다. 서비스 이름은
`macosctl _complete-names`(내부용 서브커맨드)가 confd만 읽어서 주므로 `status`처럼
launchctl/포트를 조회하지 않아 탭마다 즉시 응답한다.

## 테스트

pytest 없이 돈다.

```bash
for f in tests/test_*.py; do printf '%-26s ' "$(basename $f)"; python3 "$f" 2>&1 | tail -1; done
```

---

## 문서

| | |
|---|---|
| [docs/design.md](docs/design.md) | 설계 계약과 근거 |
| [docs/services.md](docs/services.md) | 운영 절차 |
