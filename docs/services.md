# 서비스 관리 — 절차

이 스택의 서비스를 올리고, 내리고, 고치는 방법. 설계 근거는
[설계 계약](design.md)에 있고,
여기는 절차만 담는다.

## 한 줄 요약

**서비스는 각 프로젝트의 `service.toml`에 선언하고 `macosctl`로 다룬다.** `launchctl`을 직접 부르거나
`run-*.sh`를 손으로 띄우지 않는다.

## 어느 축을 바꾸는가 — 명령 고르기

정본은 CLI다: `macosctl`(인자 없이) 또는 `macosctl <command> --help`가 아래와 같은 말을 한다.

runtime/boot 제어는 `sudo` 없이 실행한다. CLI가 NOPASSWD sudo로 helper만 호출한다.
`macosctl stop web worker`처럼 여러 이름을 지정할 수 있으며, 처리 순서와 실패 시
동작은 각 제어 명령의 `--help`를 따른다.

| 축 | 명령 | 지금 상태 | boot 뒤 | apply |
|---|---|---|---|---|
| runtime | `start` `stop` `restart` | 바꾼다 | 보존 | 불필요 |
| boot policy | `enable` `disable` (`--now`) | 보존 (`--now`면 함께) | 바꾼다 | 불필요 |
| 정의 | `link` `unlink` `edit` `mask` `unmask` | apply 때 | apply 때 | **필요** |

- `stop`은 **이번 boot 한정**이다. boot 설정은 그대로라서 재부팅하면 다시 뜬다.
  재부팅을 넘기려면 `disable`.
- `disable`은 **boot만** 차단한다. 지금 도는 것은 계속 돈다 — 함께 내리려면 `--now`.
- `restart`는 inactive였어도 기동한다. 즉 최종 상태가 항상 active다.
- `--force`는 즉시 kill(정상 종료 건너뜀), `--recreate`는 처음부터 재등록.
  서로 다른 복구 전략이라 함께 쓸 수 없다.
- `start`/`restart`의 `--wait`은 helper가 성공한 **뒤** 선언 포트를 실제로 잡을
  때까지 기다린다. 상한은 `--timeout <초>` (기본 300).

**runtime 제어에는 `apply`가 필요 없다.** `apply`는 선언에서 생성되는 plist가
바뀐 서비스만 건드린다.

---

## 지금 뭐가 떠 있나

```bash
macosctl status      # 선언·등록·실제를 한 표로
macosctl doctor      # 셋이 어긋난 곳을 짚어준다 (exit 1 = 드리프트 있음)
macosctl logs <name> # 해당 서비스의 launchd 로그
```

`macosctl doctor`가 조용하면 정상이다. 뭔가 말하면 그 줄에 조치가 함께 나온다.

## 새 서비스 올리기

```bash
macosctl new my-model --port 8004 --group mlx --model org/model-id   # 블록을 보여준다
macosctl new my-model --port 8004 --group mlx --model org/model-id   # 초안 출력
$EDITOR service.toml                                                 # 사람이 확정
macosctl link ./service.toml                                         # 머신에 연결
macosctl apply --dry-run                                              # 할 일 확인
sudo macosctl apply                                                   # 반영
```

`macosctl new`는 파일을 쓰지 않는다. 출력된 블록을 프로젝트의 `service.toml`에
검토·저장한 뒤 `macosctl link`로 연결한다.

**런처**: `--group mlx`는 기존 `run-mlx-lm.sh`를 `MODEL`/`PORT`/`SESSION`만 바꿔
공유한다. 새 스크립트를 복제하지 말 것 — 수정이 갈라진다. 다른 그룹은
`run-<name>.sh`가 필요하고, **`foreground` 서브커맨드가 필수**다. 그게 없으면
launchd가 즉시 종료를 "죽었다"로 읽고 `KeepAlive`가 무한 재기동한다.

## 서비스 내리기

```bash
# 지금만 내린다 — boot 설정은 그대로라서 재부팅하면 되살아난다
macosctl stop <name>

# 재부팅 뒤에 안 뜨게 한다 — 지금 도는 것은 그대로 둔다
macosctl disable <name>

# 지금도 내리고 재부팅 뒤에도 안 뜨게
macosctl disable <name> --now

# 정의는 남기고 등록만 해제 — service.toml에서 managed = false
sudo macosctl apply

# 완전히 제거 — service.toml에서 [[service]] 블록을 지우거나 link를 해제하고
sudo macosctl apply     # 인벤토리 대조로 알아서 은퇴시킨다
```

은퇴 목록을 손으로 관리할 필요가 없다. `macosctl apply`가 인벤토리(우리가 설치한 것의
기록)와 매니페스트를 대조해 사라진 것을 정리한다.

## 재시작

```bash
macosctl restart <name>            # inactive여도 기동. active면 SIGTERM → 10초 유예 → SIGKILL
macosctl restart <name> --force    # 즉시 kill (정상 종료 건너뜀)
macosctl restart <name> --recreate # 처음부터 재등록 (--force와 함께 못 쓴다)
macosctl restart <name> --wait     # 선언 포트를 실제로 잡을 때까지 기다린다
```

boot 설정은 어느 쪽이든 보존된다. `apply`는 필요 없다.

모델 서버는 재기동에 수 분이 걸린다(가중치 재로딩). `macosctl status`가 그동안 `down`으로
보이는 것은 정상이다.

## 설정을 고쳤는데 반영이 안 될 때

`macosctl apply`는 **plist가 바뀐 서비스만** 재기동한다. 그래서 이런 변경은 apply로
반영되지 않는다:

- `run-*.sh` 수정
- `litellm-config.yaml`, `.env.litellm` 등 런처가 읽는 설정

이 경우는 `macosctl restart <name>`이 정답이다.

## 마이그레이션 (모델 교체 등)

```bash
# 1. 프로젝트 service.toml 또는 머신 drop-in에서 env.MODEL 등을 고친다
# 2. 무엇이 재기동될지 먼저 본다
sudo macosctl apply --dry-run
# 3. 반영
sudo macosctl apply
```

`--dry-run`은 "변경 → 재기동한다"를 서비스별로 보여준다. 모델 서버가 목록에 있으면
그만큼 서빙이 멎는다는 뜻이니, 시점을 고를 수 있다.

---

## 하지 말아야 할 것

| 대신 이것 | 하지 말 것 | 이유 |
|---|---|---|
| `macosctl restart x` | `launchctl kickstart system/com.korellas.x` | 감사 기록이 남지 않고, 인가를 우회한다 |
| `macosctl apply` | `launchctl bootstrap/bootout` 직접 호출 | 인벤토리와 어긋나 은퇴가 깨진다 |
| `macosctl new` + `macosctl apply` | `run-*.sh`로 상시 서비스 기동 | launchd 밖이라 재부팅에 안 뜨고, doctor가 rogue로 잡는다 |

**일회성 실험은 예외다.** `./run-mtplx.sh`로 잠깐 띄워 보는 것은 정당한 용도이고,
그러라고 tmux 경로를 남겨뒀다. 문제는 **상시 서비스가 tmux에 눌러앉는 것**이다.
실험이 끝나면 내리거나 `macosctl new`로 승격한다.

## 트러블슈팅

**`macosctl doctor`가 `rogue-listener`라고 한다**
포트를 launchd 밖 프로세스가 쥐고 있다. `macosctl status`의 PID를 확인해 그 프로세스를
내리고 `macosctl start <name>`.

**`duplicate-instance`라고 한다**
launchd 잡은 살아 있는데 포트는 남이 쥐고 있다. **가장 위험한 형태다** — 프로세스가
죽지 않았으니 `KeepAlive`가 개입하지 않고, `launchctl print`는 `running`이라고
보고한다. 관리 밖 프로세스를 내리고 `macosctl restart <name>`.

**`macosctl stop` 했는데 재부팅하니 다시 떠 있다**
정상이다. `stop`의 수명은 재부팅까지다(`bootout`은 런타임 상태만 지우고 plist는
남는다). 재부팅을 넘기려면 `macosctl disable`.

**`macosctl apply`가 "인벤토리가 없다"고 한다**
최초 1회 `sudo macosctl apply --adopt`로 현재 설치본을 승계한다. 멱등이라 여러 번 돌려도
된다.

**서비스가 안 뜨고 로그도 비어 있다**
`macosctl logs <name>`이 먼저다. 의존 포트를 기다리는 중이면 `Dependencies not ready`가
반복 찍힌다 — 그 포트의 서비스를 먼저 살펴야 한다.

## 감사

권한이 필요한 모든 동작은 `/usr/local/sbin/macosctl-helper`을 지나고 기록된다.

```bash
tail -f /var/log/macosctl-helper.log
```

## 참고

- launchd plist 키: [launchd.info](https://www.launchd.info/) ·
  Apple [Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) ·
  [TN2083](https://developer.apple.com/library/archive/technotes/tn2083/_index.html)
- 이 시스템의 [설계 계약](design.md)
