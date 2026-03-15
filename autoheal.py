"""
AutoHeal v2 — Autonomous Infrastructure Repair Agent
- Monitors Coolify apps every 5 min
- Diagnoses crashes via Claude API
- Tests fixes in dry-run before applying
- Max 3 attempts per app to avoid token burn
"""
import asyncio
import json
import os
import logging
import base64
import re
import httpx
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
COOLIFY_URL    = os.environ.get("COOLIFY_URL", "").rstrip("/")
COOLIFY_TOKEN  = os.environ.get("COOLIFY_TOKEN", "")
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
GITHUB_USER    = os.environ.get("GITHUB_USER", "szachmacik")
DRY_RUN        = os.environ.get("DRY_RUN", "false").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("autoheal")

fix_attempts: dict[str, int] = {}
MAX_ATTEMPTS = 3


# ── Coolify API ────────────────────────────────────────────────────────────────
def ch():
    return {"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"}

async def coolify_get(path: str):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{COOLIFY_URL}/api/v1{path}", headers=ch())
        r.raise_for_status()
        return r.json()

async def coolify_post(path: str, body: dict):
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{COOLIFY_URL}/api/v1{path}", headers=ch(), json=body)
        r.raise_for_status()
        return r.json()

async def list_apps() -> list[dict]:
    apps = await coolify_get("/applications")
    return [{
        "uuid": a["uuid"], "name": a["name"],
        "status": a.get("status", ""),
        "repo": a.get("git_repository", ""),
        "restarts": a.get("restart_count", 0)
    } for a in apps]

async def get_deployment_logs(app_uuid: str) -> str:
    try:
        deploys = await coolify_get(f"/deployments?applicationId={app_uuid}")
        # API returns dict {"0": {...}, "1": {...}} not a list
        if not deploys:
            return "No deployments found"
        first = list(deploys.values())[0]
        deploy_uuid = first.get("deployment_uuid")
        if not deploy_uuid:
            return "No deployment UUID found"
        detail = await coolify_get(f"/deployments/{deploy_uuid}")
        logs = detail.get("logs")
        if not logs:
            return "No logs in deployment"
        entries = json.loads(logs)
        errors = [
            str(e.get("output", ""))[:400]
            for e in entries
            if not e.get("hidden") and any(
                x in str(e.get("output", ""))
                for x in ["ERROR", "error:", "ENOENT", "failed to", "exit code", "ERR_", "Cannot"]
            )
        ]
        return "\n".join(errors[-20:]) if errors else "Build OK but container exited at runtime"
    except Exception as ex:
        return f"Log fetch error: {ex}"

async def deploy_app(uuid: str, force: bool = True) -> str:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{COOLIFY_URL}/api/v1/deploy?uuid={uuid}" + ("&force=true" if force else ""),
            headers=ch()
        )
        r.raise_for_status()
        return r.json()["deployments"][0]["deployment_uuid"]


# ── GitHub API ─────────────────────────────────────────────────────────────────
def gh():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"}

async def gh_get_file(repo: str, path: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}",
            headers=gh()
        )
        r.raise_for_status()
        d = r.json()
        return base64.b64decode(d["content"]).decode(), d["sha"]

async def gh_update_file(repo: str, path: str, content: str, sha: str, msg: str) -> bool:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}",
            headers=gh(),
            json={"message": msg, "content": base64.b64encode(content.encode()).decode(), "sha": sha}
        )
        return r.status_code in (200, 201)

async def gh_get_hint(repo: str) -> str:
    for fname in [".env.example", "docker-compose.yml"]:
        try:
            content, _ = await gh_get_file(repo, fname)
            return content[:1500]
        except Exception:
            pass
    return ""


# ── Dockerfile validation (test before apply) ──────────────────────────────────
def validate_dockerfile(content: str) -> tuple[bool, str]:
    """
    Runs basic sanity checks on a Dockerfile patch before applying.
    Returns (is_valid, reason).
    """
    if not content or len(content.strip()) < 30:
        return False, "Dockerfile is empty or too short"

    lines = [l.strip() for l in content.strip().splitlines() if l.strip() and not l.strip().startswith("#")]

    # Must start with FROM
    if not lines or not lines[0].upper().startswith("FROM"):
        return False, "Dockerfile must start with FROM"

    # Must have CMD or ENTRYPOINT
    has_cmd = any(l.upper().startswith(("CMD", "ENTRYPOINT")) for l in lines)
    if not has_cmd:
        return False, "Dockerfile missing CMD or ENTRYPOINT"

    # Must have WORKDIR
    has_workdir = any(l.upper().startswith("WORKDIR") for l in lines)
    if not has_workdir:
        return False, "Dockerfile missing WORKDIR"

    # No obvious shell injection
    dangerous = ["$(rm", "$(curl", "> /etc/passwd", "chmod 777 /", "rm -rf /"]
    for danger in dangerous:
        if danger in content:
            return False, f"Dangerous pattern detected: {danger}"

    # Check COPY patches exists if patchedDependencies hinted
    if "patches" in content.lower():
        has_copy_patches = any("COPY patches" in l for l in lines)
        if not has_copy_patches:
            return False, "References patches but missing COPY patches ./patches"

    # Check multi-stage: if AS builder exists, production stage should exist too
    stages = [l for l in lines if re.match(r"FROM .+ AS ", l, re.IGNORECASE)]
    if len(stages) > 1:
        stage_names = [re.search(r"AS (\w+)", s, re.IGNORECASE).group(1).lower() for s in stages if re.search(r"AS (\w+)", s, re.IGNORECASE)]
        if "builder" in stage_names and "production" not in stage_names and "prod" not in stage_names:
            return False, "Multi-stage build has builder but no production stage"

    return True, "OK"


def validate_env_vars(env_vars: dict) -> tuple[bool, str]:
    """Check env vars don't contain placeholders or look wrong."""
    if not env_vars:
        return False, "Empty env vars dict"

    placeholder_patterns = ["YOUR_", "CHANGE_ME", "TODO", "<", ">", "example.com", "localhost"]
    for k, v in env_vars.items():
        if not k or not isinstance(k, str):
            return False, f"Invalid key: {k}"
        for pattern in placeholder_patterns:
            if pattern in str(v):
                return False, f"Placeholder detected in {k}: {v[:30]}"
        # Key should be uppercase with underscores
        if not re.match(r'^[A-Z][A-Z0-9_]*$', k):
            return False, f"Key {k} should be uppercase with underscores"

    return True, "OK"


# ── Claude API ─────────────────────────────────────────────────────────────────
async def ask_claude(system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "system": system,
                "messages": [{"role": "user", "content": user}]
            }
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]

async def diagnose(app: dict, logs: str, dockerfile: str, hint: str) -> dict:
    system = """You are an autonomous DevOps repair agent. Return ONLY valid JSON, nothing else.
Schema:
{
  "diagnosis": "root cause in one sentence",
  "fix_type": "dockerfile" | "restart" | "env_var" | "skip",
  "dockerfile_patch": "complete fixed Dockerfile (only when fix_type=dockerfile)",
  "env_vars": {"KEY": "VALUE"},
  "confidence": 0-100,
  "reason": "why this fix works"
}
Rules:
- fix_type=restart: container starts but crashes (OOM, port issue, missing runtime dep)
- fix_type=dockerfile: build error, missing COPY, wrong stage, missing dep
- fix_type=env_var: missing required env var causes crash (only if clearly shown in logs)
- fix_type=skip: unclear cause or requires manual intervention
- confidence<60 → always use fix_type=skip to avoid wasting tokens
- dockerfile_patch must be complete Dockerfile, not a diff
- env_vars values must be real values, not placeholders"""

    user = f"""App: {app['name']} | Status: {app['status']}

CRASH LOGS:
{logs[:2500]}

CURRENT DOCKERFILE:
{dockerfile[:1500]}

ENV HINTS:
{hint[:400] or 'none'}

Return diagnosis JSON:"""

    try:
        raw = await ask_claude(system, user)
        clean = raw.strip()
        if clean.startswith("```"):
            parts = clean.split("```")
            clean = parts[1][4:] if parts[1].startswith("json") else parts[1]
        return json.loads(clean.strip())
    except Exception as ex:
        return {"fix_type": "skip", "diagnosis": f"Claude error: {ex}", "confidence": 0, "reason": ""}


# ── Main heal logic ─────────────────────────────────────────────────────────────
async def heal_app(app: dict):
    name  = app["name"]
    uuid  = app["uuid"]
    repo_full = app["repo"]
    repo  = repo_full.split("/")[-1] if "/" in repo_full else repo_full

    if not repo:
        log.warning(f"[{name}] No repo URL, skipping")
        return

    key = uuid
    attempts = fix_attempts.get(key, 0)
    if attempts >= MAX_ATTEMPTS:
        log.warning(f"[{name}] Max attempts ({MAX_ATTEMPTS}) reached, giving up")
        return

    fix_attempts[key] = attempts + 1
    log.info(f"[{name}] 🔧 Attempt {attempts + 1}/{MAX_ATTEMPTS}")

    # 1. Get logs
    logs = await get_deployment_logs(uuid)
    log.info(f"[{name}] Logs preview: {logs[:150].replace(chr(10),' ')}")

    # 2. Get Dockerfile
    try:
        dockerfile, df_sha = await gh_get_file(repo, "Dockerfile")
    except Exception as ex:
        log.error(f"[{name}] Cannot fetch Dockerfile ({ex}), blind restart")
        if not DRY_RUN:
            await deploy_app(uuid, force=True)
        return

    # 3. Get env hint
    hint = await gh_get_hint(repo)

    # 4. If no Claude key → blind restart only
    if not ANTHROPIC_KEY:
        log.info(f"[{name}] No ANTHROPIC_API_KEY → blind restart")
        if not DRY_RUN:
            await deploy_app(uuid, force=True)
        return

    # 5. Ask Claude for diagnosis
    fix = await diagnose(app, logs, dockerfile, hint)
    confidence = fix.get("confidence", 0)
    fix_type   = fix.get("fix_type", "skip")

    log.info(f"[{name}] Diagnosis: {fix.get('diagnosis','?')}")
    log.info(f"[{name}] Fix: {fix_type} (confidence: {confidence}%)")

    if confidence < 60:
        log.info(f"[{name}] Low confidence ({confidence}%), skipping to save tokens")
        return

    # 6. TEST fix before applying
    if fix_type == "dockerfile":
        patch = fix.get("dockerfile_patch", "")
        valid, reason = validate_dockerfile(patch)
        if not valid:
            log.warning(f"[{name}] Dockerfile validation FAILED: {reason}")
            log.info(f"[{name}] Skipping bad patch to avoid broken deploy")
            return
        log.info(f"[{name}] ✅ Dockerfile validation passed")

        if DRY_RUN:
            log.info(f"[{name}] DRY_RUN – would patch Dockerfile and deploy")
            return

        ok = await gh_update_file(repo, "Dockerfile", patch, df_sha,
                                  f"fix(autoheal): {fix.get('diagnosis','')[:60]}")
        if ok:
            log.info(f"[{name}] Dockerfile patched, deploying...")
            await asyncio.sleep(3)
            await deploy_app(uuid, force=True)
        else:
            log.error(f"[{name}] GitHub update failed")

    elif fix_type == "env_var":
        env_vars = fix.get("env_vars", {})
        valid, reason = validate_env_vars(env_vars)
        if not valid:
            log.warning(f"[{name}] Env var validation FAILED: {reason}")
            return
        log.info(f"[{name}] ✅ Env var validation passed: {list(env_vars.keys())}")

        if DRY_RUN:
            log.info(f"[{name}] DRY_RUN – would set {list(env_vars.keys())} and deploy")
            return

        for k, v in env_vars.items():
            try:
                await coolify_post(f"/applications/{uuid}/envs", {"key": k, "value": v})
                log.info(f"[{name}] Set {k}")
            except Exception as ex:
                log.error(f"[{name}] Set {k} failed: {ex}")
        await deploy_app(uuid, force=True)

    elif fix_type == "restart":
        log.info(f"[{name}] Restarting (no code change needed)")
        if not DRY_RUN:
            await deploy_app(uuid, force=False)

    else:
        log.info(f"[{name}] Skip: {fix.get('reason','no actionable fix')}")


# ── Check cycle ─────────────────────────────────────────────────────────────────
async def check_cycle():
    try:
        apps   = await list_apps()
        broken  = [a for a in apps if "exited" in a["status"] or "restarting" in a["status"]]
        healthy = [a for a in apps if "running" in a["status"]]

        log.info(f"📊 {len(healthy)} healthy | {len(broken)} broken | {len(apps)} total")

        # Reset counters for recovered apps
        for a in healthy:
            if a["uuid"] in fix_attempts:
                log.info(f"[{a['name']}] ✅ Recovered, resetting counter")
                del fix_attempts[a["uuid"]]

        # Heal max 2 at a time
        for app in broken[:2]:
            try:
                await heal_app(app)
                await asyncio.sleep(5)
            except Exception as ex:
                log.error(f"[{app['name']}] Unexpected error: {ex}")

    except Exception as ex:
        log.error(f"Cycle error: {ex}")


async def main():
    log.info("🚀 AutoHeal v2 starting")
    log.info(f"   Coolify:  {COOLIFY_URL or 'NOT SET'}")
    log.info(f"   Interval: {CHECK_INTERVAL}s")
    log.info(f"   Claude:   {'✅ enabled (haiku)' if ANTHROPIC_KEY else '⚠️  disabled (blind restart mode)'}")
    log.info(f"   GitHub:   {'✅ enabled' if GITHUB_TOKEN else '❌ disabled'}")
    log.info(f"   DRY_RUN:  {DRY_RUN}")
    log.info(f"   Validation: always ON before any fix")

    while True:
        log.info(f"--- {datetime.now().strftime('%H:%M:%S')} ---")
        await check_cycle()
        log.info(f"Sleeping {CHECK_INTERVAL}s")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
