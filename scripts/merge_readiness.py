#!/usr/bin/env python3
"""Merge-readiness check ("merge 就绪检查" 薄工具).

Deterministic, no LLM. Run by the release role before anyone clicks merge.
A NOTARIZED certificate releases the merge GATE, not the merge itself —
this card answers one question: "can THIS commit be merged NOW?"

Checks (fail-closed, in order):
  1. 证书有效     — certificate.md present, run ended NOTARIZED,
                    contract frozen_hash recomputes, manifest seals match
  2. SHA 绑定     — pr_binding.head_sha == current PR head (审完又改 = 作废)
  3. 无冲突       — git merge-tree --write-tree <base> <head> clean
  4. 分支不落后   — base tip already reachable from head
  5. 必需 check   — codenotary-audit status = success on head SHA

Verdict card: 可合并 ✅ / 需 rebase ⚠️ / 有冲突 ❌ / 证据失效 ⛔
Exit code 0 iff 可合并.

Usage:
  python3 scripts/merge_readiness.py --repo-dir . --base main \
      --head-sha <sha> --evidence runs/<sid>  (or evidence-pack.zip) \
      [--repo OWNER/REPO] [--local]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.dont_write_bytecode = True


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


def merge_conflict(repo: Path, base: str, head: str) -> tuple[bool, str]:
    """True iff merging head into base conflicts. Prefers `merge-tree
    --write-tree` (git >= 2.38); falls back to a scratch-worktree merge."""
    for rev in (base, head):
        if git(repo, "rev-parse", "--verify", "--quiet", rev).returncode != 0:
            raise SystemExit(f"git 对象 {rev} 不在本地，先 git fetch")
    mt = git(repo, "merge-tree", "--write-tree", base, head)
    unsupported = mt.returncode != 0 and any(
        s in (mt.stderr + mt.stdout)
        for s in ("unknown rev", "unknown option", "usage:"))
    if mt.returncode != 0 and not unsupported:
        detail = (mt.stdout.strip().splitlines() or ["冲突"])[-1]
        return True, detail
    if mt.returncode == 0:
        return False, "干净"
    # legacy fallback: throwaway worktree, real merge attempt
    scratch = Path(tempfile.mkdtemp(prefix="mergecheck-"))
    try:
        add = git(repo, "worktree", "add", "--detach", str(scratch), head)
        if add.returncode != 0:
            raise SystemExit(f"git worktree add 失败: {add.stderr.strip()}")
        m = git(scratch, "merge", "--no-commit", "--no-ff", base)
        git(scratch, "merge", "--abort")
        if m.returncode == 0:
            return False, "干净（scratch worktree 实测）"
        conflicts = [l for l in git(scratch, "diff", "--name-only",
                                    "--diff-filter=U").stdout.splitlines()
                     if l.strip()]
        return True, f"冲突文件: {', '.join(conflicts) or '见 merge 输出'}"
    finally:
        git(repo, "worktree", "remove", "--force", str(scratch))


# --- check 1: certificate validity ------------------------------------------

def check_certificate(run_dir: Path) -> tuple[bool, list[str]]:
    """Independent recomputation — mirrors the gateway's canonical hash
    on purpose (verification must not share code with what it verifies)."""
    problems: list[str] = []
    cert = run_dir / "certificate.md"
    cp = run_dir / "checkpoint.json"
    contract_p = run_dir / "contract.json"
    manifest_p = run_dir / "manifest.json"
    for p in (cert, cp, contract_p, manifest_p):
        if not p.exists():
            problems.append(f"缺文件 {p.name}")
    if problems:
        return False, problems
    sm_state = json.loads(cp.read_text())["sm"]["state"]
    if sm_state != "NOTARIZED":
        problems.append(f"run 终态是 {sm_state}，不是 NOTARIZED")
    contract = json.loads(contract_p.read_text())
    canonical = {"issue_id": contract.get("issue_id", ""),
                 "assertions": contract.get("assertions") or [],
                 "context_refs": contract.get("context_refs")
                 or [str(run_dir / "diagnosis.json")]}  # legacy packs
    assumptions = contract.get("assumptions") or []
    if assumptions:
        canonical["assumptions"] = assumptions
    recomputed = sha256_text(json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if recomputed != contract.get("frozen_hash"):
        problems.append("契约 frozen_hash 重算不符（内容被改或版本不对）")
    manifest = json.loads(manifest_p.read_text())
    files = manifest.get("files", manifest)  # new sealed format or legacy flat
    for rel in ("certificate.md", "contract.json", "evidence/pr_binding.json"):
        if rel not in files:
            problems.append(f"manifest 未封印 {rel}")
            continue
        actual = sha256_text(
            (run_dir / rel).read_bytes().decode("utf-8", errors="replace"))
        if files[rel] != actual:
            problems.append(f"{rel} 与封印清单哈希不符（证据被篡改）")
    return not problems, problems


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-dir", required=True)
    ap.add_argument("--base", default="main")
    ap.add_argument("--head-sha", required=True)
    ap.add_argument("--evidence", required=True,
                    help="run dir or evidence-pack.zip")
    ap.add_argument("--repo", help="OWNER/REPO for check-status lookup")
    ap.add_argument("--local", action="store_true",
                    help="skip GitHub check-status lookup (rehearsal)")
    args = ap.parse_args()

    ev = Path(args.evidence)
    tmp = None
    if ev.suffix == ".zip":
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(ev) as zf:
            zf.extractall(tmp.name)
        runs = [d for d in (Path(tmp.name) / "runs").iterdir() if d.is_dir()]
        if len(runs) != 1:
            raise SystemExit(f"evidence pack 应含且仅含一个 run，实际 {len(runs)}")
        run_dir = runs[0]
    else:
        run_dir = ev

    checks: list[tuple[str, bool, str]] = []

    # 1. certificate
    ok, problems = check_certificate(run_dir)
    checks.append(("证书有效（NOTARIZED ∧ 契约哈希 ∧ 封印一致）", ok,
                   "；".join(problems) if problems else "证书与证据封印核验通过"))

    # 2. SHA binding
    binding_p = run_dir / "evidence" / "pr_binding.json"
    if binding_p.exists():
        binding = json.loads(binding_p.read_text())
        bound = binding.get("head_sha", "")
        sha_ok = bool(bound) and bool(args.head_sha) and (
            args.head_sha.startswith(bound) or bound.startswith(args.head_sha))
        checks.append(("证书绑定当前 PR head（防审完又改）", sha_ok,
                       f"绑定 {bound[:12]} vs 当前 {args.head_sha[:12]}"))
    else:
        checks.append(("证书绑定当前 PR head（防审完又改）", False,
                       "缺 evidence/pr_binding.json"))

    # 3 & 4. git checks
    repo = Path(args.repo_dir)
    head = args.head_sha
    base = args.base
    conflict, detail = merge_conflict(repo, base, head)
    checks.append(("git merge 无冲突", not conflict, detail))
    behind_n = git(repo, "rev-list", "--count", f"{head}..{base}").stdout
    behind = behind_n.strip().isdigit() and int(behind_n.strip()) > 0
    checks.append((f"分支不落后于 {base}", not behind,
                   "已是最新" if not behind else f"落后 {behind_n.strip()} 个提交"))

    # 5. required check
    if args.local:
        checks.append(("必需 check codenotary-audit = success", True,
                       "--local 彩排跳过 GitHub 查询"))
    else:
        if not args.repo:
            raise SystemExit("非 --local 模式需要 --repo")
        import os
        import urllib.request
        token = os.environ.get("GH_TOKEN")
        req = urllib.request.Request(
            f"https://api.github.com/repos/{args.repo}/commits/"
            f"{head}/check-runs?per_page=100",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            runs = json.loads(resp.read()).get("check_runs", [])
        audit = [r for r in runs if r["name"] == "codenotary-audit"] or \
            [r for r in runs if r.get("external_id", "").endswith("audit")]
        ok = bool(audit) and all(
            r.get("conclusion") == "success" for r in audit)
        checks.append(("必需 check codenotary-audit = success", ok,
                       f"{len(audit)} 个 check" if audit else "未找到 check"))

    # --- verdict --------------------------------------------------------------
    cert_ok = checks[0][1] and checks[1][1] and checks[4][1]
    git_ok = checks[2][1] and checks[3][1]
    if not cert_ok:
        verdict, icon = "证据失效，不可合并", "⛔"
    elif not checks[2][1]:
        verdict, icon = "有冲突，需人工解冲突后重新送审", "❌"
    elif not checks[3][1]:
        verdict, icon = ("需 rebase：基线已前进，rebase 后证书作废，"
                         "将快速重验（diff 等价性检查 + 门禁重跑）"), "⚠️"
    else:
        verdict, icon = "可合并", "✅"

    print("\n┌─ 合并就绪意见卡 ─────────────────────────")
    for name, ok, detail in checks:
        print(f"│  {'✅' if ok else '❌'} {name}")
        print(f"│     {detail}")
    print("├─────────────────────────────────────────")
    print(f"│  {icon} 结论：{verdict}")
    print("│  注意：验收 ≠ 合并 ≠ 发布——本卡只是意见，点合并的是人。")
    print("└─────────────────────────────────────────")
    if tmp:
        tmp.cleanup()
    sys.exit(0 if (cert_ok and git_ok) else 1)


if __name__ == "__main__":
    main()
