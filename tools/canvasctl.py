#!/usr/bin/env python3
"""Control the Infinite Canvas service from local automation."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterable, List


DEFAULT_BASE_URL = os.environ.get("CANVAS_BASE_URL", "http://127.0.0.1:3001").rstrip("/")
DONE_STATUSES = {"succeeded", "success", "done", "completed", "complete", "finished"}
FAIL_STATUSES = {"failed", "fail", "error", "canceled", "cancelled"}


class CanvasCtlError(RuntimeError):
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def clean_base_url(value: str) -> str:
    return (value or DEFAULT_BASE_URL).rstrip("/")


def api_url(base_url: str, path: str) -> str:
    return f"{clean_base_url(base_url)}{path if path.startswith('/') else '/' + path}"


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: Any = None,
    *,
    headers: Dict[str, str] | None = None,
    timeout: float = 60,
) -> Any:
    data = None
    req_headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(api_url(base_url, path), data=data, headers=req_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise CanvasCtlError(f"{method.upper()} {path} -> HTTP {exc.code}: {detail[:800]}") from exc
    except urllib.error.URLError as exc:
        raise CanvasCtlError(f"{method.upper()} {path} -> {exc.reason}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CanvasCtlError(f"{method.upper()} {path} returned non-JSON data") from exc


def print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def media_url(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith(("http://", "https://", "/output/", "/assets/", "data:image/"))


def same_origin_headers(base_url: str) -> Dict[str, str]:
    base = clean_base_url(base_url)
    return {"Origin": base, "Referer": f"{base}/static/canvas.html"}


def import_local_image(base_url: str, value: str) -> Dict[str, Any]:
    path = value.strip().strip('"').strip("'")
    if path.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(path)
        path = urllib.request.url2pathname(parsed.path or "")
        if os.name == "nt" and path.startswith("/") and len(path) > 3 and path[2] == ":":
            path = path[1:]
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    data = request_json(
        base_url,
        "POST",
        "/api/ai/import-local-image",
        {"path": path},
        headers=same_origin_headers(base_url),
        timeout=120,
    )
    files = data.get("files") or []
    if not files:
        raise CanvasCtlError(f"本地图片导入失败：{value}")
    return files[0]


def normalize_images(base_url: str, values: Iterable[str]) -> List[Dict[str, Any]]:
    images = []
    for value in values or []:
        text = str(value or "").strip()
        if not text:
            continue
        if media_url(text):
            images.append({"url": text, "name": os.path.basename(text.split("?")[0]) or "reference.png"})
        else:
            images.append(import_local_image(base_url, text))
    return images


def provider_summary(provider: Dict[str, Any]) -> Dict[str, Any]:
    models = provider.get("image_models") or []
    return {
        "id": provider.get("id") or "",
        "name": provider.get("name") or provider.get("id") or "",
        "enabled": bool(provider.get("enabled", True)),
        "has_key": bool(provider.get("has_key") or provider.get("has_wallet_key")),
        "image_model_count": len(models),
        "image_models": models[:8],
        "primary": bool(provider.get("primary")),
    }


def cmd_doctor(args: argparse.Namespace) -> int:
    app = request_json(args.base, "GET", "/api/app-info", timeout=20)
    providers = request_json(args.base, "GET", "/api/providers", timeout=20).get("providers") or []
    image_providers = [p for p in providers if p.get("enabled", True) and p.get("image_models")]
    keyed = [p for p in image_providers if p.get("has_key") or p.get("has_wallet_key")]
    result = {
        "base_url": clean_base_url(args.base),
        "app": app,
        "provider_count": len(providers),
        "image_provider_count": len(image_providers),
        "keyed_image_provider_count": len(keyed),
        "image_providers": [provider_summary(p) for p in image_providers],
    }
    if args.json:
        print_json(result)
    else:
        print(f"服务：{clean_base_url(args.base)} OK")
        print(f"平台：{len(providers)} 个；可生图：{len(image_providers)} 个；已配置 key：{len(keyed)} 个")
        for item in result["image_providers"]:
            key_state = "key" if item["has_key"] else "no-key"
            print(f"- {item['id']} / {item['name']} [{key_state}] models={item['image_model_count']}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    providers = request_json(args.base, "GET", "/api/providers", timeout=20).get("providers") or []
    items = [provider_summary(p) for p in providers if p.get("image_models") or not args.image_only]
    if args.json:
        print_json(items)
    else:
        for item in items:
            state = "enabled" if item["enabled"] else "disabled"
            key_state = "key" if item["has_key"] else "no-key"
            models = ", ".join(item["image_models"][:4])
            print(f"{item['id']}\t{item['name']}\t{state}\t{key_state}\t{models}")
    return 0


def get_or_create_canvas(args: argparse.Namespace) -> str:
    if args.new_title or not args.canvas:
        title = args.new_title or args.title or "Canvas Agent Board"
        data = request_json(args.base, "POST", "/api/canvases", {"title": title, "icon": "sparkles", "kind": "classic"})
        canvas = data.get("canvas") or {}
        canvas_id = canvas.get("id")
        if not canvas_id:
            raise CanvasCtlError("创建画布失败：返回中没有 canvas.id")
        return canvas_id
    return args.canvas


def parse_targets(raw_targets: Iterable[str], ratio: str = "") -> List[Dict[str, Any]]:
    targets = []
    for index, raw in enumerate(raw_targets or []):
        text = str(raw or "").strip()
        if not text:
            continue
        if "::" in text:
            title, prompt = text.split("::", 1)
        else:
            title, prompt = f"任务 {index + 1}", text
        target = {"title": title.strip() or f"任务 {index + 1}", "prompt": prompt.strip()}
        if ratio:
            target["ratio"] = ratio
        targets.append(target)
    return targets


def compose_board(args: argparse.Namespace) -> Dict[str, Any]:
    canvas_id = get_or_create_canvas(args)
    images = normalize_images(args.base, args.image or [])
    payload = {
        "action": "compose_generation_board",
        "title": args.title or args.new_title or "Canvas Agent Board",
        "mode": args.mode,
        "prompt": args.prompt or "",
        "images": images,
        "image_node_ids": args.image_node or [],
        "providers": args.provider or [],
        "model": args.model or "",
        "models": args.model_list or [],
        "targets": parse_targets(args.target or [], args.ratio or ""),
        "count": args.count,
        "ratio": args.ratio or "source",
        "resolution": args.resolution,
        "quality": args.quality,
        "group_images": bool(args.multi_ref),
        "compare_providers": bool(args.compare_providers),
        "run": bool(args.run),
        "x": args.x or 0,
        "y": args.y or 0,
    }
    data = request_json(args.base, "POST", f"/api/canvas-agent/canvases/{canvas_id}/actions", payload, timeout=120)
    data["canvas_id"] = canvas_id
    return data


def create_task(base_url: str, plan: Dict[str, Any], index: int, timeout: float) -> Dict[str, Any]:
    payload = {
        "prompt": plan.get("prompt") or "Edit the reference images.",
        "provider_id": plan.get("provider_id") or "",
        "model": plan.get("model") or "",
        "size": plan.get("size") or "1024x1024",
        "quality": plan.get("quality") or "auto",
        "n": 1,
        "reference_images": [
            {"url": ref.get("url"), "name": ref.get("name") or "reference.png", "role": ref.get("role") or ""}
            for ref in (plan.get("reference_images") or [])
            if ref.get("url")
        ],
    }
    created_at = now_ms()
    task = request_json(base_url, "POST", "/api/canvas-image-tasks", payload, timeout=60)
    task_id = task.get("task_id")
    if not task_id:
        raise CanvasCtlError("创建生图任务失败：返回中没有 task_id")
    deadline = time.time() + timeout
    last = task
    while time.time() < deadline:
        time.sleep(1.5)
        last = request_json(base_url, "GET", f"/api/canvas-image-tasks/{task_id}", timeout=30)
        status = str(last.get("status") or "").lower()
        if status in DONE_STATUSES:
            result = last.get("result") or {}
            images = result.get("images") or []
            return {
                "ok": True,
                "status": status,
                "task_id": task_id,
                "images": images,
                "raw": last,
                "plan": plan,
                "index": index,
                "created_at": created_at,
                "run_ms": last.get("run_ms") or last.get("total_ms") or 0,
            }
        if status in FAIL_STATUSES:
            return {
                "ok": False,
                "status": status,
                "task_id": task_id,
                "error": last.get("error") or "任务失败",
                "raw": last,
                "plan": plan,
                "index": index,
                "created_at": created_at,
                "run_ms": last.get("run_ms") or last.get("total_ms") or 0,
            }
    return {
        "ok": False,
        "status": "timeout",
        "task_id": task_id,
        "error": f"任务超过 {int(timeout)} 秒未完成",
        "raw": last,
        "plan": plan,
        "index": index,
        "created_at": created_at,
        "run_ms": int((now_ms() - created_at)),
    }


def run_plan_tasks(args: argparse.Namespace, canvas_id: str, run_plan: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    expanded = []
    for plan in run_plan:
        for index in range(max(1, min(8, int(plan.get("count") or 1)))):
            expanded.append((plan, index + 1))
    if not expanded:
        return []
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(args.concurrency or 8))) as pool:
        futures = [
            pool.submit(create_task, args.base, plan, index, float(args.task_timeout))
            for plan, index in expanded
        ]
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {"ok": False, "status": "error", "error": str(exc), "images": []}
            results.append(result)
            plan = result.get("plan") or {}
            if not args.json:
                provider = plan.get("provider_name") or plan.get("provider_id") or "provider"
                state = "成功" if result.get("ok") else "失败"
                print(f"[{state}] {provider} / {plan.get('model') or ''} task={result.get('task_id') or '-'}")
    write_results_to_canvas(args.base, canvas_id, results)
    return results


def output_items_for_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    plan = result.get("plan") or {}
    items = []
    for url in result.get("images") or []:
        if not url:
            continue
        items.append({
            "url": url,
            "kind": "image",
            "source": "canvasctl",
            "provider_id": plan.get("provider_id") or "",
            "provider_name": plan.get("provider_name") or "",
            "model": plan.get("model") or "",
            "task_id": result.get("task_id") or "",
            "runMs": result.get("run_ms") or 0,
            "createdAt": now_ms(),
        })
    return items


def write_results_to_canvas(base_url: str, canvas_id: str, results: List[Dict[str, Any]]) -> None:
    data = request_json(base_url, "GET", f"/api/canvases/{canvas_id}", timeout=60)
    canvas = data.get("canvas") or {}
    nodes = canvas.get("nodes") or []
    logs = canvas.get("logs") or []
    by_id = {node.get("id"): node for node in nodes}
    generator_success = {}
    for result in results:
        plan = result.get("plan") or {}
        gen = by_id.get(plan.get("generator_id"))
        out = by_id.get(plan.get("output_id"))
        images = output_items_for_result(result)
        if out is not None and images:
            out.setdefault("images", []).extend(images)
        if gen is not None:
            if images:
                gen.setdefault("generatedOutputs", []).extend(images)
                gen["runStatus"] = "done"
                gen["runError"] = ""
                generator_success[gen.get("id")] = True
            elif not generator_success.get(gen.get("id")):
                gen["runStatus"] = "failed"
                gen["runError"] = str(result.get("error") or result.get("status") or "任务失败")[:500]
        logs.insert(0, {
            "id": f"canvasctl_{now_ms()}_{len(logs)}",
            "createdAt": now_ms(),
            "status": "done" if result.get("ok") else "failed",
            "nodeType": "generator",
            "model": plan.get("model") or "",
            "prompt": plan.get("prompt") or "",
            "outputs": [item["url"] for item in images],
            "error": "" if result.get("ok") else str(result.get("error") or result.get("status") or "任务失败")[:1000],
            "platform": plan.get("provider_name") or plan.get("provider_id") or "",
            "runMs": result.get("run_ms") or 0,
            "task_id": result.get("task_id") or "",
            "request": {
                "provider_id": plan.get("provider_id") or "",
                "model": plan.get("model") or "",
                "size": plan.get("size") or "",
                "quality": plan.get("quality") or "",
                "reference_count": plan.get("reference_count") or 0,
            },
        })
    payload = {
        "title": canvas.get("title") or "未命名画布",
        "icon": canvas.get("icon") or "layers",
        "nodes": nodes,
        "connections": canvas.get("connections") or [],
        "viewport": canvas.get("viewport") or {"x": 0, "y": 0, "scale": 1},
        "logs": logs[:500],
        "settings": canvas.get("settings") or {},
        "client_id": "canvasctl",
        "base_updated_at": canvas.get("updated_at") or 0,
    }
    request_json(base_url, "PUT", f"/api/canvases/{canvas_id}", payload, timeout=120)


def cmd_compose(args: argparse.Namespace) -> int:
    data = compose_board(args)
    canvas_id = data.get("canvas_id") or ""
    result = data.get("result") or {}
    run_results = []
    if args.run:
        run_results = run_plan_tasks(args, canvas_id, result.get("run_plan") or [])
    summary = {
        "canvas_id": canvas_id,
        "canvas_url": f"{clean_base_url(args.base)}/static/canvas.html?canvas={canvas_id}",
        "result": result,
        "run_results": run_results,
    }
    if args.json:
        print_json(summary)
    else:
        print(f"画布：{summary['canvas_url']}")
        print(f"节点：prompts={len(result.get('prompt_ids') or [])} generators={len(result.get('generator_ids') or [])} outputs={len(result.get('output_ids') or [])}")
        if args.run:
            ok_count = len([item for item in run_results if item.get("ok")])
            print(f"执行：{ok_count}/{len(run_results)} 成功")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control Infinite Canvas automation.")
    parser.add_argument("--base", default=DEFAULT_BASE_URL, help="Canvas service base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check service and image providers")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    providers = sub.add_parser("providers", help="List providers")
    providers.add_argument("--json", action="store_true")
    providers.add_argument("--image-only", action="store_true", default=True)
    providers.set_defaults(func=cmd_providers)

    compose = sub.add_parser("compose", help="Create a visible generation board")
    compose.add_argument("--json", action="store_true")
    compose.add_argument("--canvas", default="")
    compose.add_argument("--new-title", default="")
    compose.add_argument("--title", default="")
    compose.add_argument("--mode", default="reference_edit")
    compose.add_argument("--prompt", default="")
    compose.add_argument("--target", action="append", default=[], help="target title::prompt")
    compose.add_argument("--image", action="append", default=[], help="local path or /assets, /output, http(s), data image URL")
    compose.add_argument("--image-node", action="append", default=[])
    compose.add_argument("--provider", action="append", default=[])
    compose.add_argument("--model", default="")
    compose.add_argument("--model-list", action="append", default=[])
    compose.add_argument("--count", type=int, default=1)
    compose.add_argument("--ratio", default=None)
    compose.add_argument("--resolution", default="1k")
    compose.add_argument("--quality", default="auto")
    compose.add_argument("--multi-ref", action="store_true", help="Use all input images together as references for each target")
    compose.add_argument("--compare-providers", action="store_true", help="Create one generator/output pair for each provider")
    compose.add_argument("--x", type=float, default=0)
    compose.add_argument("--y", type=float, default=0)
    compose.add_argument("--run", action="store_true")
    compose.add_argument("--concurrency", type=int, default=8)
    compose.add_argument("--task-timeout", type=int, default=900)
    compose.set_defaults(func=cmd_compose)
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.base = clean_base_url(args.base)
    try:
        return args.func(args)
    except CanvasCtlError as exc:
        print(f"canvasctl: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("canvasctl: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
