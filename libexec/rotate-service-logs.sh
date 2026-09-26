#!/usr/bin/env bash
#
# rotate-service-logs.sh — macosctl.toml이 선언한 log_dir의 launchd 로그를 회전한다.
#
#   ./libexec/rotate-service-logs.sh            # 임계 초과분만 회전
#   ./libexec/rotate-service-logs.sh --force    # 크기 무관하게 전부 회전
#   ./libexec/rotate-service-logs.sh --dry-run  # 무엇을 할지만 출력
#
# 주기 실행은 macosctl apply가 등록하는 com.korellas.log-rotate가 맡는다.
#
# launchd가 연 로그 FD는 rename 후에도 기존 inode를 가리킨다.
# copytruncate로 inode를 보존한다. 복사와 truncate 사이 기록은 손실될 수 있다.
set -euo pipefail

CONFIG="${MACOSCTL_CONFIG:-}"

FORCE=false
DRY_RUN=false
case "${1:-}" in
  --force)   FORCE=true ;;
  --dry-run) DRY_RUN=true ;;
  "") ;;
  *) echo "Usage: $0 [--force|--dry-run]" >&2; exit 2 ;;
esac

log() { printf '[rotate-service-logs] %s\n' "$*"; }
err() { printf '[rotate-service-logs] %s\n' "$*" >&2; }

[[ -n "$CONFIG" ]] || {
  err "MACOSCTL_CONFIG가 없다 — launchd plist를 macosctl apply로 다시 생성할 것"
  exit 1
}
[[ -f "$CONFIG" ]] || { err "macosctl config not found: $CONFIG"; exit 1; }

# macosctl defaults가 단일 진실 원천 — 임계값도 여기서 읽는다.
#
# 파싱 실패를 반드시 여기서 잡는다. `read <<<"$(...)"`는 명령 치환이 죽어도
# set -e가 걸리지 않아, 빈 문자열을 그대로 받고 한참 뒤에 엉뚱한 증상으로
# 실패가 난다. 파싱 단계의 오류를 먼저 보고한다.
if ! PARSED_DEFAULTS="$(python3 - "$CONFIG" <<'PY'
import sys, tomllib
with open(sys.argv[1], 'rb') as f:
    d = tomllib.load(f).get('defaults', {})
print(d.get('log_dir', ''), d.get('log_max_mb', 20), d.get('log_keep', 5))
PY
)"; then
  err "macosctl config 파싱 실패: $CONFIG"
  err "  python3 = $(command -v python3 || echo '없음')  ($(python3 -V 2>&1 || true))"
  err "  tomllib은 Python 3.11+ 표준 라이브러리다. launchd로 도는 중이라면"
  err "  plist의 EnvironmentVariables.PATH가 brew/uv 경로를 /usr/bin보다"
  err "  앞에 두고 있는지 확인할 것 (macosctl.toml [defaults].path와 동일해야 한다)."
  exit 1
fi

read -r LOG_DIR MAX_MB KEEP <<<"$PARSED_DEFAULTS"
[[ -n "$LOG_DIR" ]] || {
  err "macosctl config에 defaults.log_dir이 없다: $CONFIG"
  exit 1
}
[[ -d "$LOG_DIR" ]] || { err "log_dir not found: $LOG_DIR"; exit 1; }
MAX_BYTES=$(( MAX_MB * 1024 * 1024 ))

rotated=0
shopt -s nullglob
for f in "$LOG_DIR"/*.out.log "$LOG_DIR"/*.err.log; do
  size=$(stat -f %z "$f")
  if [[ "$FORCE" == false ]] && (( size < MAX_BYTES )); then
    continue
  fi

  if [[ "$DRY_RUN" == true ]]; then
    log "would rotate $(basename "$f")  ($(( size / 1024 )) KB ≥ ${MAX_MB}MB)"
    continue
  fi

  # 세대 밀어내기: .N.gz → .(N+1).gz, 가장 오래된 것부터. 역순으로 돌지
  # 않으면 .1이 .2를 덮어쓰며 전 세대가 한 파일로 뭉개진다.
  for (( n = KEEP - 1; n >= 1; n-- )); do
    [[ -f "$f.$n.gz" ]] && mv -f "$f.$n.gz" "$f.$(( n + 1 )).gz"
  done
  rm -f "$f.$(( KEEP + 1 )).gz"

  # 복사 → 원자적 배치 → in-place 비우기. gzip이 실패하면 truncate하지
  # 않는다 (set -e + &&) — 압축이 깨졌는데 원본까지 날리는 일은 없어야 한다.
  if gzip -c "$f" > "$f.1.gz.partial" && mv -f "$f.1.gz.partial" "$f.1.gz"; then
    : > "$f"          # ← rename이 아니다. inode 보존이 이 줄의 전부다.
    log "rotated $(basename "$f")  ($(( size / 1024 )) KB → $(basename "$f").1.gz)"
    rotated=$(( rotated + 1 ))
  else
    rm -f "$f.1.gz.partial"
    err "gzip failed for $f — left untouched"
  fi
done

log "done ($rotated rotated, threshold ${MAX_MB}MB, keep ${KEEP})"
