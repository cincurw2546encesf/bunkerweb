import os
from datetime import datetime

from worker.app import app, get_worker_db
from worker.executor import JobExecutor

SENSITIVE_ENV_KEYS = {"CELERY_BROKER_URL", "JOBS_HMAC_SECRET"}


def _get_apis():
    from API import API  # type: ignore
    from ApiCaller import ApiCaller  # type: ignore

    instances = [hostname.strip() for hostname in os.getenv("BUNKERWEB_INSTANCES", "").split() if hostname.strip()]
    if not instances:
        return None

    return ApiCaller([API(f"http://{hostname}:5000", host=hostname) for hostname in instances])


def job_shadow_name(task, args, kwargs, options) -> str:
    if args and isinstance(args[0], dict):
        job_data = args[0]
        return f"job.{job_data.get('plugin_id', '?')}.{job_data.get('name', '?')}"
    return "job.unknown"


def _request_reload_debounced(apis, broker_url: str, logger) -> None:
    import redis

    client = redis.Redis.from_url(broker_url)
    if not client.set("bw:reload_pending", "1", nx=True, ex=10):
        logger.info("Reload already pending, skipping duplicate request")
        return

    cache_sent = apis.send_files("/var/cache/bunkerweb", "/cache")
    if not cache_sent:
        raise RuntimeError("Failed to send /var/cache/bunkerweb to BunkerWeb instances")

    test = "no" if os.getenv("DISABLE_CONFIGURATION_TESTING", "no").lower() == "yes" else "yes"
    reload_sent = apis.send_to_apis("POST", f"/reload?test={test}")[0]
    if not reload_sent:
        raise RuntimeError("Failed to request BunkerWeb reload")


@app.task(
    bind=True,
    name="worker.execute_job",
    shadow_name=job_shadow_name,
    acks_late=False,
    track_started=True,
)
def execute_job(self, job_data: dict) -> dict:
    from logger import setup_logger  # type: ignore

    logger = setup_logger("WORKER")
    executor = JobExecutor(logger)
    db = get_worker_db()
    apis = _get_apis()

    name = job_data.get("name", "unknown")
    plugin = job_data.get("plugin_id", "unknown")
    run_id = job_data.get("run_id", "")
    broker_url = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
    start = datetime.now().astimezone()

    logger.info(f"[{run_id}] Starting job {plugin}/{name}")

    saved_env = os.environ.copy()
    safe_env = saved_env.copy()
    for key in SENSITIVE_ENV_KEYS:
        safe_env.pop(key, None)

    ret = 2
    success = False

    try:
        os.environ.clear()
        os.environ.update(safe_env)

        job_env = job_data.get("env")
        if isinstance(job_env, dict):
            os.environ.update(job_env)

        ret = executor.run(job_data)
        success = ret in (0, 1)
    except SystemExit as exc:
        ret = exc.code if isinstance(exc.code, int) else 1
        success = ret in (0, 1)
        if not success:
            logger.error(f"[{run_id}] Job {plugin}/{name} exited with code {ret}")
    except Exception as exc:
        logger.error(f"[{run_id}] Job {plugin}/{name} crashed: {exc}")
    finally:
        os.environ.clear()
        os.environ.update(saved_env)

    end = datetime.now().astimezone()
    duration = (end - start).total_seconds()

    if db:
        try:
            err = db.add_job_run(name, success, start, end)
            if err:
                logger.error(f"[{run_id}] Failed to record job run: {err}")
        except Exception as exc:
            logger.error(f"[{run_id}] Failed to record job run: {exc}")
    else:
        logger.warning(f"[{run_id}] Worker database is not initialized, skipping job run persistence")

    if ret == 1 and apis:
        try:
            logger.info(f"[{run_id}] Job {plugin}/{name} requested reload")
            _request_reload_debounced(apis, broker_url, logger)
        except Exception as exc:
            logger.error(f"[{run_id}] Cache/reload failed: {exc}")

    logger.info(f"[{run_id}] Job {plugin}/{name} completed with code {ret} in {duration:.1f}s")

    return {
        "duration_seconds": duration,
        "name": name,
        "needs_reload": ret == 1,
        "plugin": plugin,
        "return_code": ret,
        "run_id": run_id,
        "success": success,
    }
