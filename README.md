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

웹 UI: `http://NAS_IP:8787`
Swagger: `http://NAS_IP:8787/docs`

## NAS 폴더와 마운트

현재 Compose는 NAS의 `/volume1/homes`를 컨테이너의 같은 경로에 연결합니다.

```yaml
volumes:
  - /volume1/homes:/volume1/homes
  - /volume1/docker/synology-duplicate-manager/data:/data
  - /volume1/docker/synology-duplicate-manager/model-cache:/root/.cache/huggingface
```

`원본경로:컨테이너경로`에서 왼쪽은 NAS 경로, 오른쪽은 앱이 사용하는 컨테이너 경로입니다.
앱 설정에는 오른쪽 경로를 지정합니다. 컨테이너의 `/app`, `/etc`, `/usr` 등은 이미지 내부 폴더이므로 마운트 없이도 보이지만, NAS 외부 폴더는 마운트해야 접근할 수 있습니다.

다른 공유 폴더도 사용하려면 실제 존재하는 경로를 각각 마운트하고 웹의 폴더 설정에서 선택하세요. 예: `/volume2/photo:/volume2/photo`.
`NAS_ROOT=/`만으로 NAS의 모든 볼륨이 연결되는 것은 아닙니다. 현재 구성은 볼륨 최상위 대신 공유 폴더 단위로 마운트합니다.

## 환경변수

`docker-compose.yml`의 `environment`에서 앱 동작을 설정합니다. 아래 값은 현재 Compose 구성 기준이며, 코드 내부 기본값과 다를 수 있습니다.
변수 순서는 동작에 영향을 주지 않습니다. 표에 나온 경로는 모두 컨테이너 내부 경로입니다.

### 폴더와 저장 위치

| 변수 | 현재 설정값 | 설명 |
| --- | --- | --- |
| `NAS_ROOT` | `/` | 폴더 탐색 시작점이자 경로 허용 범위입니다. 폴더 선택과 파일 삭제 시 이 범위 안인지 검사합니다. `/`이면 컨테이너 자체 폴더까지 탐색할 수 있습니다. |
| `SCAN_ROOTS` | `/volume1/homes` | 중복 파일 검사의 기본 대상입니다. 여러 경로는 쉼표로 구분합니다. |
| `PHOTO_ROOTS` | `/volume1/homes` | AI 사진 검색 인덱싱의 기본 대상입니다. 중복 검사와 별도 폴더를 지정할 수 있습니다. |
| `DB_PATH` | `/data/duplicates.db` | 파일 경로, 크기, 수정 시간, MD5 등 중복 검사 기록을 저장하는 SQLite DB입니다. |
| `SETTINGS_PATH` | `/data/settings.json` | 웹에서 선택한 중복 스캔 및 사진 인덱싱 대상 폴더 목록을 저장합니다. |
| `PHOTO_DATA_DIR` | `/data/photo-search` | 사진 검색의 로컬 작업 데이터 위치입니다. 미리보기 이미지는 이 아래 `thumbnails`에 저장됩니다. 사진 검색용 벡터 인덱스는 Qdrant에 별도로 저장됩니다. |

**웹에서 저장한 폴더 설정이 `SCAN_ROOTS`와 `PHOTO_ROOTS`보다 우선합니다.** 환경변수만 바꿔도 저장된 선택이 자동으로 바뀌지는 않습니다.
환경변수가 없거나 비어 있으면 현재 코드의 `SCAN_ROOTS` 기본값은 `/volume1`입니다. `PHOTO_ROOTS`도 없으면 기존 `PHOTO_ROOT` 설정을 확인하고, 그것도 없으면 `SCAN_ROOTS`를 사용합니다.
실제 마운트 경로에 맞춰 Compose에 대상 폴더를 명시하는 편이 좋습니다.

### 제외 규칙과 중복 검사

| 변수 | 현재 설정값 | 설명 |
| --- | --- | --- |
| `EXCLUDE_DIRS` | `#recycle,#snapshot,docker` | 해당 이름의 폴더를 탐색·스캔에서 제외합니다. 쉼표로 구분합니다. |
| `EXCLUDE_AT_PREFIX` | `true` | `@eaDir`처럼 이름이 `@`로 시작하는 폴더를 제외합니다. |
| `HASH_CHUNK_SIZE` | `4194304` | MD5 계산 시 한 번에 읽는 크기입니다. 현재 값은 4 MiB이며 검사할 파일 크기의 제한은 아닙니다. |

### AI 사진 검색

| 변수 | 현재 설정값 | 설명 |
| --- | --- | --- |
| `QDRANT_URL` | `http://qdrant:6333` | 사진 검색 인덱스를 저장·조회하는 Qdrant 서버 주소입니다. `qdrant`는 같은 Compose 네트워크의 서비스 이름입니다. |
| `QDRANT_COLLECTION` | `nas_photos` | Qdrant 안에서 사용할 사진 인덱스 컬렉션 이름입니다. 바꾸면 다른 인덱스를 사용하게 됩니다. |
| `PHOTO_SCAN_ON_START` | `false` | 컨테이너 시작 시 사진 인덱싱을 자동 실행할지 정합니다. 현재는 웹에서 직접 시작합니다. |
| `PHOTO_BATCH_SIZE` | `16` | AI 인덱싱 시 한 묶음에 처리하는 사진 수입니다. 작업자 수가 아닙니다. 값을 높이면 메모리 사용량이 늘 수 있으며 반드시 빨라지는 것은 아닙니다. |
| `PHOTO_SEARCH_LIMIT` | `60` | 사진 검색에서 반환할 결과 수의 상한입니다. 실제 반환 수는 요청한 개수와 검색 결과에 따라 더 적을 수 있습니다. |
| `THUMBNAIL_SIZE` | `480` | 미리보기 이미지를 480×480 픽셀 범위 안에 원본 비율을 유지하여 맞춥니다. 원본 사진은 변경하지 않습니다. |

환경변수 값은 문자열로 통일하여 다음처럼 작성할 수 있습니다. 따옴표 자체는 앱에 전달되지 않습니다.
특히 `true`, `false`와 `#`으로 시작하는 값은 YAML에서 다른 의미로 해석되지 않도록 따옴표를 유지하세요.

```yaml
environment:
  NAS_ROOT: "/"
  SCAN_ROOTS: "/volume1/homes"
  PHOTO_ROOTS: "/volume1/homes"
  EXCLUDE_DIRS: "#recycle,#snapshot,docker"
  EXCLUDE_AT_PREFIX: "true"
  PHOTO_BATCH_SIZE: "16"
```

실행 중인 컨테이너에 설정된 환경변수는 다음으로 조회합니다. 이미지에서 설정한 기본 환경변수도 포함되며, 코드 내부 기본값이나 웹에서 저장한 폴더 설정은 별개입니다.

```bash
sudo docker inspect synology-duplicate-manager --format '{{range .Config.Env}}{{println .}}{{end}}'
```

Compose 환경변수 또는 마운트만 바꿨다면 컨테이너를 재생성하면 됩니다. 이때 진행 중인 스캔·인덱싱은 중단되므로 작업을 완료한 뒤 적용하세요.

```bash
sudo docker compose -f docker-compose.yml up -d --force-recreate
```

앱 코드나 Dockerfile도 변경했다면 위 명령에 `--build`를 추가합니다.

## 주의

현재 NAS 소스 마운트는 읽기/쓰기입니다. 파일 삭제 기능을 사용하지 않을 경우 `/volume1/homes:/volume1/homes:ro`로 변경하면 읽기 전용으로 사용할 수 있습니다.
SQLite, 설정, 썸네일, 모델 캐시, Qdrant 데이터는 `/volume1/docker/synology-duplicate-manager` 아래의 별도 마운트에 저장됩니다.
컨테이너에서 사용하는 파일 경로가 바뀌면 DB와 사진 인덱스에 이전 경로가 남을 수 있으므로, 새 마운트와 웹의 폴더 설정을 확인한 뒤 중복 스캔 및 사진 인덱싱을 다시 실행하세요.

## v3.2: 웹 폴더 설정

웹 UI의 **폴더 설정** 탭에서 `NAS_ROOT`를 시작점으로 탐색해 마운트된 NAS 공유 폴더를 선택할 수 있습니다. 현재 시작점은 컨테이너의 `/`입니다.

- 중복 스캔 대상 폴더
- AI 사진 인덱싱 대상 폴더

설정은 `/data/settings.json`에 저장되어 컨테이너 재시작 후에도 유지됩니다. `NAS_ROOT` 범위 밖의 경로와 상대경로는 API에서 거부하며, `@*`, `#recycle`, `#snapshot`, `docker` 같은 제외 폴더는 브라우저에서 숨깁니다.

기본값은 compose의 `SCAN_ROOTS` / `PHOTO_ROOTS`이며, 웹에서 한 번 저장하면 저장된 설정이 우선합니다.

## Duplicate file deletion

The duplicate list includes a per-file trash button. Deletion is permanent and removes only the selected file. Before deleting, the API verifies that the target is a regular file within `NAS_ROOT`, is recorded as a duplicate in the scan database, and that at least one other matching file still exists on disk. File deletion is blocked while duplicate scanning or photo indexing is running.

Batch deletion is also available from the duplicate list. The API validates every selected duplicate group before deleting anything and refuses a request that would remove every actual copy in any group. At least one matching file must remain on disk.

The NAS source volume must be mounted read/write for this feature. In `docker-compose.yml` this project uses:

```yaml
- /volume1/homes:/volume1/homes
```

## v3.7 경로별 일괄 선택
- 중복 파일 목록에서 경로별로 파일을 한 번에 체크/해제할 수 있습니다.
- `하위 폴더 포함` 옵션으로 선택한 폴더 아래의 중복 파일까지 함께 선택할 수 있습니다.
- 경로 선택으로 한 중복 그룹의 모든 파일이 체크되면 최소 1개를 자동으로 남깁니다.
- 서버의 일괄 삭제 안전 검증도 그대로 유지됩니다.
- 중복 목록은 한 번에 최대 500개 **그룹**을 불러옵니다. 한 그룹에 여러 파일이 있으므로 표시/선택 파일 수는 500개를 넘을 수 있으며, 일괄 삭제 API도 이를 지원합니다.
