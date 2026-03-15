"""
AutoHeal v2 — Autonomous Infrastructure Repair Agent
Monitors Coolify apps, diagnoses crashes via Claude, auto-fixes and deploys.
"""
import asyncio
import json
import os
import time
import logging
import base64
import httpx
from datetime import datetime

# ── Config ────────────────────────────────────────────────────────────────────
COOLIFY_URL   = os.environ.get("COOLIFY_URL", "").rstrip("/")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "")
GITHUB_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))  # 5 min default
GITHUB_USER   = os.environ.get("GITHUB_USER", "szachmacik")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("autoheal")

# Track what we've already tried to fix (avoid infinite loops)
fix_attempts: dict[str, int] = {}
MAX_ATTEMPTS = 3


# ── Coolify API ───────────────────────────────────────────────────────────────
def coolify_headers():
    return {"Authorization": f"Bearer {COOLIFY_TOKEN}", "Content-Type": "application/json"}

async def coolify_get(path: str) -> dict | list:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{COOLIFY_URL}/api/v1{path}", headers=coolify_headers())
        r.raise_for_status()
        return r.json()

async def coolify_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{COOLIFY_URL}/api/v1{path}", headers=coolify_headers(), json=body)
        r.raise_for_status()
        return r.json()

async def list_apps() -> list[dict]:
    apps = await coolify_get("/applications")
    return [{"uuid": a["uuid"], "name": a["name"], "status": a.get("status",""),
             "repo": a.get("git_repository",""), "fqdn": a.get("fqdn",""),
             "restarts": a.get("restart_count", 0)} for a in apps]

async def get_deployment_logs(app_uuid: str) -> str:
    """Get logs from the latest deployment."""
    try:
        deploys = await coolify_get(f"/applications/{app_uuid}/deployments?per_page=1")
        data = deploys.get("data", [])
        if not data:
            return "No deployments found"
        deploy_uuid = data[0]["deployment_uuid"]
        detail = await coolify_get(f"/deployments/{deploy_uuid}")
        logs = detail.get("logs")
        if not logs:
            return "No logs available"
        entries = json.loads(logs)
        # Extract error lines
        errors = []
        for e in entries:
            out = str(e.get("output", ""))
            if any(x in out for x in ["ERROR", "error:", "ENOENT", "failed to", "exit code", "ERR_"]):
                if not e.get("hidden"):
                    errors.append(out[:400])
        return "\n".join(errors[-20:]) if errors else "Build succeeded but container exited"
    except Exception as ex:
        return f"Could not fetch logs: {ex}"

async def deploy_app(app_uuid: str, force: bool = True) -> str:
    params = f"uuid={app_uuid}" + ("&force=true" if force else "")
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{COOLIFY_URL}/api/v1/deploy?{params}", headers=coolify_headers())
        r.raise_for_status()
        data = r.json()
        return data["deployments"][0]["deployment_uuid"]


# ── GitHub API ────────────────────────────────────────────────────────────────
def github_headers():
    return {"Authorization": f"token {GITHUB_TOKEN}", "Content-Type": "application/json"}

async def github_get_file(repo: str, path: str) -> tuple[str, str]:
    """Returns (content, sha)"""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}",
            headers=github_headers()
        )
        r.raise_for_status()
        d = r.json()
        return base64.b64decode(d["content"]).decode(), d["sha"]

async def github_update_file(repo: str, path: str, content: str, sha: str, message: str) -> bool:
    encoded = base64.b64encode(content.encode()).decode()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(
            f"https://api.github.com/repos/{GITHUB_USER}/{repo}/contents/{path}",
            headers=github_headers(),
            json={"message": message, "content": encoded, "sha": sha}
        )
        return r.status_code in (200, 201)

async def github_get_env_file(repo: str) -> tuple[str | None, str | None]:
    """Try to get .env.example or docker-compose.yml for env var hints."""
    for fname in [".env.example", "docker-compose.yml", "docker-compose.yaml"]:
        try:
            content, _ = await github_get_file(repo, fname)
            return content[:2000], fname
        except Exception:
            pass
    return None, None


# ── Claude API ────────────────────────────────────────────────────────────────
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
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "system": system,
                "messages": [{"role": "user", "content": user}]
            }
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"]

async def diagnose_and_fix(app: dict, logs: str, dockerfile: str, env_hint: str) -> dict:
    """Ask Claude to diagnose crash and return fix instructions as JSON."""
    system = """You are an autonomous DevOps repair agent.
Analyze crash logs and return ONLY valid JSON with fix instructions.
JSON schema:
{
  "diagnosis": "short description of root cause",
  "fix_type": "dockerfile" | "restart" | "env_var" | "skip",
  "dockerfile_patch": "full fixed Dockerfile content (only if fix_type=dockerfile)",
  "env_vars": {"KEY": "VALUE"} (only if fix_type=env_var),
  "reason": "why this fix will work"
}
Rules:
- If logs show missing file/module → fix Dockerfile COPY or RUN commands
- If logs show missing env var → fix_type=env_var with placeholder values
- If logs show port/health check issue → fix Dockerfile healthcheck
- If build succeeded but container exits immediately → check CMD/entrypoint
- If you cannot determine fix → fix_type=skip with reason
- NEVER include secrets or real credentials in env_vars
- Always return valid JSON, nothing else"""

    user = f"""App: {app['name']}
Repo: {app['repo']}
Status: {app['status']}

=== CRASH LOGS ===
{logs[:3000]}

=== CURRENT DOCKERFILE ===
{dockerfile[:2000]}

=== ENV HINTS ===
{env_hint[:500] if env_hint else 'none'}

Diagnose and provide fix JSON:"""

    try:
        response = await ask_claude(system, user)
        # Strip markdown if present
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("```")[1]
            if clean.startswith("json"):
                clean = clean[4:]
        return json.loads(clean.strip())
    except Exception as ex:
        log.error(f"Claude diagnosis failed: {ex}")
        return {"fix_type": "skip", "diagnosis": f"Claude error: {ex}", "reason": ""}


# ── Main heal loop ────────────────────────────────────────────────────────────
async def heal_app(app: dict):
    name = app["name"]
    uuid = app["uuid"]
    repo_full = app["repo"]  # e.g. "szachmacik/manus-brain-dashboard"

    # Extract repo name
    repo = repo_full.split("/")[-1] if "/" in repo_full else repo_full
    if not repo:
        log.warning(f"[{name}] No repo, skipping")
        return

    key = f"{uuid}"
    attempts = fix_attempts.get(key, 0)
    if attempts >= MAX_ATTEMPTS:
        log.warning(f"[{name}] Max fix attempts ({MAX_ATTEMPTS}) reached, skipping")
        return

    log.info(f"[{name}] 🔧 Healing attempt {attempts + 1}/{MAX_ATTEMPTS}")
    fix_attempts[key] = attempts + 1

    # 1. Get deployment logs
    logs = await get_deployment_logs(uuid)
    log.info(f"[{name}] Logs: {logs[:200]}")

    # 2. Get Dockerfile
    try:
        dockerfile, dockerfile_sha = await github_get_file(repo, "Dockerfile")
    except Exception as ex:
        log.error(f"[{name}] Cannot fetch Dockerfile: {ex}")
        # Try restart anyway
        await deploy_app(uuid, force=True)
        return

    # 3. Get env hints
    env_hint, _ = await github_get_env_file(repo)

    # 4. Ask Claude for fix
    if not ANTHROPIC_KEY:
        log.warning(f"[{name}] No ANTHROPIC_API_KEY, attempting blind restart")
        await deploy_app(uuid, force=True)
        return

    fix = await diagnose_and_fix(app, logs, dockerfile, env_hint or "")
    log.info(f"[{name}] Diagnosis: {fix.get('diagnosis','?')}")
    log.info(f"[{name}] Fix type: {fix.get('fix_type','?')} — {fix.get('reason','')}")

    fix_type = fix.get("fix_type", "skip")

    if fix_type == "dockerfile":
        new_dockerfile = fix.get("dockerfile_patch", "")
        if new_dockerfile and len(new_dockerfile) > 50:
            ok = await github_update_file(
                repo, "Dockerfile", new_dockerfile, dockerfile_sha,
                f"fix(autoheal): {fix.get('diagnosis','auto fix')[:60]}"
            )
            if ok:
                log.info(f"[{name}] ✅ Dockerfile patched, deploying...")
                await asyncio.sleep(3)
                await deploy_app(uuid, force=True)
            else:
                log.error(f"[{name}] GitHub update failed")
        else:
            log.warning(f"[{name}] Empty dockerfile patch, restarting instead")
            await deploy_app(uuid, force=True)

    elif fix_type == "env_var":
        env_vars = fix.get("env_vars", {})
        for k, v in env_vars.items():
            try:
                await coolify_post(f"/applications/{uuid}/envs", {"key": k, "value": v})
                log.info(f"[{name}] Set env {k}")
            except Exception as ex:
                log.error(f"[{name}] Env set failed: {ex}")
        await deploy_app(uuid, force=True)

    elif fix_type == "restart":
        log.info(f"[{name}] Restarting...")
        await deploy_app(uuid, force=False)

    elif fix_type == "skip":
        log.info(f"[{name}] Skipping: {fix.get('reason','no fix available')}")
    else:
        log.warning(f"[{name}] Unknown fix type: {fix_type}")


async def check_and_heal():
    """Single check cycle."""
    try:
        apps = await list_apps()
        broken = [a for a in apps if "exited" in a["status"] or "restarting" in a["status"]]
        healthy = [a for a in apps if "running" in a["status"]]

        log.info(f"📊 Status: {len(healthy)} healthy, {len(broken)} broken out of {len(apps)} apps")

        # Reset fix attempts for apps that are now healthy
        for app in healthy:
            if app["uuid"] in fix_attempts:
                log.info(f"[{app['name']}] ✅ Recovered, resetting attempt counter")
                del fix_attempts[app["uuid"]]

        # Heal broken apps (max 2 at a time to avoid overload)
        for app in broken[:2]:
            try:
                await heal_app(app)
                await asyncio.sleep(5)
            except Exception as ex:
                log.error(f"[{app['name']}] Heal error: {ex}")

    except Exception as ex:
        log.error(f"Check cycle error: {ex}")


async def main():
    log.info("🚀 AutoHeal v2 starting...")
    log.info(f"   Coolify: {COOLIFY_URL}")
    log.info(f"   Interval: {CHECK_INTERVAL}s")
    log.info(f"   Claude: {'enabled' if ANTHROPIC_KEY else 'DISABLED – set ANTHROPIC_API_KEY'}")
    log.info(f"   GitHub: {'enabled' if GITHUB_TOKEN else 'DISABLED'}")

    while True:
        log.info(f"--- Check cycle @ {datetime.now().strftime('%H:%M:%S')} ---")
        await check_and_heal()
        log.info(f"Next check in {CHECK_INTERVAL}s")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
