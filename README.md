# Synology NAS Manager v3.6

Synology NAS Manager v3

FastAPI 기반 Synology NAS 중복 파일 관리 + 한국어 자연어 사진 검색 통합 버전입니다.

## 기능
- NAS 전체 파일 메타데이터 스캔
- 같은 크기 파일만 MD5 계산하여 중복 그룹 생성
- `@*`, `#recycle`, `#snapshot`, `docker` 디렉터리 제외
- SQLite 증분 스캔
- Qdrant + multilingual CLIP 기반 자연어 사진 검색
- 사진 변경분만 증분 AI 인덱싱
- 썸네일 캐시
- Swagger `/docs`, ReDoc `/redoc`
- 중복 파일 개별 삭제 및 체크박스 선택 일괄 삭제

## 실행 전 폴더 생성
```bash
sudo mkdir -p /volume1/docker/synology-duplicate-manager/{data,model-cache,qdrant_data}
```

## 실행
```bash
docker compose up -d --build
```

웹 UI: `http://NAS_IP:18787`
Swagger: `http://NAS_IP:18787/docs`

## 사진 경로
기본값은 `/volume1/photo` 입니다. 실제 사진 폴더가 다르면 `docker-compose.yml`의 `PHOTO_ROOT`를 컨테이너 내부 경로 기준으로 변경하세요. `/volume1` 전체가 `/nas/volume1:ro`로 마운트되어 있으므로 예를 들어 실제 경로가 `/volume1/photos/family`라면 `PHOTO_ROOT: /nas/volume1/photos/family`로 설정하면 됩니다.

## 주의
소스 NAS 볼륨은 read-only로 마운트됩니다. Qdrant, SQLite, 썸네일, 모델 캐시만 `/volume1/docker/synology-duplicate-manager` 아래에 기록됩니다.

## v3.2: 웹 폴더 설정

웹 UI의 **폴더 설정** 탭에서 `/nas` 아래 폴더를 탐색해 다음 경로를 각각 저장할 수 있습니다.

- 중복 스캔 대상 폴더
- AI 사진 인덱싱 대상 폴더

설정은 `/data/settings.json`에 저장되어 컨테이너 재시작 후에도 유지됩니다. `/nas` 밖의 경로는 API에서 거부하며, `@*`, `#recycle`, `#snapshot`, `docker` 같은 제외 폴더는 브라우저에서 숨깁니다.

기본값은 compose의 `SCAN_ROOTS` / `PHOTO_ROOTS`이며, 웹에서 한 번 저장하면 저장된 설정이 우선합니다.

## Duplicate file deletion

The duplicate list includes a per-file trash button. Deletion is permanent and removes only the selected file. Before deleting, the API verifies that the target is a regular file under `/nas`, is recorded as a duplicate in the scan database, and that at least one other matching file still exists on disk. File deletion is blocked while duplicate scanning or photo indexing is running.

Batch deletion is also available from the duplicate list. The API validates every selected duplicate group before deleting anything and refuses a request that would remove every actual copy in any group. At least one matching file must remain on disk.

The NAS source volume must be mounted read/write for this feature. In `docker-compose.yml` this project uses:

```yaml
- /volume1:/nas/volume1
```

## v3.7 경로별 일괄 선택
- 중복 파일 목록에서 경로별로 파일을 한 번에 체크/해제할 수 있습니다.
- `하위 폴더 포함` 옵션으로 선택한 폴더 아래의 중복 파일까지 함께 선택할 수 있습니다.
- 경로 선택으로 한 중복 그룹의 모든 파일이 체크되면 최소 1개를 자동으로 남깁니다.
- 서버의 일괄 삭제 안전 검증도 그대로 유지됩니다.
- 중복 목록은 한 번에 최대 500개 **그룹**을 불러옵니다. 한 그룹에 여러 파일이 있으므로 표시/선택 파일 수는 500개를 넘을 수 있으며, 일괄 삭제 API도 이를 지원합니다.
