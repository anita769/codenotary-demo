# 10-agent 协作线（重线）：外部 watcher 模式接入

> 轻线（本仓 `.github/workflows/codenotary.yml` + `scripts/ci_audit.py`）回答
> 「这张 PR 符不符合既定规则」——spec 预写、零 LLM、十几秒出结论。
> 重线回答「规则本身也过一遍公证」——PR 由 10 个最小权限 Worker
> 现场分诊、起草契约、设计盲测，歧义停下来等人工裁决。
> 两条线共用同一个网关与同一份证据格式，互不干扰，可单独使用也可轻重搭配。

## 拓扑

```
GitHub PR (新 head)
   │  gh api 轮询
   ▼
pr_poller.py（常驻任意一台能访问网关的机器）
   │  ① notary_intake.submit_issue（external 送审，run_tag 幂等）
   ▼
CodeNotary 网关（127.0.0.1:18090）
   │  ② Matrix 房间派发（m.mentions 结构化字段）
   ▼
AgentTeams Team 房：leader 调度 10 Worker 全接力
   │  ③ 轮询终态（ESCALATED 先回写 pending，不等人）
   ▼
回写 PR 评论（锚点幂等原地更新）+ commit status
```

## 运行

```bash
# 环境：一台能同时访问网关与 GitHub 的机器；gh 已登录或 GH_TOKEN 已配
python3 scripts/pr_poller.py \
    --repo <owner>/<repo> \
    --gateway http://127.0.0.1:18090 \
    --room '<team-room-mxid>' \
    --token-file <matrix-token-file> \
    --console-url https://<your-console> \
    # 默认 dry-run：回写只打印；确认无误后加 --apply 真实回写
```

- **幂等**：同一 PR 同一 head 不重复取号；新 head 产生并存新 run，历史永不覆盖
- **人工裁决**：流水线 ESCALATED 时 check 置 pending，裁决落地后重新触发自动继续
- **房间纪律**：使用独立 Team 房；首次派发若 leader 无响应，检查
  ①消息必须带 `m.mentions` 结构化字段（裸 @昵称 不算提到）
  ②派发账号须在 leader 工作区 access_control.json 白名单内（pending 需批准）

## 与轻线的分工

| | 轻线（ci_audit） | 重线（pr_poller） |
|---|---|---|
| 判断棒 | spec 预写，无 LLM | 10 Worker 现场 LLM |
| 运行位置 | GitHub runner | 常驻服务器（网关同机） |
| 契约来源 | `.notary/audit/*.json` 预写 | LLM 现场起草冻结，歧义走人裁 |
| 适用 | 日常 PR 回归守门 | 复杂修复、契约需要公证的变更 |
