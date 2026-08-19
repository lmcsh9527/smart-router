#!/usr/bin/env python3
"""TokenSaver 智能路由网关 MVP

OpenAI 兼容网关：
- 接收 POST /v1/chat/completions
- 提取最后一条用户消息 → SquillaRouter.classify() 判档（c0-c3）
- 按档位映射到 9router 模型并转发
- 支持 stream=true（SSE 透传）与非流式

启动：
    python3 gateway.py
    或
    uvicorn gateway:app --host 0.0.0.0 --port 20130
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy

# ---------- 配置（可用环境变量覆盖） ----------
LISTEN_HOST = os.getenv("TOKENSAVER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("TOKENSAVER_PORT", "20130"))
UPSTREAM_BASE = os.getenv("ROUTER_UPSTREAM_URL", "http://localhost:20128/v1").rstrip("/")
UPSTREAM_KEY = os.getenv("ROUTER_UPSTREAM_KEY", "sk-8f1a75766b63858d-y4witj-bcf8227b")
UPSTREAM_TIMEOUT = float(os.getenv("ROUTER_UPSTREAM_TIMEOUT", "600"))
# 首字节等待超时：高档模型推理慢，超时后降级到低档，保证请求能出结果
UPSTREAM_READ_TIMEOUT = float(os.getenv("ROUTER_UPSTREAM_READ_TIMEOUT", "90"))

# 档位 → 9router 模型（任务给定映射）
TIER_MODEL_MAP: dict[str, str] = {
    "c0": "jy/deepseek-v4-flash",
    "c1": "jy/deepseek-v4-flash",
    "c2": "jy/deepseek-v4-flash-0731",
    "c3": "jy/deepseek-v4-pro",
}
VALID_TIERS: list[str] = ["c0", "c1", "c2", "c3"]
DEFAULT_TIER = "c1"

# 降级链：高档模型连接/响应超时 → 依次降级到低档，保证可用性
MODEL_FALLBACK_CHAIN: dict[str, list[str]] = {
    "jy/deepseek-v4-pro": [
        "jy/deepseek-v4-pro",
        "jy/deepseek-v4-flash-0731",
        "jy/deepseek-v4-flash",
    ],
    "jy/deepseek-v4-flash-0731": [
        "jy/deepseek-v4-flash-0731",
        "jy/deepseek-v4-flash",
    ],
    "jy/deepseek-v4-flash": ["jy/deepseek-v4-flash"],
}

# 智能路由触发值：客户端 model 为这些值时走自动路由
AUTO_MODEL_TOKENS = {"auto", "tokensaver", "tokensaver-auto", ""}

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tokensaver")

# ---------- 全局单例：分类器（模型加载一次，避免每请求重载） ----------
_strategy: V4Phase3Strategy | None = None
_strategy_lock = asyncio.Lock()


async def get_strategy() -> V4Phase3Strategy:
    global _strategy
    if _strategy is None:
        async with _strategy_lock:
            if _strategy is None:
                logger.info("loading V4Phase3Strategy ...")
                _strategy = await asyncio.to_thread(V4Phase3Strategy)
                logger.info(
                    "V4Phase3Strategy ready | bundle=%s available=%s version=%s",
                    _strategy.bundle_dir,
                    _strategy._available,
                    _strategy._model_version,
                )
    return _strategy


def pick_model(tier: str) -> str:
    return TIER_MODEL_MAP.get(tier, TIER_MODEL_MAP[DEFAULT_TIER])


def last_user_text(messages: list[dict]) -> str:
    """取最后一条 role=user 的消息文本（支持 content 为 str 或分段数组）。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            # OpenAI 多模态格式：拼 text 段
            parts = [
                str(seg.get("text", ""))
                for seg in content
                if isinstance(seg, dict) and seg.get("type") == "text"
            ]
            return " ".join(p for p in parts if p)
        return str(content)
    return ""


def _parse_upstream_json(text: str) -> dict | None:
    """解析上游 JSON。

    兼容两种情况：
    1. 单个 JSON 对象（标准 OpenAI 响应）。
    2. 多个 JSON 对象拼接（9router 偶发返回 reasoning 中间态 + 最终态），取最后一个。
    """
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    objs: list[Any] = []
    while idx < n:
        while idx < n and text[idx] in " \t\r\n":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except Exception:
            break
        objs.append(obj)
        idx = end

    dicts = [o for o in objs if isinstance(o, dict)]
    if dicts:
        if len(dicts) > 1:
            logger.warning(
                "upstream returned %d concatenated JSON objects; using the last one",
                len(dicts),
            )
        return dicts[-1]
    return None


app = FastAPI(title="TokenSaver Smart Router Gateway", version="0.1.0")


@app.get("/v1/models")
async def list_models() -> dict:
    """列出网关支持的模型（含 auto）。"""
    return {
        "object": "list",
        "data": [
            {"id": "auto", "object": "model", "owned_by": "tokensaver"},
            *[
                {"id": m, "object": "model", "owned_by": "tokensaver"}
                for m in sorted(set(TIER_MODEL_MAP.values()))
            ],
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception as exc:
        return JSONResponse(status_code=400, content={"error": {"message": f"invalid JSON: {exc}", "type": "invalid_request_error"}})

    if not isinstance(body, dict) or "messages" not in body:
        return JSONResponse(status_code=400, content={"error": {"message": "missing 'messages'", "type": "invalid_request_error"}})

    messages = body.get("messages") or []
    client_model = str(body.get("model", "auto") or "auto").strip()
    stream = bool(body.get("stream", False))
    text = last_user_text(messages)

    # ---- 路由决策 ----
    if client_model.lower() in AUTO_MODEL_TOKENS:
        strategy = await get_strategy()
        if text:
            tier, confidence, source, extra = await strategy.classify(
                text, valid_tiers=VALID_TIERS
            )
        else:
            tier, confidence, source, extra = DEFAULT_TIER, 0.0, "empty", {}
        upstream_model = pick_model(tier)
        route_info = {
            "tier": tier,
            "confidence": round(float(confidence), 4),
            "source": source,
            "route_class": extra.get("route_class"),
            "difficulty": round(float(extra.get("difficulty", 0.0)), 4),
            "upstream_model": upstream_model,
        }
        logger.info(
            "ROUTE client_model=%s tier=%s confidence=%.4f source=%s route_class=%s difficulty=%.4f upstream_model=%s text=%r",
            client_model, tier, confidence, source,
            extra.get("route_class"), extra.get("difficulty"),
            upstream_model, text[:80],
        )
    else:
        # 客户端显式指定模型：直接转发，不路由
        upstream_model = client_model
        route_info = {"mode": "passthrough", "upstream_model": upstream_model}
        logger.info("ROUTE passthrough model=%s text=%r", upstream_model, text[:80])

    # ---- 构造上游请求（带降级重试链） ----
    payload = dict(body)
    headers = {
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "Content-Type": "application/json",
    }
    upstream_url = f"{UPSTREAM_BASE}/chat/completions"

    # 流式一旦开始输出就不能降级（只能透传），故流式只尝试首选模型
    model_chain = [upstream_model] if stream else MODEL_FALLBACK_CHAIN.get(
        upstream_model, [upstream_model]
    )

    last_err: str | None = None
    last_status: int | None = None
    used_model: str | None = None

    for idx, model in enumerate(model_chain):
        payload["model"] = model
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                UPSTREAM_TIMEOUT, connect=15.0, read=UPSTREAM_READ_TIMEOUT
            )
        )
        try:
            req = client.build_request("POST", upstream_url, json=payload, headers=headers)
            # 统一以流式方式建立连接（可感知首字节/读超时），非流式再收完整 body
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            last_err = str(exc) or type(exc).__name__
            await client.aclose()
            if idx < len(model_chain) - 1:
                logger.warning(
                    "UPSTREAM attempt %d failed model=%s err=%s -> fallback to %s",
                    idx + 1, model, exc, model_chain[idx + 1],
                )
                continue
            break

        if upstream.status_code != 200:
            err_body = ""
            try:
                err_body = (await upstream.aread()).decode("utf-8", "replace")
            except Exception:
                pass
            await client.aclose()
            last_status = upstream.status_code
            last_err = err_body[:300] or f"upstream status {upstream.status_code}"
            if 500 <= upstream.status_code < 600 and idx < len(model_chain) - 1:
                logger.warning(
                    "UPSTREAM attempt %d HTTP %s model=%s -> fallback to %s",
                    idx + 1, upstream.status_code, model, model_chain[idx + 1],
                )
                continue
            break

        used_model = model
        break

    if used_model is None:
        logger.error(
            "upstream all attempts failed last_status=%s last_err=%s",
            last_status, last_err,
        )
        return JSONResponse(
            status_code=last_status if last_status and 400 <= last_status < 500 else 502,
            content={
                "error": {
                    "message": f"upstream gateway error: {last_err or 'unreachable'}",
                    "type": "upstream_error",
                }
            },
        )

    # 降级发生时更新路由元数据，如实反映最终实际使用的模型
    if used_model != upstream_model:
        logger.warning(
            "ROUTE_DEGRADED original=%s used=%s tier=%s",
            upstream_model, used_model, route_info.get("tier"),
        )
        route_info["upstream_model"] = used_model
        route_info["degraded_from"] = upstream_model

    # ---- 响应头注入路由信息（不破坏 OpenAI body 兼容） ----
    resp_headers = {
        "X-TokenSaver-Tier": str(route_info.get("tier", "")),
        "X-TokenSaver-Upstream-Model": used_model,
    }

    if stream:
        async def gen():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await client.aclose()

        return StreamingResponse(
            gen(),
            status_code=200,
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # ---- 非流式：健壮解析 JSON（9router 偶发返回多段 JSON 拼接） ----
    raw = (await upstream.aread()).decode("utf-8", "replace")
    await client.aclose()

    data = _parse_upstream_json(raw)
    if data is None:
        logger.error("upstream non-JSON response body=%s", raw[:500])
        return JSONResponse(status_code=502, content={"error": {"message": "upstream returned non-JSON", "type": "upstream_error"}})

    # 附加路由元数据（放在顶层自定义字段，标准客户端忽略未知字段）
    data["x_tokensaver"] = route_info
    return JSONResponse(status_code=200, content=data, headers=resp_headers)


@app.get("/healthz")
async def healthz() -> dict:
    strategy = await get_strategy()
    return {
        "status": "ok",
        "router_available": bool(strategy._available),
        "router_version": strategy._model_version,
        "upstream": UPSTREAM_BASE,
        "time": time.time(),
    }


if __name__ == "__main__":
    import uvicorn

    print(f"TokenSaver gateway listening on http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"upstream: {UPSTREAM_BASE}")
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
