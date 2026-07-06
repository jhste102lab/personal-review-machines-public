from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from .config import Config


ENGINE_MENTIONS = {
    "glm": "opencode_glm",
    "미니맥스": "opencode_minimax",
    "딥시크": "opencode_deepseek",
    "지피티높음": "chatgpt_high",
    "지피티매우높음": "chatgpt_xhigh",
    "지피티확장": "chatgpt_extended",
    "클로드-p": "claude_p",
    "클로드": "claude",
    "코덱스": "codexcli",
    "최종리뷰": "codexcli_final",
}

ENGINE_IDENTITIES = {
    "opencode_glm": "@glm / OpenCode GLM",
    "opencode_minimax": "@미니맥스 / OpenCode MiniMax",
    "opencode_deepseek": "@딥시크 / OpenCode DeepSeek",
    "chatgpt_high": "@지피티높음 / ChatGPT Thinking High",
    "chatgpt_xhigh": "@지피티매우높음 / ChatGPT Extra High",
    "chatgpt_extended": "@지피티확장 / ChatGPT Pro Extended",
    "claude": "@클로드 / claude -p",
    "claude_p": "@클로드-p / claude-p",
    "codexcli": "@코덱스 / codex",
    "codexcli_final": "@최종리뷰 / codex",
}

ENGINE_MODEL_NAMES = {
    "opencode_glm": "GLM 5.2 max",
    "opencode_minimax": "MiniMax M3 thinking",
    "opencode_deepseek": "DeepSeek V4 Pro max",
    "chatgpt_high": "ChatGPT Thinking High",
    "chatgpt_xhigh": "ChatGPT Extra High",
    "chatgpt_extended": "ChatGPT Pro Extended",
    "claude": "Claude Opus",
    "claude_p": "Claude Opus",
    "codexcli": "Codex High",
    "codexcli_final": "Codex XHigh",
}

OPENCODE_MODELS = {
    "opencode_glm": ("opencode-go/glm-5.2", "max"),
    "opencode_minimax": ("opencode-go/minimax-m3", "thinking"),
    "opencode_deepseek": ("opencode-go/deepseek-v4-pro", "max"),
}
OPENCODE_ENGINES = frozenset(OPENCODE_MODELS)
CHATGPT_ENGINES = frozenset({"chatgpt_high", "chatgpt_xhigh", "chatgpt_extended"})
CHATGPT_MODEL_EFFORTS = {
    "chatgpt_high": ("thinking", "extended"),
    "chatgpt_xhigh": ("thinking", "heavy"),
    "chatgpt_extended": ("pro", "extended"),
}
CHATGPT_REASONING_LEVELS = {
    "chatgpt_high": "높음",
    "chatgpt_xhigh": "매우 높음",
    "chatgpt_extended": "Pro 확장",
}
CHATGPT_CDP_POOLS = {
    "chatgpt_high": ("http://127.0.0.1:9222", "http://127.0.0.1:9224"),
    "chatgpt_xhigh": ("http://127.0.0.1:9222", "http://127.0.0.1:9224"),
    "chatgpt_extended": ("http://127.0.0.1:9223", "http://127.0.0.1:9225"),
}
CHATGPT_DEFAULT_CDP_URL = "http://127.0.0.1:9222"
CHATGPT_SLOT_WAIT_SECONDS = 2.0

CLAUDE_REVIEW_EFFORT = "high"

CLAUDE_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Grep",
    "Glob",
    "Bash(gh pr view *)",
    "Bash(gh pr view:*)",
    "Bash(gh pr diff *)",
    "Bash(gh pr diff:*)",
    "Bash(gh pr checks *)",
    "Bash(gh pr checks:*)",
    "Bash(gh issue view *)",
    "Bash(gh issue view:*)",
    "Bash(gh api *)",
    "Bash(gh api:*)",
    "Bash(gh run view *)",
    "Bash(gh run view:*)",
    "Bash(gh pr comment *)",
    "Bash(gh pr comment:*)",
    "Bash(git diff *)",
    "Bash(git diff:*)",
    "Bash(git show *)",
    "Bash(git show:*)",
    "Bash(git status *)",
    "Bash(git status:*)",
    "Bash(git grep *)",
    "Bash(git grep:*)",
    "Bash(git log *)",
    "Bash(git log:*)",
    "Bash(git blame *)",
    "Bash(git blame:*)",
    "Bash(git ls-files *)",
    "Bash(git ls-files:*)",
    "Bash(rg *)",
    "Bash(rg:*)",
    "Bash(grep *)",
    "Bash(grep:*)",
    "Bash(find *)",
    "Bash(find:*)",
    "Bash(ls *)",
    "Bash(ls:*)",
    "Bash(sed *)",
    "Bash(sed:*)",
    "Bash(cat *)",
    "Bash(cat:*)",
    "Bash(wc *)",
    "Bash(wc:*)",
    "Bash(head *)",
    "Bash(head:*)",
    "Bash(tail *)",
    "Bash(tail:*)",
    "Bash(nl *)",
    "Bash(nl:*)",
    "Bash(awk *)",
    "Bash(awk:*)",
]


def parse_request(body: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*@(?P<engine>glm|미니맥스|딥시크|지피티매우높음|지피티높음|지피티확장|클로드-p|클로드|코덱스|최종리뷰)\b(?P<instruction>.*)", body, re.I | re.S)
    if not match:
        return None
    # The mention is Korean, but the "-p" suffix may arrive as "-P" under re.I.
    engine_key = match.group("engine").lower()
    engine = ENGINE_MENTIONS[engine_key]
    instruction = match.group("instruction").strip() or "코드리뷰"
    return engine, instruction


def run_review(
    config: Config,
    event: dict,
    engine: str,
    instruction: str,
    *,
    post_failure: bool = False,
) -> bool:
    repo = event["repository"]["full_name"]
    pr_number = int(event["issue"]["number"])
    comment_id = int(event["comment"]["id"])
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo}/pull/{pr_number}/comment/{comment_id}/{engine}"))
    marker = f"<!-- ai-pr-review-run:webhook:{comment_id}:{engine} -->"
    review_root = config.work_dir / repo.replace("/", "__")
    review_root.mkdir(parents=True, exist_ok=True)
    session_title = _opencode_session_title(repo, pr_number, comment_id, engine) if engine in OPENCODE_ENGINES else None

    if _marker_exists(repo, pr_number, marker):
        return True

    with _review_workspace(review_root, pr_number, comment_id, engine) as review_dir:
        checkout_dir = review_dir / "checkout"
        log_path = review_dir / "review.log"
        prompt_path = review_dir / "review_prompt.md"
        failure_path = review_dir / "failure_comment.md"

        try:
            _checkout_pr(repo, pr_number, checkout_dir, reuse_existing=engine in OPENCODE_ENGINES)
            head_sha = _run_text(["git", "rev-parse", "HEAD"], cwd=checkout_dir).strip()
            if engine == "claude_p":
                _trust_claude_workspace(checkout_dir)
            review_write_dir = _review_write_dir(engine, review_dir, checkout_dir)
            review_write_dir.mkdir(parents=True, exist_ok=True)
            prompt_path.write_text(
                _build_prompt(
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=head_sha,
                    engine=engine,
                    instruction=instruction,
                    marker=marker,
                    review_dir=review_write_dir,
                ),
                encoding="utf-8",
            )
            exit_code = _run_agent_with_watchdog(
                config=config,
                engine=engine,
                repo=repo,
                pr_number=pr_number,
                marker=marker,
                prompt_path=prompt_path,
                checkout_dir=checkout_dir,
                review_dir=review_dir,
                log_path=log_path,
                run_id=run_id,
                session_title=session_title,
            )
            if _marker_exists(repo, pr_number, marker):
                return True
            if engine in CHATGPT_ENGINES and exit_code == 0:
                return True
            with log_path.open("a", encoding="utf-8", errors="replace") as log:
                log.write(f"\nAgent exited with code {exit_code}, but the required marker was not posted.\n")
            if post_failure and _post_failure(repo, pr_number, engine, marker, review_dir, log_path, failure_path):
                return True
            return False
        except Exception as exc:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
            if post_failure and _post_failure(repo, pr_number, engine, marker, review_dir, log_path, failure_path):
                return True
            return False


@contextmanager
def _review_workspace(review_root: Path, pr_number: int, comment_id: int, engine: str):
    if engine in OPENCODE_ENGINES:
        review_dir = review_root / f"pr-{pr_number}-{comment_id}-{engine}"
        review_dir.mkdir(parents=True, exist_ok=True)
        yield review_dir
        return
    with tempfile.TemporaryDirectory(prefix=f"pr-{pr_number}-{comment_id}-", dir=review_root) as tmp:
        yield Path(tmp)


def _checkout_pr(repo: str, pr_number: int, checkout_dir: Path, *, reuse_existing: bool = False) -> None:
    if reuse_existing and (checkout_dir / ".git").exists():
        return
    if checkout_dir.exists():
        shutil.rmtree(checkout_dir)
    _run(["gh", "repo", "clone", repo, str(checkout_dir), "--", "--depth", "1"])
    pr_ref = f"refs/pull/{pr_number}/head"
    local_ref = f"refs/remotes/origin/pr-{pr_number}"
    _run(["git", "fetch", "--depth", "1", "origin", f"{pr_ref}:{local_ref}"], cwd=checkout_dir)
    _run(["git", "checkout", "--detach", local_ref], cwd=checkout_dir)


def _review_write_dir(engine: str, review_dir: Path, checkout_dir: Path) -> Path:
    if engine in OPENCODE_ENGINES:
        return checkout_dir / ".ai-review"
    return review_dir


def _build_prompt(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    engine: str,
    instruction: str,
    marker: str,
    review_dir: Path,
) -> str:
    reviewer_identity = ENGINE_IDENTITIES.get(engine, engine)
    model_name = ENGINE_MODEL_NAMES.get(engine, reviewer_identity)
    return _build_unified_review_prompt(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        engine=engine,
        instruction=instruction,
        marker=marker,
        review_dir=None if engine in CHATGPT_ENGINES else review_dir,
        model_name=model_name,
    )


def _build_unified_review_prompt(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    engine: str,
    instruction: str,
    marker: str,
    review_dir: Path | None,
    model_name: str,
) -> str:
    template = _load_prompt_template("chatgpt-github-review-ko.md")
    if engine in CHATGPT_ENGINES:
        lines = [
            f"PR 번호: #{pr_number}",
            f"Git repo: {repo}",
            "",
            "GitHub에 게시하는 모든 PR review body, 일반 PR comment, inline review comment body의 첫 줄은 반드시 `ChatGPT`만 쓴다.",
            "",
            template,
            "",
        ]
        if instruction not in {"코드리뷰", "코드 리뷰"}:
            lines.extend(["추가 요청:", instruction, ""])
        return "\n".join(lines)

    lines = [
        model_name,
        f"PR 번호: #{pr_number}",
        f"Git repo: {repo}",
        f"Head SHA: {head_sha}",
        f"선택된 실행 모드: {model_name}",
        "",
        "# 자동 실행용 최소 추가 지시",
        "실제 접근 가능한 PR diff와 변경 파일만 기준으로 코드 리뷰한다.",
        "PR diff를 확인할 수 없으면 추측 리뷰나 완료 marker 게시를 하지 말고, marker 없이 채팅 응답에 접근 실패만 남긴다.",
        "분석이 끝나기 전에는 중간/부분/임시 리뷰를 게시하지 말고, 최종 결과만 한 번 제출한다.",
        "확신할 수 있는 지적은 GitHub Files changed의 변경 라인에 inline review comment로 나눠 남긴다.",
        "확신할 수 있는 지적이 없으면 inline comment 없이 PR review body에 모델명, `확신할 수 있는 인라인 코드리뷰 코멘트 없음.`, 완료 marker만 남긴다.",
        "GitHub inline review 또는 PR review body 제출이 막히면 일반 PR comment에 모델명, 리뷰 내용, 완료 marker를 남긴다.",
        "GitHub 게시가 모두 실패하면 marker 없이 채팅 응답에 모델명과 리뷰 내용만 남긴다.",
        f"GitHub에 게시하는 모든 PR review body, 일반 PR comment, inline review comment body의 첫 줄은 반드시 `{model_name}`만 쓴다.",
        f"완료 marker는 GitHub에 실제 게시하는 마지막 리뷰/댓글에만 넣는다: {marker}",
        "파일 수정, 커밋, 푸시, 머지, 라벨 변경, 워크플로우 재실행/취소 금지.",
        "PR code 실행, build, test, install 금지.",
    ]
    if review_dir is not None:
        lines.extend(
            [
                f"임시 리뷰 payload나 fallback markdown은 `{review_dir}` 아래에만 작성한다.",
                f"gh CLI로 제출해야 하면 `{review_dir}/review-payload.json`에 PR review payload를 만들고 `gh api --method POST repos/{repo}/pulls/{pr_number}/reviews --input {review_dir}/review-payload.json`로 제출한다.",
                "payload는 `commit_id`, `event: \"COMMENT\"`, `body`, `comments`를 사용한다.",
                "comments[]는 `path`, `line`, `side`, `body`를 사용하고, 변경 후 라인은 `side: \"RIGHT\"`, 삭제 라인은 `side: \"LEFT\"`를 쓴다.",
            ]
        )
    if instruction not in {"코드리뷰", "코드 리뷰"}:
        lines.extend(["", "추가 요청:", instruction])
    lines.extend(["", template, ""])
    if engine == "codexcli_final":
        lines.extend(
            [
                "최종리뷰 모드:",
                "- merge blocker와 non-blocking note를 분리한다.",
                "- PR 본문, 연결 이슈, 최근 리뷰 코멘트에 나온 요구사항이 현재 diff에서 충족됐는지 확인한다.",
                "- 확인하지 못한 것은 확인하지 못했다고 쓴다.",
                "",
            ]
        )
    return "\n".join(lines)


def _load_prompt_template(name: str) -> str:
    path = Path(__file__).resolve().parent.parent / "prompts" / name
    return path.read_text(encoding="utf-8").strip()


def _run_agent_with_watchdog(
    *,
    config: Config,
    engine: str,
    repo: str,
    pr_number: int,
    marker: str,
    prompt_path: Path,
    checkout_dir: Path,
    review_dir: Path,
    log_path: Path,
    run_id: str,
    session_title: str | None,
) -> int:
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        chatgpt_slot = _chatgpt_browser_slot(engine, log) if engine in CHATGPT_ENGINES else None
        with chatgpt_slot or _null_context():
            cdp_url = chatgpt_slot.cdp_url if chatgpt_slot is not None else None
            if engine in CHATGPT_ENGINES:
                _ensure_chatgpt_browser_ready(config, log)
            command = _agent_command(
                config,
                engine,
                prompt_path,
                checkout_dir,
                review_dir,
                run_id,
                config.model_timeout_seconds,
                session_title=session_title,
                chatgpt_cdp_url=cdp_url,
            )
            env = os.environ.copy()
            if engine in CHATGPT_ENGINES:
                env["AGBROWSE_RAW_PROMPT"] = "1"
                env["AGBROWSE_JSON_ERRORS"] = "1"
            proc = subprocess.Popen(
                command,
                cwd=checkout_dir,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                preexec_fn=os.setsid,
                env=env,
            )
            deadline = time.monotonic() + config.model_timeout_seconds
            while proc.poll() is None and time.monotonic() < deadline:
                if _marker_exists(repo, pr_number, marker):
                    time.sleep(config.posted_grace_seconds)
                    _terminate_process_group(proc)
                    return 0
                time.sleep(config.poll_seconds)

            if proc.poll() is None:
                settle_deadline = time.monotonic() + config.marker_settle_seconds
                while time.monotonic() < settle_deadline:
                    if _marker_exists(repo, pr_number, marker):
                        time.sleep(config.posted_grace_seconds)
                        _terminate_process_group(proc)
                        return 0
                    time.sleep(config.poll_seconds)
                log.write("\nModel timed out before posting the required marker.\n")
                _terminate_process_group(proc)
                return 124

            exit_code = proc.wait()
            if engine in CHATGPT_ENGINES:
                return exit_code
    settle_deadline = time.monotonic() + config.marker_settle_seconds
    while time.monotonic() < settle_deadline:
        if _marker_exists(repo, pr_number, marker):
            return 0
        time.sleep(config.poll_seconds)
    return exit_code or 1



@contextmanager
def _null_context():
    yield


class _ChatGPTBrowserSlot:
    def __init__(self, engine: str, log: object) -> None:
        self.engine = engine
        self.log = log
        self.cdp_url = CHATGPT_DEFAULT_CDP_URL
        self._lock_file = None

    def __enter__(self):
        self.cdp_url, self._lock_file = _acquire_chatgpt_browser_lock(self.engine, self.log)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._lock_file is None:
            return
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._lock_file.close()
            self._lock_file = None


def _chatgpt_browser_slot(engine: str, log: object) -> _ChatGPTBrowserSlot:
    return _ChatGPTBrowserSlot(engine, log)


def _acquire_chatgpt_browser_lock(engine: str, log: object):
    urls = CHATGPT_CDP_POOLS.get(engine, (CHATGPT_DEFAULT_CDP_URL,))
    lock_dir = Path(tempfile.gettempdir()) / "personal-review-machines-chatgpt-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    logged_wait = False
    while True:
        for url in urls:
            lock_path = lock_dir / f"{_chatgpt_cdp_lock_name(url)}.lock"
            lock_file = lock_path.open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                continue
            log.write(f"ChatGPT browser slot acquired: {url}\n")
            log.flush()
            return url, lock_file
        if not logged_wait:
            ports = ", ".join(urls)
            log.write(f"All ChatGPT browser slots are busy for {engine}; waiting: {ports}\n")
            log.flush()
            logged_wait = True
        time.sleep(CHATGPT_SLOT_WAIT_SECONDS)


def _chatgpt_cdp_lock_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", url).strip("_") or "default"

def _agent_command(
    config: Config,
    engine: str,
    prompt_path: Path,
    checkout_dir: Path,
    review_dir: Path,
    run_id: str,
    timeout_seconds: int,
    *,
    session_title: str | None = None,
    chatgpt_cdp_url: str | None = None,
) -> list[str]:
    prompt = prompt_path.read_text(encoding="utf-8")
    if engine in OPENCODE_ENGINES:
        binary = Path.home() / ".opencode/bin/opencode"
        if not binary.exists():
            found = shutil.which("opencode")
            if not found:
                raise RuntimeError("opencode CLI was not found")
            binary = Path(found)
        if not binary.exists():
            raise RuntimeError("opencode CLI was not found")
        model, variant = OPENCODE_MODELS[engine]
        command = [str(binary), "run", "--auto", "--model", model, "--variant", variant]
        if session_title:
            command.extend(["--title", session_title])
            session_id = _find_latest_opencode_session_id(session_title)
            if session_id:
                command.extend(["--session", session_id])
                prompt = _build_opencode_resume_prompt(prompt)
        command.append(prompt)
        return command
    if engine in {"codexcli", "codexcli_final"}:
        binary = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
        if not Path(binary).exists():
            raise RuntimeError("Codex CLI was not found")
        effort = "xhigh" if engine == "codexcli_final" else "high"
        return [
            binary,
            "exec",
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--cd",
            str(checkout_dir),
            "--sandbox",
            "danger-full-access",
            "--skip-git-repo-check",
            "--ephemeral",
            "--output-last-message",
            str(review_dir / "codex_review.md"),
            prompt,
        ]
    if engine == "claude":
        binary = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
        if not Path(binary).exists():
            raise RuntimeError("Claude CLI was not found")
        return [
            binary,
            "-p",
            prompt,
            "--model",
            "opus",
            "--effort",
            CLAUDE_REVIEW_EFFORT,
            "--tools",
            "Read,Write,Grep,Glob,Bash",
            "--allowedTools",
            *CLAUDE_ALLOWED_TOOLS,
            "--disallowedTools",
            "Edit,NotebookEdit,WebFetch,WebSearch",
            "--output-format",
            "text",
            "--permission-mode",
            "acceptEdits",
            "--no-session-persistence",
        ]
    if engine == "claude_p":
        binary = shutil.which("claude-p") or str(Path.home() / ".local/bin/claude-p")
        if not Path(binary).exists():
            raise RuntimeError("claude-p CLI was not found")
        raw_log = review_dir / "claude-p.raw.log"
        return [
            binary,
            "-p",
            prompt,
            "--session-id",
            run_id,
            "--remote-control",
            f"prm-{run_id}",
            "--remote-control-session-name-prefix",
            "webhook",
            "--disable-slash-commands",
            "--model",
            "opus",
            "--effort",
            CLAUDE_REVIEW_EFFORT,
            "--tools",
            "Read",
            "Write",
            "Grep",
            "Glob",
            "Bash",
            "--allowedTools",
            *CLAUDE_ALLOWED_TOOLS,
            "--disallowedTools",
            "Edit",
            "NotebookEdit",
            "WebFetch",
            "WebSearch",
            "--output-format",
            "text",
            "--permission-mode",
            "acceptEdits",
            "--timeout-sec",
            str(timeout_seconds),
            "--quiet-after-sec",
            "120",
            "--raw-log",
            str(raw_log),
        ]
    if engine in CHATGPT_ENGINES:
        binary = shutil.which("chatgpt-github-review") or str(
            Path(__file__).resolve().parent.parent / "scripts" / "chatgpt-github-review"
        )
        if not Path(binary).exists():
            raise RuntimeError("chatgpt-github-review CLI was not found")
        return [
            str(binary),
            "--url",
            config.chatgpt_url,
            "--model",
            ENGINE_MODEL_NAMES.get(engine, engine),
            "--timeout",
            str(timeout_seconds),
            "--prompt-file",
            str(prompt_path),
            "--cdp",
            chatgpt_cdp_url or CHATGPT_DEFAULT_CDP_URL,
            "--reasoning-level",
            CHATGPT_REASONING_LEVELS.get(engine, "Pro 확장"),
            "--fallback-delay",
            "30",
        ] + (["--force-fallback-after-delay", "1"] if engine == "chatgpt_extended" else [])
    raise RuntimeError(f"Unknown review engine: {engine}")


def _ensure_chatgpt_browser_ready(config: Config, log: object) -> None:
    env = os.environ.copy()
    env["PRM_CHATGPT_URL"] = config.chatgpt_url
    subprocess.run(
        list(config.chatgpt_browser_start_command),
        check=True,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _trust_claude_workspace(checkout_dir: Path) -> None:
    config_path = Path.home() / ".claude.json"
    path_key = str(checkout_dir)
    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        raw = {}
    projects = raw.setdefault("projects", {})
    project = projects.setdefault(path_key, {})
    project.setdefault("allowedTools", [])
    project.setdefault("mcpContextUris", [])
    project.setdefault("mcpServers", {})
    project.setdefault("enabledMcpjsonServers", [])
    project.setdefault("disabledMcpjsonServers", [])
    project["hasTrustDialogAccepted"] = True
    project.setdefault("projectOnboardingSeenCount", 0)
    project.setdefault("hasClaudeMdExternalIncludesApproved", False)
    project.setdefault("hasClaudeMdExternalIncludesWarningShown", False)
    project.setdefault("hasUnseenTeamArtifacts", False)
    config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _marker_exists(repo: str, pr_number: int, marker: str) -> bool:
    env = os.environ.copy()
    env["REVIEW_MARKER"] = marker
    paths = [
        f"repos/{repo}/pulls/{pr_number}/comments?per_page=100",
        f"repos/{repo}/pulls/{pr_number}/reviews?per_page=100",
        f"repos/{repo}/issues/{pr_number}/comments?per_page=100",
    ]
    for path in paths:
        result = subprocess.run(
            [
                "gh",
                "api",
                "--paginate",
                path,
                "--jq",
                '.[] | select((.body // "") | contains(env.REVIEW_MARKER)) | (.html_url // .url // (.id | tostring))',
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return True
    return False


def _post_failure(
    repo: str,
    pr_number: int,
    engine: str,
    marker: str,
    review_dir: Path,
    log_path: Path,
    failure_path: Path,
) -> bool:
    if _marker_exists(repo, pr_number, marker):
        return True
    reviewer_identity = ENGINE_IDENTITIES.get(engine, engine)
    model_name = ENGINE_MODEL_NAMES.get(engine, reviewer_identity)
    preserved_review = _extract_preserved_review(review_dir, log_path, marker)
    tail = _tail(log_path, 80).replace(marker, "[required marker redacted]")
    failure_path.write_text(
        "\n".join(
            [
                model_name,
                "",
                f"### AI PR Review ({reviewer_identity}) - fallback",
                "",
                "인라인/PR review 게시 확인에 실패해서 일반 PR comment로 리뷰 산출물을 보존합니다.",
                "",
                preserved_review
                or f"{model_name}\n\n리뷰 산출물을 별도로 추출하지 못했습니다. 아래 로그 tail만 보존합니다.",
                "",
                marker,
                "",
                "<details><summary>실패 로그 tail</summary>",
                "",
                "```text",
                tail,
                "```",
                "",
                "</details>",
            ]
        ),
        encoding="utf-8",
    )
    try:
        result = subprocess.run(
            ["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body-file", str(failure_path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"\nFailed to post fallback PR comment: {type(exc).__name__}: {exc}\n")
            log.write(failure_path.read_text(encoding="utf-8", errors="replace"))
        return False
    if result.returncode != 0:
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write("\nFailed to post fallback PR comment.\n")
            log.write(result.stderr[-4000:])
            log.write("\n--- fallback comment body ---\n")
            log.write(failure_path.read_text(encoding="utf-8", errors="replace"))
        return False
    return _marker_exists(repo, pr_number, marker)


def _extract_preserved_review(review_dir: Path, log_path: Path, marker: str) -> str:
    payload_review = _extract_review_payload_markdown(review_dir, marker)
    if payload_review:
        return payload_review
    chatgpt_review = _extract_chatgpt_artifact_markdown(log_path, marker)
    if chatgpt_review:
        return chatgpt_review
    return ""


def _extract_review_payload_markdown(review_dir: Path, marker: str) -> str:
    payload_paths = sorted(review_dir.rglob("review-payload.json"))
    for payload_path in payload_paths:
        try:
            payload = json.loads(payload_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        lines = ["#### Preserved review payload", ""]
        body = str(payload.get("body") or "").replace(marker, "[required marker redacted]").strip()
        if body:
            lines.extend(["**Review body**", "", body, ""])
        comments = payload.get("comments")
        if isinstance(comments, list) and comments:
            lines.extend(["**Inline comments that could not be verified as posted**", ""])
            for index, comment in enumerate(comments, 1):
                if not isinstance(comment, dict):
                    continue
                path = comment.get("path", "?")
                line = comment.get("line") or comment.get("position") or "?"
                side = comment.get("side", "")
                comment_body = str(comment.get("body") or "").replace(marker, "[required marker redacted]").strip()
                lines.extend(
                    [
                        f"{index}. `{path}:{line}` {side}".rstrip(),
                        "",
                        comment_body or "(empty comment body)",
                        "",
                    ]
                )
        if len(lines) > 2:
            return "\n".join(lines).strip()
    return ""


def _extract_chatgpt_artifact_markdown(log_path: Path, marker: str) -> str:
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    best = ""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        artifact = obj.get("answerArtifact")
        candidate = ""
        if isinstance(artifact, dict):
            candidate = str(artifact.get("markdown") or artifact.get("text") or "")
        if not candidate:
            candidate = str(obj.get("answerText") or "")
        candidate = candidate.replace(marker, "[required marker redacted]").strip()
        if len(candidate) > len(best):
            best = candidate
    if not best:
        return ""
    return "\n".join(["#### Preserved ChatGPT response", "", best]).strip()


def _opencode_session_title(repo: str, pr_number: int, comment_id: int, engine: str) -> str:
    repo_name = repo.rsplit("/", 1)[-1]
    return f"{repo_name} PR #{pr_number} {engine} review ({comment_id})"


def _find_latest_opencode_session_id(session_title: str) -> str | None:
    db_path = Path.home() / ".local/share/opencode/opencode.db"
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                """
                SELECT id
                FROM session
                WHERE title = ?
                ORDER BY time_updated DESC
                LIMIT 1
                """,
                (session_title,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return str(row[0])


def _build_opencode_resume_prompt(prompt: str) -> str:
    return "\n".join(
        [
            "이전 오픈코드 리뷰 세션을 이어서 계속 진행해.",
            "이미 확인한 diff와 문맥은 반복해서 처음부터 다시 정리하지 말고, 남은 작업만 이어서 끝내.",
            "새 리뷰를 처음부터 다시 시작하지 말고 현재 세션의 연속 작업으로 처리해.",
            "",
            prompt,
        ]
    )


def _tail(path: Path, lines: int) -> str:
    if not path.exists():
        return "(no log)"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _terminate_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        time.sleep(5)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True)


def _run_text(args: list[str], cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout
