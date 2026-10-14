from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from bau.config import Config
from bau.outcome import too_big_reason
from bau.telemetry import RateLimit, parse_stream_line, rate_limit_from_event, result_hit_limit


@dataclass(frozen=True)
class SessionResult:
    outcome: str
    reason: str = ""
    latest: RateLimit | None = None
    cost_usd: float = 0.0
    result_text: str = ""


def docker_command(
    cfg: Config,
    *,
    name: str,
    checkout: Path,
    bare: Path | None,
    model: str,
    budget: float,
    env_file: str | None = None,
    docker: str = "docker",
) -> list[str]:
    cmd = [
        docker,
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--network",
        cfg.container.network,
        "--label",
        "bau.session=1",
        "--memory",
        "2500m",
        "--pids-limit",
        "1024",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]
    if bare is not None:
        cmd += ["-v", f"{bare}:{bare}:ro"]
    cmd += [
        "-v",
        f"{checkout}:{checkout}",
        "-w",
        str(checkout),
        "-e",
        f"HTTPS_PROXY={cfg.container.proxy}",
        "-e",
        f"HTTP_PROXY={cfg.container.proxy}",
        "-e",
        "NODE_USE_ENV_PROXY=1",
    ]
    if env_file:
        cmd += ["--env-file", env_file]
    cmd += [
        cfg.container.image,
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--max-budget-usd",
        str(budget),
        "--dangerously-skip-permissions",
    ]
    return cmd


class SessionRunner:
    def __init__(
        self,
        cfg: Config,
        docker: str = "docker",
        monotonic=time.monotonic,
        wall_clock_minutes: int | None = None,
    ):
        self.cfg = cfg
        self.docker = docker
        self.monotonic = monotonic
        self.wall_clock_seconds = (wall_clock_minutes or cfg.limits.session_wall_clock_minutes) * 60

    def _kill(self, name: str) -> None:
        try:
            subprocess.run([self.docker, "kill", name], capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            pass

    def run(
        self,
        *,
        name: str,
        checkout: Path,
        bare: Path | None,
        brief: str,
        model: str,
        github_token: str,
        oauth_token: str,
        log_path: Path,
    ) -> SessionResult:
        for token in (github_token, oauth_token):
            if "\n" in token or "\r" in token:
                raise ValueError("session tokens must not contain newlines")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fd, env_file = tempfile.mkstemp(prefix="bau-env-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(
                    f"GH_TOKEN={github_token}\nGITHUB_TOKEN={github_token}\n"
                    f"CLAUDE_CODE_OAUTH_TOKEN={oauth_token}\n"
                )
            os.chmod(env_file, 0o600)
            cmd = docker_command(
                self.cfg,
                name=name,
                checkout=checkout,
                bare=bare,
                model=model,
                budget=self.cfg.limits.session_budget_usd,
                env_file=env_file,
                docker=self.docker,
            )
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
            assert proc.stdin and proc.stdout

            latest: RateLimit | None = None
            result: dict | None = None
            killed = ""
            started = self.monotonic()

            def watchdog():
                while proc.poll() is None:
                    if self.monotonic() - started > self.wall_clock_seconds:
                        nonlocal killed
                        killed = killed or "killed_wall_clock"
                        self._kill(name)
                        proc.kill()
                        return
                    time.sleep(0.2)

            def feed():
                # A child that never reads, or has exited, breaks the pipe: that is not an error here.
                try:
                    proc.stdin.write(brief)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass

            threading.Thread(target=watchdog, daemon=True).start()
            threading.Thread(target=feed, daemon=True).start()
            try:
                with open(log_path, "a", encoding="utf-8", errors="replace") as log:
                    for line in proc.stdout:
                        log.write(line)
                        obj = parse_stream_line(line)
                        if not obj:
                            continue
                        rl = rate_limit_from_event(obj)
                        if rl:
                            latest = rl
                            if rl.status == "rejected":
                                killed = killed or "rate_limited"
                                self._kill(name)
                                proc.kill()
                                break
                            if rl.five_hour and rl.five_hour.utilization >= self.cfg.limits.five_hour_kill:
                                killed = killed or "killed_five_hour"
                                self._kill(name)
                                proc.kill()
                                break
                        if obj.get("type") == "result":
                            result = obj
                code = proc.wait()
            except BaseException:
                # Anything going wrong mid-session must not leave the container running.
                self._kill(name)
                proc.kill()
                proc.wait()
                raise
            finally:
                proc.stdout.close()
        finally:
            os.unlink(env_file)

        if killed:
            return SessionResult(killed, "", latest)
        if result is None:
            return SessionResult("failed", f"no result line, exit {code}", latest)
        text = str(result.get("result", ""))
        cost = float(result.get("total_cost_usd") or 0.0)
        if result_hit_limit(result):
            return SessionResult("rate_limited", text, latest, cost, text)
        if result.get("is_error"):
            return SessionResult("failed", text[:300], latest, cost, text)
        reason = too_big_reason(text)
        if reason:
            return SessionResult("too_big", reason, latest, cost, text)
        if code != 0:
            return SessionResult("failed", f"exit {code}", latest, cost, text)
        return SessionResult("done", "", latest, cost, text)
