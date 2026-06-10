from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .plugins import get_spec, normalize_result


_RETRY_SYSTEM_PARAM_KEYS = {"_inputs", "dem_job_id", "job_key"}


@dataclass(frozen=True)
class DispatchError(Exception):
    status_code: int
    detail: Any


def _raw_input_id(inputs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = inputs.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            for id_key in ("job_id", "id"):
                raw = value.get(id_key)
                if isinstance(raw, str) and raw:
                    return raw
    return None


def _latest_dem_job(ctx: Any, project_id: str) -> Any | None:
    with ctx.lock:
        ctx.store.reload()
        candidates = [
            job
            for job in ctx.store.jobs.values()
            if job.project_id == project_id
            and job.kind == "build-dem"
            and job.status == "succeeded"
            and isinstance(job.result, dict)
            and isinstance(job.result.get("dem_path"), str)
        ]
    candidates.sort(key=lambda job: job.created_at, reverse=True)
    return candidates[0] if candidates else None


def resolve_dem_job(ctx: Any, inputs: dict[str, Any], project_id: str) -> Any:
    dem_job_id = _raw_input_id(inputs, "dem", "dem_job", "dem_job_id")
    if dem_job_id:
        dem_job = ctx.job_or_404(dem_job_id)
    else:
        dem_job = _latest_dem_job(ctx, project_id)
        if dem_job is None:
            raise DispatchError(400, "DEM input is required and no completed DEM job exists")
    if dem_job.status != "succeeded" or not isinstance(dem_job.result, dict):
        raise DispatchError(400, "DEM job has not succeeded")
    if not isinstance(dem_job.result.get("dem_path"), str):
        raise DispatchError(400, "DEM job result is missing dem_path")
    return dem_job


def _parse_payload(body: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    body = body or {}
    if "inputs" in body or "params" in body:
        raw_inputs = body.get("inputs") or {}
        raw_params = body.get("params") or {}
        if not isinstance(raw_inputs, dict):
            raise DispatchError(400, "inputs must be an object")
        if not isinstance(raw_params, dict):
            raise DispatchError(400, "params must be an object")
        return raw_inputs, raw_params
    return {}, body


def submit_tool_job(
    ctx: Any,
    tool_key: str,
    body: dict[str, Any] | None,
    *,
    user: Any,
    project_id: str,
) -> Any:
    spec = get_spec(tool_key)
    if spec is None:
        raise DispatchError(404, f"unknown job key: {tool_key}")
    if not spec.available:
        raise DispatchError(400, spec.unavailable_reason or f"{spec.label} is unavailable")

    inputs, params_body = _parse_payload(body)

    try:
        req = spec.request_model.model_validate(params_body)
    except ValidationError as exc:
        raise DispatchError(400, exc.errors()) from exc

    if spec.runner_kind == "context":
        params = {**req.model_dump(), "_inputs": dict(inputs)}

        def run_context(progress_cb, job_id: str) -> dict[str, Any]:
            result = spec.runner(
                ctx=ctx,
                req=req,
                inputs=inputs,
                params=params,
                progress_cb=progress_cb,
                output_url=ctx.output_url,
                job_id=job_id,
            )
            return normalize_result(spec, result)

        return ctx.submit(
            spec.key, params, run_context, user=user, project_id=project_id, include_job_id=True
        )

    dem_job = resolve_dem_job(ctx, inputs, project_id)
    dem_path = dem_job.result["dem_path"]
    resolved_inputs = {**inputs, "dem": dem_job.id}
    params = {
        "dem_job_id": dem_job.id,
        "job_key": spec.key,
        "_inputs": resolved_inputs,
        **req.model_dump(),
    }

    def run(progress_cb) -> dict[str, Any]:
        out_dir = ctx.output_root / "jobs" / f"{spec.key}-{uuid.uuid4().hex[:8]}"
        out_dir.mkdir(parents=True, exist_ok=True)
        result = spec.runner(
            dem_path=dem_path,
            out_dir=Path(out_dir),
            req=req,
            progress_cb=progress_cb,
            output_url=ctx.output_url,
        )
        result.setdefault("dem_job_id", dem_job.id)
        result.setdefault("job_key", spec.key)
        return normalize_result(spec, result)

    return ctx.submit(spec.key, params, run, user=user, project_id=project_id)


def retry_tool_job(ctx: Any, job_id: str, *, user: Any, project_id: str) -> Any:
    original = ctx.job_or_404(job_id)
    if original.project_id != project_id:
        raise DispatchError(404, f"job not found: {job_id}")
    if original.status != "failed":
        raise DispatchError(400, "only failed jobs can be retried")
    if get_spec(original.kind) is None:
        raise DispatchError(400, f"job kind cannot be retried: {original.kind}")

    raw_params = dict(original.params or {})
    raw_inputs = raw_params.get("_inputs")
    inputs = dict(raw_inputs) if isinstance(raw_inputs, dict) else {}
    params = {k: v for k, v in raw_params.items() if k not in _RETRY_SYSTEM_PARAM_KEYS}

    if original.kind == "kml-import" and not isinstance(params.get("source"), str):
        raise DispatchError(400, "uploaded KML file jobs cannot be retried; upload the file again")

    return submit_tool_job(
        ctx,
        original.kind,
        {"inputs": inputs, "params": params},
        user=user,
        project_id=project_id,
    )
