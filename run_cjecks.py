#!/usr/bin/env python3
"""Run health check, unit tests and eval suites; store everything under results/.

Usage (from the project root):
    python run_checks.py                       # everything
    python run_checks.py --tag qwen3-8b        # label the run (shows up in history.csv)
    python run_checks.py --categories shell file
    python run_checks.py --skip-eval           # unit tests + health only
    python run_checks.py --force               # continue even if health fails

Model selection comes from XALGV_* environment variables (see config.py),
so set them before running this script.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CATEGORIES = ["shell", "file", "python", "browser", "combined"]
PY = sys.executable


def run(cmd: list[str], out_file: Path, timeout: int | None = None) -> int:
    """Run a command, stream output to console and save it to out_file."""
    print(f"\n$ {' '.join(cmd)}")
    with out_file.open("w", encoding="utf-8") as fh:
        try:
            proc = subprocess.Popen(
                cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError as exc:
            msg = f"command not found: {exc}\n"
            print(msg)
            fh.write(msg)
            return 127
        start = time.time()
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            fh.write(line)
            if timeout and time.time() - start > timeout:
                proc.kill()
                fh.write(f"\n[run_checks] killed after {timeout}s timeout\n")
                return 124
        return proc.wait()


def capture(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"(unavailable: {exc})"


class VramSampler(threading.Thread):
    """Samples GPU memory used (MiB) via nvidia-smi while a command runs."""

    def __init__(self, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self.peak = 0
        self._halt = threading.Event()
        self.available = shutil.which("nvidia-smi") is not None

    def run(self) -> None:
        while self.available and not self._halt.is_set():
            out = capture(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"])
            try:
                self.peak = max(self.peak, int(out.splitlines()[0].strip()))
            except (ValueError, IndexError):
                pass
            self._halt.wait(self.interval)

    def stop(self) -> int:
        self._halt.set()
        return self.peak


def model_roles() -> dict:
    sys.path.insert(0, str(ROOT))
    try:
        from local_agent.config import AgentConfig

        cfg = AgentConfig.from_env()
        return {
            role: {
                "model": rc.model, "num_ctx": rc.context_length,
                "temperature": rc.temperature, "thinking": rc.thinking,
            }
            for role, rc in cfg.model_roles.items()
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def summarize(report_path: Path) -> dict:
    data = json.loads(report_path.read_text(encoding="utf-8"))
    results = data.get("results", [])
    steps = [r.get("steps_taken", 0) for r in results]
    failures = []
    for r in results:
        if r.get("passed"):
            continue
        reason = r.get("error")
        if not reason:
            bad = [c for c in r.get("checker_results", []) if not c.get("passed", False)]
            reason = "; ".join(str(c.get("description") or c.get("path") or c.get("error") or c.get("type")) for c in bad)
        failures.append({"task_id": r.get("task_id"), "steps": r.get("steps_taken"), "reason": reason or "unknown"})
    return {
        "total": data.get("total_tasks", len(results)),
        "passed": data.get("passed", 0),
        "failed": data.get("failed", 0),
        "errors": data.get("errors", 0),
        "success_rate": data.get("success_rate", 0.0),
        "wall_time_s": data.get("wall_time_seconds", 0.0),
        "avg_steps": round(sum(steps) / len(steps), 1) if steps else 0,
        "failures": failures,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="", help="label for this run, e.g. the model name")
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    ap.add_argument("--skip-unit", action="store_true")
    ap.add_argument("--skip-eval", action="store_true")
    ap.add_argument("--force", action="store_true", help="continue even if health check fails")
    ap.add_argument("--timeout", type=int, default=3600, help="per-category eval timeout (s)")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / "results" / (stamp + (f"_{args.tag}" if args.tag else ""))
    out.mkdir(parents=True, exist_ok=True)
    print(f"Results directory: {out}")

    # 1. Environment snapshot (so results are reproducible and comparable).
    env = {
        "timestamp": stamp,
        "tag": args.tag,
        "python": sys.version.split()[0],
        "git_commit": capture(["git", "rev-parse", "--short", "HEAD"]),
        "git_dirty": bool(capture(["git", "status", "--porcelain"])),
        "model_roles": model_roles(),
        "ollama_list": capture(["ollama", "list"]),
        "gpu": capture(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
        "docker_ps": capture(["docker", "compose", "ps", "--format", "table"]),
    }
    (out / "environment.json").write_text(json.dumps(env, indent=2), encoding="utf-8")

    summary: dict = {"environment": env, "unit_tests": None, "health": None, "eval": {}}

    # 2. Health (Ollama + sandbox reachable).
    rc = run([PY, "-m", "local_agent.main", "health"], out / "health.txt")
    summary["health"] = "ok" if rc == 0 else "FAILED"
    if rc != 0 and not args.force and not args.skip_eval:
        print("\nHealth check failed: start Ollama and `docker compose up -d`, or use --force.")
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        return 2

    # 3. Unit tests.
    if not args.skip_unit:
        rc = run([PY, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"], out / "unit_tests.txt")
        tail = (out / "unit_tests.txt").read_text(encoding="utf-8").strip().splitlines()
        summary["unit_tests"] = {"exit_code": rc, "result": tail[-1] if tail else ""}

    # 4. Eval suites, one category at a time, with peak VRAM sampling.
    if not args.skip_eval:
        report_src = ROOT / "traces" / "eval_report.json"
        for cat in args.categories:
            if report_src.exists():
                report_src.unlink()  # avoid copying a stale report
            sampler = VramSampler()
            sampler.start()
            t0 = time.time()
            rc = run([PY, "-m", "local_agent.main", "-v", "eval", "--category", cat],
                     out / f"eval_{cat}.log", timeout=args.timeout)
            peak = sampler.stop()
            if report_src.exists():
                shutil.copy(report_src, out / f"eval_{cat}.json")
                s = summarize(out / f"eval_{cat}.json")
            else:
                s = {"total": 0, "passed": 0, "failed": 0, "errors": 0, "success_rate": 0.0,
                     "wall_time_s": round(time.time() - t0, 1), "avg_steps": 0,
                     "failures": [{"task_id": "-", "steps": 0, "reason": f"no report produced (exit {rc})"}]}
            s["peak_vram_mib"] = peak or None
            s["exit_code"] = rc
            summary["eval"][cat] = s

        db = ROOT / "data" / "agent.db"
        if db.exists():
            shutil.copy(db, out / "agent.db")  # run + step traces

    # 5. Write summary.json, summary.md and append to history.csv.
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [f"# Run {stamp} {args.tag}".strip(), ""]
    lines.append(f"- Health: {summary['health']}")
    if summary["unit_tests"]:
        lines.append(f"- Unit tests: {summary['unit_tests']['result']}")
    lines.append(f"- Models: " + ", ".join(
        f"{r}={v['model']}" for r, v in env["model_roles"].items() if isinstance(v, dict) and r in ("planner", "executor")))
    lines += ["", "| Category | Passed | Rate | Avg steps | Time (s) | Peak VRAM (MiB) |", "|---|---|---|---|---|---|"]
    for cat, s in summary["eval"].items():
        lines.append(f"| {cat} | {s['passed']}/{s['total']} | {s['success_rate']:.0%} | {s['avg_steps']} | "
                     f"{s['wall_time_s']} | {s['peak_vram_mib'] or 'n/a'} |")
    fails = [(c, f) for c, s in summary["eval"].items() for f in s["failures"]]
    if fails:
        lines += ["", "## Failures", ""]
        lines += [f"- [{c}] {f['task_id']} ({f['steps']} steps): {f['reason']}" for c, f in fails]
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    hist = ROOT / "results" / "history.csv"
    new = not hist.exists()
    with hist.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["timestamp", "tag", "git", "executor_model", "category", "passed", "total",
                        "success_rate", "avg_steps", "wall_time_s", "peak_vram_mib"])
        ex = env["model_roles"].get("executor", {}).get("model", "?") if isinstance(env["model_roles"], dict) else "?"
        for cat, s in summary["eval"].items():
            w.writerow([stamp, args.tag, env["git_commit"], ex, cat, s["passed"], s["total"],
                        s["success_rate"], s["avg_steps"], s["wall_time_s"], s["peak_vram_mib"]])

    print("\n" + "\n".join(lines))
    print(f"\nSaved to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())