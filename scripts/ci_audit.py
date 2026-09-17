#!/usr/bin/env python3
"""CodeNotary CI audit runner + writeback ("回写器").

One Action run is one shot: it cannot pause at ESCALATED waiting for a
human. So this script drives the audit pipeline against the notary
gateway, then writes the outcome back to GitHub — and stops:

  commit status  success / failure / pending   (branch protection gate)
  PR comment     certificate / rework ticket / escalation notice,
                 updated IN PLACE keyed by run_tag (idempotent re-runs)
  EvidencePack   zipped run dir -> artifact    (the counter-audit entry)

Pipeline driving is spec-filed (`.notary/audit/<tag>.json`): role
judgments (triage / diagnosis / contract / blind tests) are recorded
from REAL LLM sessions (pre-stage, screen-recorded), while every gate
verdict is computed live by the gateway on the PR's actual code. The
machine judges by the rules; people rule on the rules.

Disputes (`/notary dispute` PR comments) are NOT handled here — they
land on the pre-sealed gateway via console / poller, by design.

Usage (Action):  see .github/workflows/codenotary.yml
Usage (local rehearsal):
  python3 scripts/ci_audit.py --repo OWNER/REPO --pr 1 --head-sha abc123 \
      --spec .notary/audit/pr1-v1.json --run-prefix pr1 \
      --notary-dir notary --evidence-out evidence-pack.zip --local
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

COMMENT_MARKER = "<!-- codenotary:run={run_tag} -->"
STATUS_CONTEXT = "codenotary-audit"


class AuditFailure(Exception):
    pass


# ---------------------------------------------------------------------------
# HTTP helpers: gateway (local, no auth) and GitHub API (token + backoff)
# ---------------------------------------------------------------------------

def gw_call(base: str, sid: str, tool: str, payload: dict | None = None,
            tolerate: tuple[str, ...] = ()) -> dict:
    req = urllib.request.Request(
        f"{base}/tools/{sid}/{tool}",
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
    if not body.get("ok"):
        err = body.get("error", "")
        if any(t in err for t in tolerate):
            return body
        raise AuditFailure(f"gateway tool {tool} failed: {err}")
    return body


def gh_api(repo: str, method: str, path: str, payload: dict | None = None,
           token: str | None = None, max_retries: int = 5) -> dict | list:
    """GitHub API with exponential backoff on 429/5xx (rate-limit safe)."""
    url = f"https://api.github.com/repos/{repo}{path}"
    delay = 2.0
    for attempt in range(max_retries + 1):
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(payload).encode("utf-8") if payload else None,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            retriable = exc.code == 429 or exc.code >= 500
            if not retriable or attempt == max_retries:
                raise AuditFailure(
                    f"gh api {method} {path}: HTTP {exc.code} "
                    f"{exc.read().decode('utf-8')[:300]}")
            retry_after = exc.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else delay
            print(f"  gh api {path}: HTTP {exc.code}, retry in {wait:.0f}s "
                  f"({attempt + 1}/{max_retries})")
            time.sleep(wait)
            delay = min(delay * 2, 60)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# Gateway lifecycle
# ---------------------------------------------------------------------------

def start_gateway(notary_dir: Path, port: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(notary_dir / "tools" / "notary_gateway.py"),
         "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1):
                return proc
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise AuditFailure("gateway did not start")


# ---------------------------------------------------------------------------
# Comment templates — facts from the gateway, wording fixed (公证处窗口口吻)
# ---------------------------------------------------------------------------

def render_comment(state: str, ctx: dict) -> str:
    marker = COMMENT_MARKER.format(run_tag=ctx["run_tag"])
    head = (f"{marker}\n### 代码公证处 · 审计结论（run `{ctx['run_tag']}`）\n\n"
            f"公证对象：`{ctx['head_sha'][:7]}`　契约：{ctx['contract_line']}\n\n")
    if state == "NOTARIZED":
        return head + (
            "**结论：验收通过（NOTARIZED）** ✅\n\n"
            "| 门禁 | 结果 |\n|---|---|\n" + ctx["gate_rows"] + "\n"
            f"证书与本次提交严格绑定（`{ctx['head_sha'][:7]}`）；"
            "验收 ≠ 合并 ≠ 发布——合并动作由团队负责人执行。\n"
            f"证据包（含复算指引）：见本 PR 的 Actions artifact "
            f"`evidence-pack-pr{ctx['pr']}`。")
    if state == "ESCALATED":
        return head + (
            "**结论：升级人工裁决（ESCALATED）** ⏸\n\n"
            "本次审计遇到需要人工裁定的事项（通常是契约条款的需求依据）。"
            "系统按设计暂停等待——这不是报错。\n\n"
            f"争议焦点：{ctx.get('dispute_focus', '见裁决卡')}\n\n"
            "裁决人请在裁决通道处理；裁决落地后，推送新提交或手动重跑 "
            "本工作流即可继续。PR 保持阻塞，无人可以绕过。")
    if state == "REJECTED":
        return head + (
            "**结论：暂不可放行（REJECTED）** ❌\n\n"
            + ctx.get("failure_section", "") + "\n"
            "**修复指引**：" + ctx.get("fix_hint", "见上方失败详情") + "\n\n"
            "**异议通道**：以上判定依据验收契约 v"
            f"{ctx.get('contract_version', '?')}；若认为契约条款本身缺乏"
            "需求依据，可在本 PR 评论 `/notary dispute <争议焦点>` "
            "发起人工裁决——争议对象是规则，不是检验过程。\n"
            f"证据包：Actions artifact `evidence-pack-pr{ctx['pr']}`。")
    return head + f"**结论：{state}**（未识别的中间态，请联系公证处值班）"


def render_insufficient_comment(ctx: dict) -> str:
    marker = COMMENT_MARKER.format(run_tag=ctx["run_tag"])
    return (f"{marker}\n### 代码公证处 · 受理回执（run `{ctx['run_tag']}`）\n\n"
            "**暂不受理：送审材料不足（INSUFFICIENT）** ❌\n\n"
            "公证处不是只发证的机构——材料不齐时说明缺什么，同样是为你好。\n\n"
            "本次送审缺少：**预期行为 / 验收条件**。请按模板补充 PR 描述后"
            "重新送审：\n\n"
            "```\n## 预期行为\n<改动后系统应当如何表现>\n\n## 验收条件\n"
            "<可检验的判定标准（什么算修好）>\n\n## 影响面\n<触及哪些模块/下游>\n```\n")


# ---------------------------------------------------------------------------
# Writeback
# ---------------------------------------------------------------------------

def writeback(repo: str, pr: str, head_sha: str, run_tag: str, state: str,
              body: str, token: str | None, local: bool) -> None:
    status = {"NOTARIZED": ("success", "验收通过，证书已签发"),
              "REJECTED": ("failure", "未通过验收，见 PR 评论返工工单"),
              "ESCALATED": ("pending", "等待人工裁决")}.get(
                  state, ("pending", f"pipeline {state}"))
    if local:
        print(f"\n[local] commit status {head_sha[:7]} -> "
              f"{status[0]} ({STATUS_CONTEXT}: {status[1]})")
        print(f"[local] PR #{pr} comment (in-place, run={run_tag}):\n")
        print(body)
        return
    gh_api(repo, "POST", f"/statuses/{head_sha}", {
        "state": status[0], "context": STATUS_CONTEXT,
        "description": status[1]}, token)
    marker = COMMENT_MARKER.format(run_tag=run_tag)
    comments = gh_api(repo, "GET", f"/issues/{pr}/comments", token=token)
    mine = next((c for c in comments if marker in c.get("body", "")), None)
    if mine:
        gh_api(repo, "PATCH", f"/issues/comments/{mine['id']}",
               {"body": body}, token)
        print(f"comment updated in place (id={mine['id']})")
    else:
        gh_api(repo, "POST", f"/issues/{pr}/comments", {"body": body}, token)
        print("comment created")


def pack_evidence(notary_dir: Path, sid: str, out: Path) -> None:
    run_dir = notary_dir / "runs" / sid
    scenario = notary_dir / "scenarios" / f"{sid}.json"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(run_dir.rglob("*")):
            if f.is_file() and "work" not in f.parts:
                zf.write(f, f.relative_to(notary_dir))
        if scenario.exists():
            zf.write(scenario, scenario.relative_to(notary_dir))
    print(f"evidence pack -> {out}")


# ---------------------------------------------------------------------------
# Main drive
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--run-prefix", required=True)
    ap.add_argument("--spec", help=".notary/audit/<tag>.json; absent -> "
                                   "insufficient-materials fast path")
    ap.add_argument("--notary-dir", default="notary")
    ap.add_argument("--evidence-out", default="evidence-pack.zip")
    ap.add_argument("--port", type=int, default=18096)
    ap.add_argument("--local", action="store_true",
                    help="rehearsal: print GitHub writes instead of posting")
    args = ap.parse_args()
    token = os.environ.get("GH_TOKEN")
    if not args.local and not token:
        raise SystemExit("GH_TOKEN required (or pass --local)")

    run_tag = args.run_prefix
    ctx = {"run_tag": run_tag, "head_sha": args.head_sha, "pr": args.pr}

    # -- insufficient-materials fast path (受理窗口形式审查) ------------------
    if not args.spec or not Path(args.spec).exists():
        print("no audit spec for this PR -> INSUFFICIENT fast path")
        writeback(args.repo, args.pr, args.head_sha, run_tag, "INSUFFICIENT",
                  render_insufficient_comment(ctx), token, args.local)
        return
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    run_tag = spec.get("run_tag", run_tag)
    ctx["run_tag"] = run_tag

    # -- collect the submitted change from the PR checkout -------------------
    file_map = spec["file_map"]  # repo path -> flat target name
    files: dict[str, str] = {}
    for repo_path, flat in file_map.items():
        p = Path(repo_path)
        if not p.exists():
            raise AuditFailure(f"spec references missing file {repo_path}")
        files[flat] = p.read_text(encoding="utf-8")

    notary_dir = Path(args.notary_dir).resolve()
    proc = start_gateway(notary_dir, args.port)
    base = f"http://127.0.0.1:{args.port}"
    sid = None
    try:
        # -- intake (idempotent by content hash) ------------------------------
        intake = dict(spec["intake"])
        intake["target"] = spec["target"]
        intake["files"] = files
        body = gw_call(base, "intake", "notary_intake.submit_issue", intake,
                       tolerate=("already intaken",))
        if body.get("ok"):
            sid = body["result"]["scenario_id"]
        else:
            # Same content + same tag = same scenario (webhook retry). A
            # re-run is a RE-AUDIT from scratch: reset the restored run so
            # the spec drives from RECEIVED again; the PR comment is still
            # updated in place keyed by run_tag.
            sid = "intake_" + __import__("hashlib").sha256(
                (intake["title"] + intake["report"]
                 + intake["expected_behavior"]
                 + str(intake.get("run_tag", ""))).encode()).hexdigest()[:10]
            gw_call(base, sid, "reset")
            print(f"  scenario {sid} already intaken -> reset for re-audit")
        print(f"scenario: {sid} (run {run_tag})")

        # -- drive the pipeline per spec -------------------------------------
        for step in spec["steps"]:
            tool = step["tool"]
            payload = dict(step.get("payload") or {})
            if step.get("role"):
                payload["role"] = step["role"]
            body = gw_call(base, sid, tool, payload,
                           tolerate=tuple(step.get("tolerate", ())))
            result = body.get("result", body)
            state = result.get("pipeline_state", "")
            print(f"  {tool}: {'ok' if body.get('ok') else 'ERR'} {state}")
            if not body.get("ok"):
                raise AuditFailure(f"spec step {tool}: {result.get('error')}")

        state = gw_call(base, sid, "notary_state.get")["result"]
        final = state["state"]
        expected = spec.get("expect", {}).get("final_state")
        if expected and final != expected:
            raise AuditFailure(f"final state {final} != expected {expected}")
        contract = state.get("contract_version")
        ctx["contract_version"] = contract
        ctx["contract_line"] = spec.get("contract_label") or \
            (f"v{contract}" if contract else "未冻结")

        # -- verdict details for the comment ----------------------------------
        verdicts = gw_call(base, sid, "notary_verdicts.list",
                           {"role": "leader"})["result"].get(
                               "verdicts", {})
        rows, failures = [], []
        for v in verdicts.values():
            icon = {"green": "🟢", "red": "🔴", "yellow": "🟡"}.get(
                v.get("decision"), "⚪")
            rows.append(f"| {v.get('gate')} | {icon} {v.get('decision')} — "
                        f"{v.get('summary', '')[:60]} |")
            if v.get("decision") == "red":
                failures.append(f"- 门禁 **{v.get('gate')}**："
                                f"{v.get('summary', '')}")
        ctx["gate_rows"] = "\n".join(rows)
        ctx["failure_section"] = "**失败详情**：\n" + "\n".join(failures) \
            if failures else ""
        ctx["fix_hint"] = spec.get("fix_hint", "见失败详情")
        disp = {}
        disp_path = notary_dir / "runs" / sid / "dispute.json"
        if disp_path.exists():
            disp = json.loads(disp_path.read_text())[-1]
        ctx["dispute_focus"] = disp.get("focus", "")

        seal = gw_call(base, sid, "notary_evidence.seal",
                       {"role": "release"}, tolerate=())
        print(f"  sealed: {seal['result'].get('sealed_files', '?')} files")

        pack_evidence(notary_dir, sid, Path(args.evidence_out))
        writeback(args.repo, args.pr, args.head_sha, run_tag, final,
                  render_comment(final, ctx), token, args.local)
        print(f"\nDONE: run {run_tag} -> {final}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    main()
