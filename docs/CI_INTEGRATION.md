# CI 集成指南（API Token）

本指南介绍如何让外部 CI 系统（Jenkins、GitLab CI、GitHub Actions 等）通过 **API Token** 调用 AutoDroid 接口，打通「App 代码合并 → 上传新包 → 自动触发回归场景 → 轮询结果 → 判定流水线成败」的完整链路。

## 1. API Token 是什么

- **长效机器凭证**：不像 JWT 登录态会过期，Token 长期有效，直到被吊销。
- **格式**：`adk_` + 48 位十六进制随机串（共 52 字符），例如 `adk_3f9c…`。请求时放在 `Authorization: Bearer adk_...` 头中，与 JWT 用法一致。
- **安全存储**：服务端仅保存 sha256 哈希，明文只在创建时返回一次，之后任何接口都无法再查看。
- **权限受限**：Token 与创建者账号绑定、继承其业务权限，但**禁止**访问管理面（详见下文权限边界）。

### 权限边界

| 操作 | API Token | 说明 |
| --- | --- | --- |
| 上传 APK（`POST /api/packages/upload`、分片上传） | ✅ | |
| 触发场景执行（`POST /api/scenarios/{id}/run`） | ✅ | |
| 中止执行（`POST /api/runs/cancel`） | ✅ | |
| 查询报告 / 批次结果（`GET /api/reports/executions`） | ✅ | |
| 查询设备（`GET /api/devices/`）、执行预检（`GET /api/scenarios/{id}/precheck`） | ✅ | |
| 用例 / 场景 / 任务等其他业务读写 | ✅ | 与属主账号权限一致 |
| 管理接口（`/api/admin/*`） | ❌ 403 | 机器凭证禁止管理面 |
| 修改密码（`PUT /api/auth/password`） | ❌ 403 | |
| 系统设置写入（`POST /api/settings/`、`POST /api/settings/test-notification`） | ❌ 403 | 设置读取允许 |
| Token 管理（`/api/tokens/*`） | ❌ 403 | Token 不能创建 / 吊销 Token |

## 2. 创建 Token

1. 登录 AutoDroid Web，进入 **系统配置 → API Token**。
2. 点击「创建 Token」，输入名称（建议 `CI 系统 + 用途`，如 `jenkins-regression`）。
3. **立即复制明文 Token**——对话框关闭后无法再次查看。
4. 将 Token 存入 CI 的密钥管理（Jenkins Credentials / GitHub Actions Secrets / GitLab CI Variables），变量名建议 `AUTODROID_TOKEN`。

也可以用已登录的 JWT 通过接口创建：

```bash
curl -s -X POST "$AUTODROID_URL/api/tokens/" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"name": "jenkins-regression"}'
# => {"token": "adk_...", "token_prefix": "adk_xxxxxxxx", ...}  # token 字段仅此一次返回
```

## 3. 完整回归脚本（curl）

下面的脚本演示 CI 中最常用的链路：上传 APK → 触发场景 → 轮询批次结果 → 判定 pass/fail。

```bash
#!/usr/bin/env bash
# autodroid_regression.sh
# 依赖：curl、jq
set -euo pipefail

AUTODROID_URL="${AUTODROID_URL:-http://autodroid.example.com:8000}"
TOKEN="${AUTODROID_TOKEN:?请通过环境变量注入 API Token}"
APK_PATH="${1:?用法: $0 <apk 路径> <场景 ID> <设备序列号,逗号分隔>}"
SCENARIO_ID="${2:?缺少场景 ID}"
DEVICE_SERIALS="${3:?缺少设备序列号}"
POLL_INTERVAL=15          # 轮询间隔（秒）
POLL_TIMEOUT=1800         # 轮询超时（秒）

auth=(-H "Authorization: Bearer $TOKEN")

# 1) 上传 APK（<100MB 可直接单次上传；更大的包见下文分片上传）
echo "==> 上传 APK: $APK_PATH"
pkg=$(curl -sf "${auth[@]}" -F "file=@${APK_PATH}" \
  "$AUTODROID_URL/api/packages/upload")
echo "    包名: $(echo "$pkg" | jq -r .package_name)  版本: $(echo "$pkg" | jq -r .version_name)"

# 2) 触发场景执行（device_serials 必填）
echo "==> 触发场景 #$SCENARIO_ID"
serials_json=$(printf '%s' "$DEVICE_SERIALS" | jq -R 'split(",")')
run=$(curl -sf "${auth[@]}" -H "Content-Type: application/json" \
  -d "{\"device_serials\": $serials_json}" \
  "$AUTODROID_URL/api/scenarios/$SCENARIO_ID/run")
batch_id=$(echo "$run" | jq -r .batch_id)
echo "    batch_id: $batch_id"
echo "$run" | jq -r '.blocked_prechecks[]? | "    预检拦截: \(.device_serial) - \(.reason)"'

# 3) 轮询批次结果，直到所有执行离开 RUNNING 状态
echo "==> 轮询批次结果"
deadline=$(( $(date +%s) + POLL_TIMEOUT ))
while true; do
  result=$(curl -sf "${auth[@]}" \
    "$AUTODROID_URL/api/reports/executions?batch_id=$batch_id&limit=100")
  running=$(echo "$result" | jq '[.items[] | select(.status == "RUNNING")] | length')
  total=$(echo "$result" | jq '.total')
  echo "    进度: $((total - running))/$total 完成"
  [ "$running" -eq 0 ] && [ "$total" -gt 0 ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "!! 轮询超时，主动中止批次"
    curl -sf "${auth[@]}" -H "Content-Type: application/json" \
      -d "{\"kind\": \"scenario\", \"target_id\": $SCENARIO_ID, \"batch_id\": \"$batch_id\"}" \
      "$AUTODROID_URL/api/runs/cancel" > /dev/null || true
    exit 1
  fi
  sleep "$POLL_INTERVAL"
done

# 4) 判定：任一执行非 PASS 即失败（WARNING 视为通过可按需调整）
echo "==> 批次结果明细"
echo "$result" | jq -r '.items[] | "    [\(.status)] \(.device_info // .device_serial) 用时 \(.duration)s 报告 \(.report_id // "-")"'
fails=$(echo "$result" | jq '[.items[] | select(.status != "PASS" and .status != "WARNING")] | length')
if [ "$fails" -gt 0 ]; then
  echo "== 回归失败：$fails 个执行未通过 =="
  exit 1
fi
echo "== 回归通过 =="
```

要点说明：

- **上传**：`POST /api/packages/upload` 为 multipart 表单，字段名 `file`，仅接受 `.apk`；同包名旧版本自动标记为非最新。超大 APK 建议走分片上传：`POST /api/packages/upload-sessions`（body：`filename`/`file_size`/`chunk_size`/`total_chunks`）→ 逐片 `POST /api/packages/upload-sessions/{upload_id}/chunks/{index}` → `POST /api/packages/upload-sessions/{upload_id}/complete`。
- **触发**：`POST /api/scenarios/{id}/run`，body `{"device_serials": ["serial1", ...], "env_id": 可选}`。响应含 `batch_id`、`execution_ids` 与被预检拦截的设备列表 `blocked_prechecks`；全部设备被拦截时返回 400（或限流时 429）。
- **轮询**：`GET /api/reports/executions?batch_id=<batch_id>`，`items[].status` 取值 `RUNNING / PASS / FAIL / WARNING / ERROR / ABORTED`。
- **预检**（可选）：触发前可用 `GET /api/scenarios/{id}/precheck?device_serial=xxx` 与 `GET /api/devices/`（看 `status` 是否 `IDLE`）先挑选可用设备。
- **中止**：`POST /api/runs/cancel`，body `{"kind": "scenario", "target_id": <场景ID>, "batch_id": "<批次>"}`，用于超时兜底。

## 4. GitHub Actions 示例

将 Token 存为仓库 Secret `AUTODROID_TOKEN`，服务地址存为 `AUTODROID_URL`（或 Variables）。

```yaml
# .github/workflows/autodroid-regression.yml
name: AutoDroid Regression

on:
  push:
    branches: [main]

jobs:
  regression:
    runs-on: [self-hosted, android-build]   # 需要能访问 AutoDroid 服务的 runner
    env:
      AUTODROID_URL: ${{ vars.AUTODROID_URL }}
      AUTODROID_TOKEN: ${{ secrets.AUTODROID_TOKEN }}
      SCENARIO_ID: 12                        # 回归场景 ID
      DEVICE_SERIALS: emulator-5554,RF8M33Z  # 执行设备
    steps:
      - uses: actions/checkout@v4

      - name: Build APK
        run: ./gradlew assembleDebug

      - name: Run AutoDroid regression
        run: |
          chmod +x ci/autodroid_regression.sh
          ci/autodroid_regression.sh \
            app/build/outputs/apk/debug/app-debug.apk \
            "$SCENARIO_ID" "$DEVICE_SERIALS"
```

## 5. Jenkins Pipeline 示例

将 Token 存为 Jenkins 凭据（Secret text，ID：`autodroid-token`）。

```groovy
// Jenkinsfile
pipeline {
    agent { label 'android-build' }

    environment {
        AUTODROID_URL   = 'http://autodroid.example.com:8000'
        AUTODROID_TOKEN = credentials('autodroid-token')
        SCENARIO_ID     = '12'
        DEVICE_SERIALS  = 'emulator-5554,RF8M33Z'
    }

    stages {
        stage('Build APK') {
            steps {
                sh './gradlew assembleDebug'
            }
        }
        stage('AutoDroid Regression') {
            steps {
                sh '''
                    chmod +x ci/autodroid_regression.sh
                    ci/autodroid_regression.sh \
                        app/build/outputs/apk/debug/app-debug.apk \
                        "$SCENARIO_ID" "$DEVICE_SERIALS"
                '''
            }
        }
    }

    post {
        failure {
            echo 'AutoDroid 回归未通过，请查看报告中心排查'
        }
    }
}
```

GitLab CI 同理：把 Token 配置为 Masked/Protected CI Variable，`script` 段调用同一个脚本即可。

## 6. 安全建议

- **只放密钥管理**：Token 必须存入 CI 的密钥管理（Credentials / Secrets / Variables），严禁写入代码仓库、日志或构建产物。
- **一系统一 Token**：为每个 CI 系统 / 用途创建独立 Token，便于按名称与前缀（列表页展示前 12 位）审计，泄露时可精准吊销、影响面最小。
- **定期轮换**：建议每 90 天轮换一次——先创建新 Token 并更新 CI 密钥，验证流水线通过后再吊销旧 Token，实现无缝切换。
- **吊销流程**：Web「系统配置 → API Token」点击吊销（或 `DELETE /api/tokens/{id}`，需 JWT 登录态），吊销**立即生效**，所有使用该 Token 的请求返回 401。怀疑泄露时应立刻吊销。
- **最小权限属主**：用普通角色（非 admin）账号创建 CI 用 Token。Token 天然无法访问管理面，但业务删除等权限仍随属主角色放大。
- **关注最近使用**：列表页的「最近使用」时间（约 1 分钟粒度）可用于发现僵尸 Token（长期未用应吊销）与异常调用（非构建时段的使用）。
- **网络层面**：生产部署建议 HTTPS 反向代理，避免 Token 在链路上明文传输。
