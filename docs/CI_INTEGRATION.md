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
| 上传 APK/IPA（`POST /api/packages/upload`、分片上传） | ✅ | |
| 触发场景执行（`POST /api/scenarios/{id}/run`） | ✅ | |
| 中止执行（`POST /api/runs/cancel`） | ✅ | |
| 查询报告 / 批次结果（`GET /api/reports/executions`） | ✅ | |
| 查询设备（`GET /api/devices/`）、执行预检（`GET /api/scenarios/{id}/precheck`） | ✅ | |
| 巡检配置 / 触发 / 查询 / 取消（`/api/inspections/*`） | ✅ | 继承属主业务权限；需启用 `model_inspection` |
| 用巡检稳定路径创建兼容性任务（`POST /api/compatibility/runs`） | ✅ | `source_type=inspection` |
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

- **上传**：`POST /api/packages/upload` 为 multipart 表单，字段名 `file`，接受 `.apk` 和 Ad Hoc 签名的 `.ipa`；同平台、同包标识的旧版本自动标记为非最新。超大安装包建议走分片上传：`POST /api/packages/upload-sessions`（body：`filename`/`file_size`/`chunk_size`/`total_chunks`）→ 逐片 `POST /api/packages/upload-sessions/{upload_id}/chunks/{index}` → `POST /api/packages/upload-sessions/{upload_id}/complete`。
- **触发**：`POST /api/scenarios/{id}/run`，body `{"device_serials": ["serial1", ...], "env_id": 可选}`。响应含 `batch_id`、`execution_ids` 与被预检拦截的设备列表 `blocked_prechecks`；全部设备被拦截时返回 400（或限流时 429）。
- **轮询**：`GET /api/reports/executions?batch_id=<batch_id>`，`items[].status` 取值 `RUNNING / PASS / FAIL / WARNING / ERROR / ABORTED`。
- **预检**（可选）：触发前可用 `GET /api/scenarios/{id}/precheck?device_serial=xxx` 与 `GET /api/devices/`（看 `status` 是否 `IDLE`）先挑选可用设备。
- **中止**：`POST /api/runs/cancel`，body `{"kind": "scenario", "target_id": <场景ID>, "batch_id": "<批次>"}`，用于超时兜底。

## 4. 模型化智能巡检（Android）

巡检接口同样接受 API Token。它只支持显式指定一台 Android 设备，且服务器必须先开启
`model_inspection` Feature Flag。Profile 中保存未登录、已登录两条业务线，用例和环境 ID
由平台配置管理，CI 不应硬编码这些内部依赖。

```bash
PROFILE_ID="${INSPECTION_PROFILE_ID:?缺少巡检 Profile ID}"
DEVICE_SERIAL="${INSPECTION_DEVICE_SERIAL:?缺少巡检设备}"
PACKAGE_ID="${AUTODROID_PACKAGE_ID:-null}" # 可使用上传接口响应中的 .id

run=$(curl -sf "${auth[@]}" -H "Content-Type: application/json" \
  -d "{
    \"profile_id\": $PROFILE_ID,
    \"name\": \"CI 模型巡检 ${GIT_COMMIT:-manual}\",
    \"device_serial\": \"$DEVICE_SERIAL\",
    \"package_id\": $PACKAGE_ID,
    \"branches\": [\"guest\", \"authenticated\"]
  }" \
  "$AUTODROID_URL/api/inspections/runs")
inspection_run_id=$(echo "$run" | jq -r .id)

deadline=$(( $(date +%s) + 2400 ))
while true; do
  result=$(curl -sf "${auth[@]}" \
    "$AUTODROID_URL/api/inspections/runs/$inspection_run_id")
  status=$(echo "$result" | jq -r .status)
  case "$status" in
    PASS|WARNING|FAIL|ERROR|ABORTED) break ;;
  esac
  if [ "$(date +%s)" -ge "$deadline" ]; then
    curl -sf -X POST "${auth[@]}" \
      "$AUTODROID_URL/api/inspections/runs/$inspection_run_id/cancel" >/dev/null
    echo "巡检超时并已请求取消"
    exit 1
  fi
  sleep 15
done

echo "$result" | jq '{
  id, status, total_states, total_transitions, blocked_count, stable_count, fault_count
}'
# CI 默认将 WARNING（预算停止/覆盖盲区）视为通过；可按项目门禁改为仅 PASS。
[ "$status" = PASS ] || [ "$status" = WARNING ]
```

巡检报告可通过 `GET /api/inspections/runs/{id}/graph` 查询拓扑。报告资产接口要求同一
Authorization 头，不能把资产 URL 当作匿名静态链接。取消使用巡检自己的
`POST /api/inspections/runs/{id}/cancel`，不要调用仅支持 case/scenario 的通用取消接口。

新巡检任务会在不可变的 `profile_snapshot` 中记录 `graph_hierarchy_version=2`，graph
顶层通过 `hierarchy_version` 返回该协议版本。v2 graph link 会返回 `relation_type`
（`SELF`、`VIEWPORT`、`PEER`、`CHILD`）和 `relation_confidence`；node 的
`hierarchy_role` 为 `BRANCH_ROOT`、`PEER`、`PAGE`、`VIEWPORT` 或 `ORPHAN`。
`depth` / `parent_state_id` 表示业务展示层级，真实设备回放仍以 `first_path` 为准。
历史任务缺少版本标记时按 v1 返回，`hierarchy_role` 固定为空并继续使用旧构树逻辑，
不会根据新关系字段重新解释历史报告。

Graph schema v5 增加页面子类型、Frontier 优先级、页面族代表覆盖率和 Coverage Contract
统计。灰度环境可分别开启 `inspection_coverage_scheduler_v2` 与
`inspection_visual_home_actions`；后者依赖前者。覆盖调度器按页面族代表和动作组采样，
滚动只新增 `VIEWPORT` Observation，不创建新的业务 State。历史任务不会重算。

上线真机前可用历史资产做只读离线验收：

```bash
.venv/bin/python scripts/maintenance/replay_inspection_coverage.py 22 --strict
```

严格模式会检查商品动作不超过 5、滚动动作不超过 25、viewport State 为 0，以及附近
门店不重复执行全局导航。

若要把已勾选的稳定状态作为兼容性基线，创建兼容性任务时传：

```json
{
  "name": "CI 巡检快照回归",
  "source_type": "inspection",
  "inspection_run_id": 123,
  "inspection_state_ids": [],
  "compare_mode": "snapshot",
  "old_package_id": null,
  "new_package_id": 456,
  "page_set_id": null,
  "device_serials": ["emulator-5554"],
  "mode": "upgrade",
  "thresholds": {}
}
```

空的 `inspection_state_ids` 表示使用该巡检任务中人工勾选的全部稳定状态。创建任务时，
路径、规则及脱敏基线文件会复制进兼容性报告目录，因此后续巡检报告清理不会改变历史判定。

## 5. GitHub Actions 示例

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

## 6. Jenkins Pipeline 示例

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

### Jenkins + 蒲公英 iOS 制品

蒲公英的 iOS 安装入口属于手机端 OTA，不能替代 AutoDroid 向指定测试机推送。Jenkins 应把**同一份 Ad Hoc IPA**同时交给两个系统：蒲公英继续用于人工内测下载，AutoDroid 保存本地制品并通过当前 Mac 的 `devicectl` 安装到已配对 iPhone。

```groovy
environment {
    AUTODROID_URL   = 'http://autodroid.example.com:8000'
    AUTODROID_TOKEN = credentials('autodroid-token')
    PGYER_API_KEY   = credentials('pgyer-api-key')
    IPA_PATH        = 'build/export/YourApp.ipa'
}

stages {
    stage('Build Ad Hoc IPA') {
        steps {
            sh './ci/export_adhoc_ipa.sh "$IPA_PATH"'
        }
    }
    stage('Publish IPA') {
        steps {
            sh '''
                set -euo pipefail

                # 保留现有蒲公英上传脚本/插件，并等待其发布成功。
                ./ci/upload_to_pgyer.sh "$IPA_PATH" "$PGYER_API_KEY"

                # 将同一制品交给 AutoDroid；失败时让流水线失败，避免商城缺包。
                curl --fail --silent --show-error \
                    -H "Authorization: Bearer $AUTODROID_TOKEN" \
                    -F "file=@$IPA_PATH" \
                    "$AUTODROID_URL/api/packages/upload" | jq .
            '''
        }
    }
}
```

注意：

- IPA 必须使用 Ad Hoc 导出，目标测试机 UDID 必须已写入 provisioning profile；App Store、Development 和 Enterprise/In-House 包会被 AutoDroid 拒绝。
- iPhone 必须由运行 AutoDroid 后端的 Mac 配对并信任，iOS 16 起还需预先开启开发者模式；一期安装支持 iOS 17 及以上。
- AutoDroid 不需要、也不会保存蒲公英 API Key；两个密钥分别保存在 Jenkins Credentials 中。
- 若反向代理限制大文件请求，IPA 上传改用上文的 20 MiB 分片接口，不要从蒲公英页面抓取临时下载地址。

## 7. 安全建议

- **只放密钥管理**：Token 必须存入 CI 的密钥管理（Credentials / Secrets / Variables），严禁写入代码仓库、日志或构建产物。
- **一系统一 Token**：为每个 CI 系统 / 用途创建独立 Token，便于按名称与前缀（列表页展示前 12 位）审计，泄露时可精准吊销、影响面最小。
- **定期轮换**：建议每 90 天轮换一次——先创建新 Token 并更新 CI 密钥，验证流水线通过后再吊销旧 Token，实现无缝切换。
- **吊销流程**：Web「系统配置 → API Token」点击吊销（或 `DELETE /api/tokens/{id}`，需 JWT 登录态），吊销**立即生效**，所有使用该 Token 的请求返回 401。怀疑泄露时应立刻吊销。
- **最小权限属主**：用普通角色（非 admin）账号创建 CI 用 Token。Token 天然无法访问管理面，但业务删除等权限仍随属主角色放大。
- **关注最近使用**：列表页的「最近使用」时间（约 1 分钟粒度）可用于发现僵尸 Token（长期未用应吊销）与异常调用（非构建时段的使用）。
- **网络层面**：生产部署建议 HTTPS 反向代理，避免 Token 在链路上明文传输。
