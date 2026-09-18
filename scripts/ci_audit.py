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
import re
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
        nar = ctx.get("narrative", {})
        passed_note = nar.get("passed_note")
        return head + (
            "**✅ 公证通过，这张 PR 可以合并了**\n\n"
            "三项检验全部通过：\n\n"
            + ctx["gate_rows"] + "\n\n"
            + (passed_note + "\n\n" if passed_note else "")
            + f"这张证书绑定了当前代码版本（`{ctx['head_sha'][:7]}`）"
            "与规则版本，任何一方再变都会自动失效，"
            "不会出现“审完之后又改”的空档。\n\n"
            "⚠️ **一句重要的话**：这是系统的**验收结论**，不是发布动作。"
            "是否合并由团队负责人决定，生产发布仍走你们原有的 CD 审批——"
            "我们只负责让“可以合并”这四个字有据可查。\n\n"
            f"<details><summary>技术详情</summary>\n\nRun `{ctx['run_tag']}`"
            f" ｜ 契约 {ctx['contract_line']} ｜ 复算："
            "`make verify EVIDENCE=<证据包>`\n</details>")
    if state == "ESCALATED":
        return head + (
            "**结论：升级人工裁决（ESCALATED）** ⏸\n\n"
            "本次审计遇到需要人工裁定的事项（通常是契约条款的需求依据）。"
            "系统按设计暂停等待——这不是报错。\n\n"
            f"争议焦点：{ctx.get('dispute_focus', '见裁决卡')}\n\n"
            "裁决人请在裁决通道处理；裁决落地后，推送新提交或手动重跑 "
            "本工作流即可继续。PR 保持阻塞，无人可以绕过。")
    if state == "REJECTED":
        nar = ctx.get("narrative", {})
        ack = nar.get("acknowledge")
        how = nar.get("how_found")
        scene = nar.get("failure_scene")
        body_mid = ""
        if ack:
            body_mid += f"先说结论：{ack}\n\n"
        if how:
            body_mid += f"这个问题是我们这样发现的：{how}\n\n"
        if scene:
            body_mid += f"**失败场景**（事实）：{scene}\n\n"
        failed_tests = ctx.get("failed_tests") or []
        if failed_tests:
            body_mid += "**未通过的检验用例**：" + "、".join(
                f"`{t}`" for t in failed_tests) + "\n\n"
        return head + (
            "**⛔ 这张 PR 暂时不能合并——我们在验收规则边界上发现了"
            "一个需要你处理的问题**\n\n"
            + body_mid
            + "**你现在有三个选择：**\n\n"
            "🔧 **修复后重新提交**——如果你认可规则解读："
            + ctx.get("fix_hint", "按失败详情修复").rstrip("。.") +
            "。push 后系统会自动从头完整重验。\n\n"
            "⚖️ **发起争议裁决**——如果你认为**规则本身**的解读有问题，"
            "回复 `/notary dispute` 加上你的理由，任务会升级给人工裁决。"
            "注意：争议针对的是规则，不是这次的检验过程——"
            "检验过程的全部证据都可复算。\n\n"
            "📎 **查看证据**——失败用例的完整输出、代码差异、规则原文，"
            "都在证据包里（Actions artifact "
            f"`evidence-pack-pr{ctx['pr']}`），欢迎先核实再决定。\n\n"
            "---\n<details><summary>技术详情</summary>\n\n"
            f"Run `{ctx['run_tag']}` ｜ 契约 "
            f"v{ctx.get('contract_version','?')} ｜ 公证对象 "
            f"`{ctx['head_sha'][:7]}`\n\n"
            + ctx.get("gate_rows", "") + "\n\n"
            "本结论由确定性检验产出，无模型参与裁决；"
            "复算：`make verify EVIDENCE=<证据包>`\n</details>")
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


def pack_evidence(notary_dir: Path, sid: str, out: Path,
                  run_tag: str = "", head_sha: str = "") -> None:
    """Self-contained EvidencePack: run dir + scenario + target sources +
    config snapshot + pack meta, so `make verify` recomputes anywhere."""
    run_dir = notary_dir / "runs" / sid
    scenario = notary_dir / "scenarios" / f"{sid}.json"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(run_dir.rglob("*")):
            if f.is_file() and "work" not in f.parts:
                zf.write(f, f.relative_to(notary_dir))
        if scenario.exists():
            zf.write(scenario, scenario.relative_to(notary_dir))
            spec = json.loads(scenario.read_text(encoding="utf-8"))
            if not spec.get("embedded_target_files"):
                tdir = notary_dir / "tools" / "notary_target"
                for name in (spec.get("target_files", [])
                             + spec.get("baseline_test_files", [])):
                    src = tdir / name
                    if src.exists():
                        zf.write(src, f"target/{name}")
        cfg = notary_dir / "notary.json"
        if cfg.exists():
            zf.write(cfg, "notary.json.snapshot")
        zf.writestr("pack_meta.json", json.dumps({
            "run_tag": run_tag, "head_sha": head_sha,
            "scenario": sid, "created": time.time(),
            "verify": "make verify EVIDENCE=<this pack>  "
                      "(零 LLM 复算：签名→哈希链→契约→门禁重跑比对)"},
            ensure_ascii=False, indent=2))
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

        # -- bind this run to the audited commit BEFORE sealing ---------------
        # The gateway is VCS-agnostic by design; the PR binding is what lets
        # merge-readiness refuse a certificate whose audited SHA is not the
        # current head ("审完又改" protection). Sealed into the manifest.
        binding = {"repo": args.repo, "pr": int(args.pr),
                   "head_sha": args.head_sha, "run_tag": run_tag,
                   "bound_at": time.time()}
        (notary_dir / "runs" / sid / "evidence").mkdir(parents=True,
                                                       exist_ok=True)
        (notary_dir / "runs" / sid / "evidence" / "pr_binding.json").write_text(
            json.dumps(binding, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

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
        ctx["narrative"] = spec.get("narrative", {})
        tp_out = verdicts.get("test_pass", {}).get("test_output", "")
        ctx["failed_tests"] = sorted(set(
            re.findall(r"(?:FAIL|ERROR): (\w+)", tp_out)))
        disp = {}
        disp_path = notary_dir / "runs" / sid / "dispute.json"
        if disp_path.exists():
            disp = json.loads(disp_path.read_text(encoding="utf-8"))[-1]
        ctx["dispute_focus"] = disp.get("focus", "")

        seal = gw_call(base, sid, "notary_evidence.seal",
                       {"role": "release"}, tolerate=())
        print(f"  sealed: {seal['result'].get('sealed_files', '?')} files")

        pack_evidence(notary_dir, sid, Path(args.evidence_out),
                      run_tag=run_tag, head_sha=args.head_sha)
        writeback(args.repo, args.pr, args.head_sha, run_tag, final,
                  render_comment(final, ctx), token, args.local)
        print(f"\nDONE: run {run_tag} -> {final}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# PR 通知回写（A3 异议受理 / A4 裁决结果）：dispute 与裁决落在网关侧，
# PR 评论由这里补写——由人工或 Poller 触发，评论带独立幂等锚点。
# ---------------------------------------------------------------------------

def notify_main(argv: list[str]) -> None:
    ap = argparse.ArgumentParser(description="PR notification writeback")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--run-tag", required=True)
    ap.add_argument("--kind", required=True,
                    choices=["dispute_received", "adjudication_result"])
    ap.add_argument("--notary-dir", default="notary")
    ap.add_argument("--sid", required=True)
    ap.add_argument("--local", action="store_true")
    args = ap.parse_args(argv)
    token = os.environ.get("GH_TOKEN")
    if not args.local and not token:
        raise SystemExit("GH_TOKEN required (or --local)")

    run_dir = Path(args.notary_dir) / "runs" / args.sid
    marker = COMMENT_MARKER.format(run_tag=args.run_tag + "-notice")
    if args.kind == "dispute_received":
        d = json.loads((run_dir / "dispute.json").read_text(encoding="utf-8"))[-1]
        body = (f"{marker}\n### ⚖️ 你的争议已被受理，任务进入人工裁决\n\n"
                "谢谢你明确提出异议——这正是这个通道存在的意义。"
                "你质疑的不是检验结果，而是**规则条款的立项依据**；"
                "这类问题不该由系统自答，已升级给有裁决权的同事。\n\n"
                f"你的异议原文：「{d['focus']}」\n\n"
                "接下来：异议原文、工单原文、契约全文、失败用例输出会一并"
                "呈现在裁决人面前；裁决期间本 PR 保持“不可合并”，"
                "不会有人绕过它。结果一出，我会在这里通知你——"
                "含结论、理由、以及对你的具体要求。\n\n"
                f"争议编号 dispute-{d.get('ts', 0):.0f}。"
                "你不需要做任何等待操作。")
    else:
        a = json.loads((run_dir / "adjudication.json").read_text(encoding="utf-8"))[-1]
        LABEL = {"uphold": "维持契约", "revise": "修订契约",
                 "request_evidence": "要求补充证据", "override": "特批放行"}
        refs = "\n".join(f"- {r}" for r in a.get("references", []))
        body = (f"{marker}\n### ⚖️ 裁决出结果了："
                f"{LABEL.get(a['decision'], a['decision'])}\n\n"
                f"裁决人 {a.get('actor')} 的裁定：{a.get('rationale', '')}\n\n"
                + ("**依据附件**：\n" + refs + "\n\n" if refs else "")
                + "裁决全文与引用材料已永久写入证据链，可查验、可复算。\n\n"
                "**下一步**：按裁决后的规则修复并 push，系统会自动从头"
                "完整重验，结果贴在这里。\n\n"
                f"<details><summary>技术详情</summary>\n\n"
                f"裁决记录 #{a.get('id')} ｜ 通道 {a.get('channel')} ｜ "
                f"契约 v{a.get('contract_version')}（哈希 "
                f"{(a.get('contract_hash') or '')[:12]}…）\n</details>")
    if args.local:
        print(body)
        return
    gh_api(args.repo, "POST", f"/issues/{args.pr}/comments",
           {"body": body}, token)
    print(f"notification posted to PR #{args.pr}")


def _force_utf8_stdio() -> None:
    """Cross-platform output safety: Chinese Windows consoles are GBK, and
    printing Unicode status marks would crash the process. Force UTF-8."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == "notify":
        notify_main(_sys.argv[2:])
    else:
        _force_utf8_stdio()
        main()
