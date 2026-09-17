# codenotary-demo — GOAI 2026 决赛演示仓库（C1 主案例）

一个刻意"小而坑"的计费服务：**AI 修好了用户看到的问题，但修复本身引入了更贵的语义错误**——
现有测试全绿、PR 看起来合理，而代码会把大促最后一天晚高峰（上海 11-10 08:00 起）整段核销关闭。

> 本仓库为脱敏改编演示材料：缺陷模式抽象自真实开源项目
> （django-celery-beat #759/#798 的时区混比与边界语义、kubernetes#78494 的过期语义），
> 不逐字对应任何真实代码库。

## 预埋结构

| 分支 | 内容 |
|---|---|
| `main` | v0：第一层 bug——naive `EXPIRES_AT` 与支付网关 aware UTC 混比抛 TypeError，fail-closed 兜底导致"没到期就核销不了" |
| `fix/coupon-expiry-timezone` | **PR #1**：时区修复正确（统一 UTC 比较）✅，但把"有效至 11 月 10 日"译成 **UTC 日界** `2026-11-10T00:00:00Z` ❌ = 上海 11-10 08:00 起全部失效。自带 3 个测试全绿，独缺"11-10 当天业务时段" |
| `perf/optimize-billing` | **PR #2**：一句话描述、无验收条件、触及 3 文件 → 走"证据不足拒绝"（INSUFFICIENT）路径 |

## 复现演示流程（fork 后）

```bash
# 0. 一次性就位（需要 gh CLI）
OWNER=<your-account> REPO=codenotary-demo bash scripts/setup_github.sh
```

1. **赛前预跑 v1**：对 PR #1 触发 CodeNotary 审计（Action 或本地网关）→
   盲测按冻结契约 v1.0 写出 `test_valid_on_last_business_day_evening`（11-10 18:00 上海应可用）→ RED →
   REJECTED → PR 评论返工工单 → 作者评论 `/notary dispute` → ESCALATED 封存。
2. **现场裁决**：裁决人确认"业务日含当天"，援引 `docs/channel-reconciliation-agreement-v2.3.md`
   第 4 条补证 → 发布契约 v1.1（previous_hash 咬合 v1，v1 不覆盖）。
3. **现场 push 修复**（v2，synchronize 触发重跑）：

```python
# src/billing/coupon.py —— v1.1 契约下的正确实现（两处：界值 + 算子）
EXPIRES_AT = datetime(2026, 11, 10, 23, 59, 59,
                      tzinfo=timezone(timedelta(hours=8)))  # 业务日终（上海）

def is_expired(self, now: datetime) -> bool:
    return now.astimezone(timezone.utc) > EXPIRES_AT  # 末日 23:59:59 前（含）均可核销
```

4. 全绿 → NOTARIZED → commit status success → **merge 按钮由灰变亮，但点合并的是人**。

## 本地跑测试

```bash
pip install pytest
python -m pytest tests/ -q          # main 上 3 个 baseline 全绿
```

## CI 审计机制（回写器）

`.github/workflows/codenotary.yml` 两个 job：

- **pr-tests**：跑 PR 自带测试——全绿 ≠ 可信，这正是旁边审计 job 存在的意义
- **audit**：`scripts/ci_audit.py` 起网关（钉 `CODENOTARY_SHA` 仓库变量）→
  intake（内容哈希 + run_tag 幂等）→ 按 `.notary/audit/<tag>.json` spec 驱动流水线 →
  回写 commit status（context `codenotary-audit`）+ PR 评论（**run_tag 幂等键，原地更新**）+
  EvidencePack artifact

**spec 即"角色判断的赛前实录"**：triage/diagnosis/契约/盲测是真实 LLM 会话的产出（赛前录屏佐证），
门禁 verdict 全部现场重算。无 spec 的 PR（如 PR #2）走**受理窗口形式审查** → INSUFFICIENT 拒绝评论。

**争议不在 Action 里处理**：`/notary dispute` 落在预封存网关（console 受审通道 / Poller），
Action 遇 ESCALATED 只发 `pending` + 升级评论，裁决后由下一次 `synchronize`/手动 re-run 接续。

## 目录

```
src/billing/coupon.py     # 核销有效期判定（两层 bug 的舞台）
src/billing/order.py      # 下游：订单结算
src/billing/refund.py     # 下游：退款补偿
docs/                     # 《渠道对账协议 v2.3》（裁决补证材料，脱敏改编）
.github/workflows/        # CodeNotary 审计 Action
scripts/setup_github.sh   # issue/PR/branch protection 一键就位
```
