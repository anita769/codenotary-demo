# codenotary-demo — CodeNotary 公开演示仓库

**AI 写的代码，先公证，再上线。** 本仓库是 CodeNotary 的公开演示仓库：
一个计费服务，一次 AI 修复，一条完整的公证流水线——从 PR 提交到人工裁决到合并放行，全程留痕、可复算。

> 缺陷模式抽象自真实开源项目
> （django-celery-beat #759/#798 的时区混比与边界语义、kubernetes #78494 的过期语义）。

## 在线体验与文档

- **在线体验**：http://ara.sciba.cn/tour · PR 实例：https://github.com/anita769/codenotary-demo/pull/2
- **体验版操作手册**：[CodeNotary/docs/manuals/体验版操作手册](https://github.com/anita769/CodeNotary/tree/main/docs/manuals/体验版操作手册)——12 步逐步实拍
- **使用手册**：[CodeNotary/docs/manuals/使用手册](https://github.com/anita769/CodeNotary/tree/main/docs/manuals/使用手册)——GitHub 接入（本仓 PR #2 就是活例）、ZIP 上传送审、证据包复算
- **公证引擎主仓**：https://github.com/anita769/CodeNotary ——源码、评测规范、演示视频

## 主案例：两层缺陷（C1）

**工单**（[Issue #1](../../issues/1)）："优惠券有效至 11 月 10 日，但没到期就核销不了。"

| 层 | 内容 | 结局 |
|---|---|---|
| 第一层 | naive `EXPIRES_AT` 与支付网关 aware UTC 混比抛 TypeError，fail-closed 兜底导致未到期券无法核销 | AI 修复**正确**（统一 UTC 比较），PR 自带测试全绿 |
| 第二层 | 修复把"有效至 11 月 10 日"译成 **UTC 日界**——上海时间 11-10 08:00 起全部失效，大促最后一天的晚高峰整段消失 | 普通 CI 抓不住：自带测试恰好没人测"10 号当天业务时段" |

## 这条 PR 真实走过的路（[PR #2](../../pull/2)，已合并）

1. **提交即审**：PR 触发 CodeNotary 审计（`.github/workflows/codenotary.yml`），钉版网关克隆 → 盲测按冻结契约推导出一个**原测试集里不存在的**用例——"11-10 18:00（上海）应可核销" → 红
2. **拒绝有理**：审计评论给出失败场景、未通过用例名、三个选择（修复重审 / 争议裁决 / 查看证据）；commit status `codenotary-audit` 红 → 分支保护拦住合并
3. **争议裁决**：送审方异议（"工单没说按哪个时区"）→ 升级人工裁决 → 裁决人援引 `docs/channel-reconciliation-agreement-v2.3.md` 第 4 条补证，修订契约 v1.0 → **v1.1**（有效期按业务所在地自然日，含当天至 23:59:59+08:00）
4. **修复重验**：按 v1.1 推送修复 → 自动重审 → 三门禁全绿 → 证书签发（绑定 commit SHA 与契约版本，任一方再变即失效）
5. **就绪意见**：合并就绪四态检查（证书有效 / SHA 绑定 / 无冲突 / 分支不落后 / 检查全绿）
6. **合并的是人**：系统全程不提供合并按钮——验收 ≠ 合并 ≠ 发布

对照组：同仓 [PR #3](../../pull/3) 只有一句"优化了性能"——**材料不足，受理窗口拒绝**，并告诉对方缺什么。对成功的放行和对失败的拒绝，依据同样明确。

## 复算与核验

每次审计产出 EvidencePack（Actions artifact，可下载）：

```bash
make verify EVIDENCE=evidence-pack.zip   # 四层复算：Ed25519 签名 → 哈希链 → 契约重算 → 门禁重跑比对
```

## CI 审计机制

`.github/workflows/codenotary.yml` 两个 job：

- **pr-tests**：跑 PR 自带测试——全绿 ≠ 可信，这正是旁边审计 job 存在的意义
- **audit**：`scripts/ci_audit.py` 起钉版网关（仓库变量 `CODENOTARY_SHA`）→ intake（内容哈希 + run_tag 幂等）→ 按 `.notary/audit/<tag>.json` spec 驱动流水线 → 回写 commit status + PR 评论（**原地更新不刷屏**）+ EvidencePack artifact

争议不在 Action 里等人：遇 ESCALATED 发 `pending` + 升级评论，裁决后由下一次触发接续。

## 本地跑测试

```bash
python -m unittest discover -s tests -v   # 标准库，零依赖
```

## 目录

```
src/billing/coupon.py     # 核销有效期判定（两层缺陷的舞台）
src/billing/order.py      # 下游：订单结算
src/billing/refund.py     # 下游：退款补偿
docs/                     # 《渠道对账协议 v2.3》（裁决补证材料，脱敏改编）
.github/workflows/        # CodeNotary 审计 Action
scripts/ci_audit.py       # CI 回写器（审计驱动 + 状态/评论回写 + 证据包）
scripts/merge_readiness.py# 合并就绪检查（四态意见卡）
scripts/setup_github.sh   # 复现环境一键就位（fork 后）
```
