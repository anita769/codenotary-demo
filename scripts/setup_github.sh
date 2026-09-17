#!/usr/bin/env bash
# setup_github.sh — 在装有 gh CLI 的机器上执行，把演示仓库的 GitHub 侧一次性就位。
# 用法: OWNER=anita769 REPO=codenotary-demo bash scripts/setup_github.sh
set -euo pipefail
: "${OWNER:?set OWNER}"; : "${REPO:?set REPO}"

echo "== 1. 创建工单（C1 原始 issue，刻意保留时区/当天两处歧义）=="
gh issue create --repo "$OWNER/$REPO" \
  --title "优惠券有效至 11 月 10 日，但没到期就核销不了" \
  --body "$(cat <<'EOF'
用户投诉集中在今天上午：券还在有效期内，结算时提示无法核销。

优惠券规则：有效至 11 月 10 日。
请尽快修复，大促期间影响支付核销、订单结算、退款补偿三个流程。
EOF
)"

echo "== 2. 推送预埋分支并开 PR =="
git push -u origin main fix/coupon-expiry-timezone perf/optimize-billing

gh pr create --repo "$OWNER/$REPO" --base main \
  --head fix/coupon-expiry-timezone \
  --title "fix: 优惠券核销时区混比修复" \
  --body "$(cat <<'EOF'
修复 #1：核销请求全部报 validation_error。

根因：支付网关传入 aware UTC 时间，与 naive 的 EXPIRES_AT 混比抛 TypeError，
落入 fail-closed 兜底，导致未到期券也无法核销。

修改：统一在 UTC 下比较（`now.astimezone(utc)`），EXPIRES_AT 标注 tzinfo。
新增 3 个测试：时区转换、11-09 可用、11-11 不可用，全部通过。
EOF
)"

gh pr create --repo "$OWNER/$REPO" --base main \
  --head perf/optimize-billing \
  --title "perf: optimize billing" \
  --body "优化了一下计费模块的性能。"

echo "== 3. branch protection：合并被 codenotary-audit 状态门禁卡住 =="
gh api -X PUT "repos/$OWNER/$REPO/branches/main/protection" \
  --input - <<'EOF'
{
  "required_status_checks": {"strict": true, "contexts": ["codenotary-audit"]},
  "enforce_admins": true,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "required_conversation_resolution": false
}
EOF

echo "== 4. 仓库变量：钉住网关版本 =="
echo "   gh variable:set CODENOTARY_SHA --repo $OWNER/$REPO --body <pinned-sha>"
echo "完成。接下来按 README 的'复现演示流程'走。"
