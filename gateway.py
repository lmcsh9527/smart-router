#!/usr/bin/env python3
"""TokenSaver 智能路由网关 v3（机缘直连版）

OpenAI 兼容网关：
- 接收 POST /v1/chat/completions
- 提取最后一条用户消息 → OpenSquilla SquillaRouter.classify() 判档（c0-c3）
- 按档位映射到机缘(tokenrhythm) DeepSeek V4 Flash/Pro
- 单路直连机缘 API，不经 9router，不并发抢答
- 支持 stream=true（SSE 透传）与非流式

启动（需先设置 ROUTER_UPSTREAM_KEY）：
    export ROUTER_UPSTREAM_KEY=$(cat jy_api_key)
    python3 gateway.py
    或
    uvicorn gateway:app --host 0.0.0.0 --port 20130

架构说明：
- 路由判决：OpenSquilla 内置分类器（不依赖外部 LLM）
- 模型映射：c0/c1→deepseek-v4-flash，c2/c3→deepseek-v4-pro
- 上游：https://tokenrhythm.studio/v1（机缘直连）
- 降级保护：pro 超时→flash，flash 超时失败
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

# ---------- 本地 vendor 化 opensquilla 路由模块 ----------
# 深度融合：把 opensquilla 的 squilla_router（v4_phase3 分类器 + 模型资产）提取到
# vendor/opensquilla/ 下，脱离外部 pip 包，便于深改 classify 逻辑。
# 原 import 语句不变：from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy
_VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")
if _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)
# 子进程（train_worker）也要能看到 vendor 化 opensquilla：同步进 PYTHONPATH
_pp = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
if _VENDOR_DIR not in _pp:
    os.environ["PYTHONPATH"] = os.pathsep.join([_VENDOR_DIR] + _pp)

from opensquilla.squilla_router.v4_phase3 import V4Phase3Strategy, default_bundle_dir
from opensquilla.squilla_router.self_learning import hooks as self_learning_hooks
from opensquilla.squilla_router.self_learning.capture import build_train_sample
from opensquilla.squilla_router.self_learning.feedback import scan_feedback_stats, write_feedback
from opensquilla.squilla_router.self_learning.gates import evaluate_training_gates
from opensquilla.squilla_router.self_learning.orchestrator import maybe_run_update_router
from opensquilla.squilla_router.self_learning.promotion import read_active, resolve_active_bundle_dir
from opensquilla.squilla_router.self_learning.state import load_train_state, scan_event_store
from opensquilla.squilla_router.self_learning.store import (
    agent_data_dir,
    self_learning_disabled_by_env,
    write_sample,
)

# ---------- 配置（可用环境变量覆盖） ----------
LISTEN_HOST = os.getenv("TOKENSAVER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("TOKENSAVER_PORT", "20130"))
# 机缘(tokenrhythm)直连，不再经过 9router
UPSTREAM_BASE = os.getenv("ROUTER_UPSTREAM_URL", "https://tokenrhythm.studio/v1").rstrip("/")


def _load_upstream_key() -> str:
    """优先读项目目录 jy_api_key 文件（600，机缘直连 key），环境变量仅作兜底。

    注意：环境变量 ROUTER_UPSTREAM_KEY 曾用于 9router key，shell/launchd 可能有残留，
    若优先读环境变量会拿错 key（9router key 打机缘 → 401）。故文件优先。
    """
    keyfile = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jy_api_key")
    try:
        with open(keyfile, "r", encoding="utf-8") as f:
            key = f.read().strip()
        if key:
            return key
    except Exception:
        pass
    return os.getenv("ROUTER_UPSTREAM_KEY", "")


UPSTREAM_KEY = _load_upstream_key()
print(f"upstream key loaded: len={len(UPSTREAM_KEY)} head={UPSTREAM_KEY[:4] or 'EMPTY'!r} tail={UPSTREAM_KEY[-6:] or 'EMPTY'!r}")

# ---------- 模型池（后台可手工维护） ----------
# models_pool.json（600 权限）：
#   {"models": [{"id","name","base_url","api_key_file","api_key","model","tiers","priority","enabled","note"}]}
# - api_key_file: 项目目录内 key 文件名（优先，如 jy_api_key）
# - api_key:      直接填写的 key（仅当 api_key_file 为空时使用；存储于池文件，600 权限）
# - tiers:        该模型服务的档位，如 ["c0","c1"]
# - priority:     同档位多模型时优先级（数字小优先）
MODELS_POOL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models_pool.json")


def load_models_pool() -> list[dict]:
    try:
        with open(MODELS_POOL_FILE, "r", encoding="utf-8") as f:
            return list((json.load(f) or {}).get("models", []))
    except Exception:
        return []


def save_models_pool(models: list[dict]) -> None:
    with open(MODELS_POOL_FILE, "w", encoding="utf-8") as f:
        json.dump({"models": models}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(MODELS_POOL_FILE, 0o600)
    except Exception:
        pass


def resolve_model_key(m: dict) -> str:
    """按 api_key_file → api_key 顺序解析模型 key。"""
    kf = (m.get("api_key_file") or "").strip()
    if kf:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), kf)
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass
    return (m.get("api_key") or "").strip()


# ---------- 供应商库（providers） ----------
# providers.json（600 权限）：保存服务商 base_url + key（文件引用或直填），
# 发现模型后自动保存，下次添加模型直接选供应商，免重复输入。
PROVIDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")


def load_providers() -> list[dict]:
    try:
        with open(PROVIDERS_FILE, "r", encoding="utf-8") as f:
            return list((json.load(f) or {}).get("providers", []))
    except Exception:
        return []


def save_providers(providers: list[dict]) -> None:
    with open(PROVIDERS_FILE, "w", encoding="utf-8") as f:
        json.dump({"providers": providers}, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(PROVIDERS_FILE, 0o600)
    except Exception:
        pass


def resolve_provider_key(p: dict) -> str:
    """按 api_key_file → api_key 顺序解析供应商 key。"""
    kf = (p.get("api_key_file") or "").strip()
    if kf:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), kf)
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
            if key:
                return key
        except Exception:
            pass
    return (p.get("api_key") or "").strip()


def provider_public(p: dict) -> dict:
    pub = dict(p)
    pub["api_key_masked"] = mask_key(resolve_provider_key(p))
    pub["api_key"] = ""
    return pub


def find_provider_by_base_url(base_url: str) -> dict | None:
    b = (base_url or "").strip().rstrip("/")
    for p in load_providers():
        if (p.get("base_url") or "").strip().rstrip("/") == b:
            return p
    return None


def mask_key(key: str) -> str:
    if not key:
        return ""
    return f"{key[:4]}***{key[-6:]}"


# ---------- DSH 接入（一键设置默认智能路由） ----------
DSH_SETTINGS = os.path.expanduser("~/Library/Application Support/dsh-desktop/harness/settings.yaml")
TOKENSAVER_PROVIDER = "router9"  # DSH 配置里指向本网关的 provider 名
TOKENSAVER_MODEL = "auto"


def _read_dsh_settings() -> dict:
    try:
        import yaml
        with open(DSH_SETTINGS, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _write_dsh_settings(data: dict) -> None:
    import yaml
    import shutil
    if os.path.exists(DSH_SETTINGS):
        shutil.copy2(DSH_SETTINGS, DSH_SETTINGS + f".bak-tokensaver-{time.strftime('%Y%m%d-%H%M%S')}")
    with open(DSH_SETTINGS, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _dsh_status_info() -> dict:
    data = _read_dsh_settings()
    provs = (data.get("llm-pi-ai", {}) or {}).get("providers", {}) or {}
    provider_ok = TOKENSAVER_PROVIDER in provs
    am = data.get("agent-default-model", {}) or {}
    default_provider = am.get("provider", "")
    default_model = am.get("model", "")
    enabled = provider_ok and default_provider == TOKENSAVER_PROVIDER and default_model == TOKENSAVER_MODEL
    return {
        "provider_configured": provider_ok,
        "default_provider": default_provider,
        "default_model": default_model,
        "enabled": enabled,
        "settings_path": DSH_SETTINGS,
        "provider_name": TOKENSAVER_PROVIDER,
    }


def infer_model_type(model_id: str) -> str:
    """按模型名推断类型：image（生图）/ embedding（向量）/ text（默认）。"""
    m = (model_id or "").lower()
    emb_kw = ["embedding", "embed", "text-embedding", "bge", "rerank", "e5-", "nv-embed"]
    img_kw = ["image", "imagine", "dalle", "sdxl", "flux", "stable-diffusion", "midjourney",
              "draw", "art", "pic", "photo", "tts", "audio", "speech"]
    if any(k in m for k in emb_kw):
        return "embedding"
    if any(k in m for k in img_kw):
        return "image"
    return "text"


_CONTEXT_CATALOG_CACHE: dict[str, int] | None = None


def _load_context_catalog() -> dict[str, int]:
    """从本地 opensquilla 模型目录加载 model_id → contextWindow 映射。"""
    global _CONTEXT_CATALOG_CACHE
    if _CONTEXT_CATALOG_CACHE is not None:
        return _CONTEXT_CATALOG_CACHE
    catalog: dict[str, int] = {}
    import glob
    files = glob.glob(os.path.expanduser("~/.opensquilla/state/model_catalog/*.json"))
    for f in files:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            for ent in (d.get("entitlements") or {}).values():
                for name, cfg in (ent.get("models") or {}).items():
                    ctx = cfg.get("contextWindow")
                    if ctx and name not in catalog:
                        catalog[name] = int(ctx)
        except Exception:
            continue
    _CONTEXT_CATALOG_CACHE = catalog
    return catalog


def infer_context_window(model_id: str) -> int | None:
    """推断模型上下文长度：优先本地目录，找不到返回 None。"""
    if not model_id:
        return None
    return _load_context_catalog().get(model_id)


def pick_model_from_pool(tier: str) -> tuple[str, dict] | tuple[None, None]:
    """从模型池选模型：优先 enabled 且 tier 匹配，按 priority 升序。"""
    models = load_models_pool()
    matched = [
        m for m in models
        if m.get("enabled", True) and tier in (m.get("tiers") or [])
    ]
    matched.sort(key=lambda m: int(m.get("priority", 100)))
    if not matched:
        return None, None
    m = matched[0]
    model_name = m.get("model") or ""
    return model_name, m


def build_fallback_chain_from_pool(tier: str, first_model: str) -> list[str]:
    """降级链：首选 + 池内其余 enabled 模型（同档优先，再低档兜底）。"""
    models = load_models_pool()
    enabled = [m for m in models if m.get("enabled", True) and (m.get("model") or "")]
    # 同档位在前，其他在后；同档按 priority
    same = [m for m in enabled if tier in (m.get("tiers") or [])]
    other = [m for m in enabled if tier not in (m.get("tiers") or [])]
    same.sort(key=lambda m: int(m.get("priority", 100)))
    other.sort(key=lambda m: int(m.get("priority", 100)))
    chain = [m["model"] for m in same] + [m["model"] for m in other]
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for x in chain:
        if x not in seen:
            seen.add(x)
            out.append(x)
    if first_model not in out:
        out.insert(0, first_model)
    return out


def build_upstream_chain(tier: str, first_model: str, first_cfg: dict | None, stream: bool) -> list[tuple[str, str, str]]:
    """构造上游降级链，每项 = (base_url, key, model)。

    - 池驱动（first_cfg 来自模型池）：所有 enabled 模型按「同档优先 + priority 升序」排列，
      每个模型用自己配置的 base_url/key；流式只取首选。
    - 回退（池未命中）：全局 UPSTREAM_BASE + UPSTREAM_KEY + MODEL_FALLBACK_CHAIN。
    """
    if first_cfg is not None:
        models = load_models_pool()
        enabled = [m for m in models if m.get("enabled", True) and (m.get("model") or "")]
        same = [m for m in enabled if tier in (m.get("tiers") or [])]
        other = [m for m in enabled if tier not in (m.get("tiers") or [])]
        same.sort(key=lambda m: int(m.get("priority", 100)))
        other.sort(key=lambda m: int(m.get("priority", 100)))
        ordered = same + other
        seen: set[str] = set()
        chain: list[tuple[str, str, str]] = []
        for m in ordered:
            name = m.get("model")
            if not name or name in seen:
                continue
            seen.add(name)
            chain.append(
                ((m.get("base_url") or UPSTREAM_BASE).rstrip("/"), resolve_model_key(m), name)
            )
        if stream:
            return chain[:1]
        return chain

    # 回退：全局直连 + 旧降级链
    fallback_models = [first_model] if stream else MODEL_FALLBACK_CHAIN.get(
        first_model, [first_model]
    )
    return [(UPSTREAM_BASE, UPSTREAM_KEY, m) for m in fallback_models]
UPSTREAM_TIMEOUT = float(os.getenv("ROUTER_UPSTREAM_TIMEOUT", "600"))
# 首字节等待超时：高档模型推理慢，超时后降级到低档，保证请求能出结果
UPSTREAM_READ_TIMEOUT = float(os.getenv("ROUTER_UPSTREAM_READ_TIMEOUT", "90"))

# 档位 → 模型名（智能路由映射）
# OpenSquilla classify() 判档 → 直连机缘(tokenrhythm)单模型单路转发，不经过 9router。
TIER_MODEL_MAP: dict[str, str] = {
    "c0": "deepseek-v4-flash",  # 最简单 → V4 Flash
    "c1": "deepseek-v4-flash",  # 简单 → V4 Flash
    "c2": "deepseek-v4-pro",    # 中等 → V4 Pro
    "c3": "deepseek-v4-pro",    # 复杂 → V4 Pro
}
VALID_TIERS: list[str] = ["c0", "c1", "c2", "c3"]
DEFAULT_TIER = "c1"

# 降级链：高档模型连接/响应超时 → 依次降级到低档，保证可用性
MODEL_FALLBACK_CHAIN: dict[str, list[str]] = {
    "deepseek-v4-pro": [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
    ],
    "deepseek-v4-flash": ["deepseek-v4-flash"],
}

# 智能路由触发值：客户端 model 为这些值时走自动路由
AUTO_MODEL_TOKENS = {"auto", "tokensaver", "tokensaver-auto", ""}

# ---------- LLM 智能分类器（实验特性，默认关闭） ----------
# 开启方式：环境变量 LLM_CLASSIFIER_ENABLED=1
LLM_CLASSIFIER_ENABLED = os.getenv("LLM_CLASSIFIER_ENABLED", "0") == "1"
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "deepseek-v4-flash")
CLASSIFIER_TIMEOUT = float(os.getenv("CLASSIFIER_TIMEOUT", "20"))

# 分类 prompt：只让 flash 输出极小 JSON，成本 ~150 token/次（免费池，≈0 元）
CLASSIFIER_PROMPT = (
    "你是任务分类器。分析用户请求，只输出一个 JSON 对象，不要输出其他文字：\n"
    '{"task_type":"query|reasoning|creative|coding|translation|analysis",'
    '"reasoning_depth":0-10,"precision_req":0-10,"safety_risk":"none|low|medium|high|critical"}\n'
    "含义：reasoning_depth=需要几步逻辑推理；precision_req=答案精确度要求；"
    "safety_risk=医疗/法律/金融等风险。\n\n用户请求：{prompt}"
)

# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("tokensaver")

# ---------- 全局单例：分类器（模型加载一次，避免每请求重载） ----------
_strategy: V4Phase3Strategy | None = None
_strategy_lock = asyncio.Lock()


def _resolve_active_bundle_dir() -> str | None:
    """自学习 promotion 切换后，优先加载 learned 模型包；异常回退 baseline。"""
    try:
        b = resolve_active_bundle_dir()
        return str(b) if b is not None else None
    except Exception as exc:
        logger.warning("SELFLEARN resolve active bundle failed: %s", exc)
        return None


def _reset_strategy_cache() -> None:
    global _strategy
    _strategy = None
    logger.info("SELFLEARN strategy cache invalidated (promotion/rollback)")


# 训练 orchestrator 在 promote/rollback 后会调 hooks.invalidate_router_cache()
self_learning_hooks.set_cache_invalidator(_reset_strategy_cache)


async def get_strategy() -> V4Phase3Strategy:
    global _strategy
    if _strategy is None:
        async with _strategy_lock:
            if _strategy is None:
                bundle_dir = _resolve_active_bundle_dir()
                logger.info("loading V4Phase3Strategy ... bundle=%s", bundle_dir or "baseline(default)")
                if bundle_dir:
                    _strategy = await asyncio.to_thread(
                        V4Phase3Strategy, bundle_dir=bundle_dir,
                        emit_train_features=True, emit_raw_bge=True,
                    )
                else:
                    _strategy = await asyncio.to_thread(
                        V4Phase3Strategy,
                        emit_train_features=True, emit_raw_bge=True,
                    )
                logger.info(
                    "V4Phase3Strategy ready | bundle=%s available=%s version=%s",
                    _strategy.bundle_dir,
                    _strategy._available,
                    _strategy._model_version,
                )
    return _strategy


def pick_model(tier: str) -> str:
    return TIER_MODEL_MAP.get(tier, TIER_MODEL_MAP[DEFAULT_TIER])


async def classify_task(text: str) -> dict:
    """用轻量模型（flash 组合）做任务分类，返回结构化特征。

    失败时返回空 dict（调用方回退 SquillaRouter）。
    成本：~150 token/次，走 flashfree 免费池优先。
    """
    if not text or not text.strip():
        return {"task_type": "query", "reasoning_depth": 1, "precision_req": 1, "safety_risk": "none"}
    prompt = CLASSIFIER_PROMPT.format(prompt=text[:2000])
    payload = {
        "model": CLASSIFIER_MODEL,
        "messages": [
            {"role": "system", "content": "你是任务分类器，严格输出 JSON。"},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": 150,
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {UPSTREAM_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{UPSTREAM_BASE}/chat/completions"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(CLASSIFIER_TIMEOUT, connect=10.0, read=CLASSIFIER_TIMEOUT)
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code != 200:
                logger.warning("CLASSIFIER upstream HTTP %s err=%s", resp.status_code, resp.text[:200])
                return {}
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
            start, end = content.find("{"), content.rfind("}")
            if start == -1 or end == -1:
                logger.warning("CLASSIFIER no JSON in response: %r", content[:200])
                return {}
            parsed = json.loads(content[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
    except Exception as exc:
        logger.warning("CLASSIFIER exception: %s", exc)
        return {}


def classify_to_tier(task: dict) -> str:
    """把结构化任务特征映射到档位 c0-c3。"""
    try:
        risk = str(task.get("safety_risk", "none") or "none").lower()
        if risk in ("high", "critical"):
            return "c3"
        depth = float(task.get("reasoning_depth", 0) or 0)
        precision = float(task.get("precision_req", 0) or 0)
        if depth >= 7 or precision >= 9:
            return "c3"
        if depth >= 4 or precision >= 6:
            return "c2"
        return "c0"
    except Exception:
        return "c0"


# 升级词：用户明示要"最好/最强/最高"能力时，强制路由到最高档（c3→pro）
_UPGRADE_HINTS = [
    "最好的模型", "最强模型", "最高能力", "最高思考", "拿出你最高的", "拿出你最强",
    "用最好的模型", "用最强", "深度思考", "深度分析", "最高水平", "全力以赴",
    "best model", "strongest", "deepest thinking", "maximum effort", "use the best",
]

def _apply_upgrade_hint(text: str, tier: str) -> str:
    """用户明示要最强模型时，把 tier 强制升到 c3。"""
    if not text:
        return tier
    low = text.lower()
    if any(hint in low for hint in _UPGRADE_HINTS):
        return "c3"
    return tier


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


def has_new_image(messages: list[dict]) -> bool:
    """只检测【当前轮】（最新一条 user 消息）是否含图片——决定是否走识图模型。
    历史回合里的旧图不再影响路由（避免长上下文因残留截图被全走识图模型）。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") in ("image_url", "image", "input_image"):
                    return True
        return False
    return False


def strip_image_parts(messages: list[dict]) -> list[dict]:
    """剥掉 messages 中所有图片段（image_url/image/input_image），返回文本模型可用的副本。"""
    out = []
    for msg in (messages or []):
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        m = dict(msg)
        content = m.get("content", "")
        if isinstance(content, list):
            m["content"] = [seg for seg in content if not (isinstance(seg, dict) and seg.get("type") in ("image_url", "image", "input_image"))]
        out.append(m)
    return out


# ---------- 自学习采集（只存特征向量，不存原文；best-effort 不阻塞主流程） ----------
SELF_LEARN_AGENT_ID = "tokensaver"
_session_turn_counters: dict[str, int] = {}


def _next_turn_index(session_key: str) -> int:
    n = _session_turn_counters.get(session_key, 0)
    _session_turn_counters[session_key] = n + 1
    return n


def _capture_train_sample(
    *,
    decision_id: str,
    session_key: str,
    tier: str,
    confidence: float,
    source: str,
    extra: dict,
    turn_index: int,
) -> bool:
    """classify 后采集一条训练样本；任何异常只记日志，绝不影响路由主流程。"""
    try:
        if self_learning_disabled_by_env():
            return False
        features = (extra or {}).get("_train_features")
        if not isinstance(features, dict) or features.get("features_390") is None:
            return False
        metadata = {
            "routing_train_features": features,
            "routing_source": source,
            "routing_extra": extra,
            "routed_tier": tier,
            "routing_confidence": confidence,
            "routing_train_turn_index": turn_index,
            "router_decision_id": decision_id,
        }
        sample = build_train_sample(session_key=session_key, metadata=metadata)
        if sample is None:
            return False
        path = write_sample(sample, SELF_LEARN_AGENT_ID)
        logger.info(
            "SELFLEARN captured decision_id=%s session=%s tier=%s source=%s schema=%s path=%s",
            decision_id, session_key, tier, source, sample.feature_schema_version, path,
        )
        return True
    except Exception as exc:
        logger.warning("SELFLEARN capture failed (best-effort): %s", exc)
        return False


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

# ---------- 实时调用记录（内存，重启清零） ----------
CALL_LOG: deque = deque(maxlen=200)


def _body_text_summary(body_bytes: bytes, limit: int = 50) -> str:
    """从请求体提取最后一条用户消息摘要（用于实时调用展示）。"""
    try:
        data = json.loads(body_bytes or b"{}")
        messages = data.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                c = msg.get("content")
                if isinstance(c, str):
                    return c.strip().replace("\n", " ")[:limit]
                if isinstance(c, list):
                    parts = [str(s.get("text", "")) for s in c if isinstance(s, dict) and s.get("type") == "text"]
                    return " ".join(p for p in parts if p).strip().replace("\n", " ")[:limit]
                return str(c)[:limit]
    except Exception:
        pass
    return ""


# ---------- 调用记录持久化（按日期 JSONL，重启不丢） ----------
CALLS_DATA_DIR = os.path.expanduser("~/.opensquilla/router/data/tokensaver")


def _calls_path(day: str) -> str:
    return os.path.join(CALLS_DATA_DIR, f"calls-{day}.jsonl")


def _persist_call_entry(entry: dict) -> None:
    """Best-effort 把一条调用记录 append 到当日文件；失败只记日志。"""
    try:
        day = str(entry.get("ts") or "")[:10] or time.strftime("%Y-%m-%d", time.gmtime())
        path = _calls_path(day)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        logger.warning("CALL persist failed (best-effort): %s", exc)


def _load_call_rows(period: str) -> list[dict]:
    """读 calls-YYYY-MM-DD.jsonl 并按 period（today/7d/30d/all）过滤。"""
    today = datetime.now(UTC).date()
    days = {"today": 0, "7d": 7, "30d": 30, "all": 3650}.get(period, 0)
    start = today - timedelta(days=days)
    rows: list[dict] = []
    for path in sorted(glob.glob(os.path.join(CALLS_DATA_DIR, "calls-*.jsonl"))):
        name = os.path.basename(path)
        try:
            day = datetime.strptime(name[len("calls-"):-len(".jsonl")], "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < start:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return rows


def _extract_cached_tokens(usage: dict) -> int:
    """兼容 DeepSeek prompt_cache_hit_tokens 与 OpenAI prompt_tokens_details.cached_tokens。"""
    try:
        if not isinstance(usage, dict):
            return 0
        v = usage.get("prompt_cache_hit_tokens")
        if v is not None:
            return int(v)
        details = usage.get("prompt_tokens_details") or {}
        if isinstance(details, dict):
            c = details.get("cached_tokens")
            if c is not None:
                return int(c)
    except (TypeError, ValueError):
        pass
    return 0


def _apply_usage_to_entry(entry: dict, usage: dict, cfg: dict | None) -> None:
    """usage → entry（token/缓存/费用）。缓存命中按 price_cached 计费，未配置则按输入价（保守）。"""
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    cached = _extract_cached_tokens(usage)
    entry["tokens_in"] = tin
    entry["tokens_out"] = tout
    entry["tokens_total"] = int(usage.get("total_tokens") or (tin + tout))
    entry["cached_tokens"] = cached
    entry["cached_savings"] = 0.0
    if cfg is not None:
        p_in = float(cfg.get("price_in") or 0)
        p_out = float(cfg.get("price_out") or 0)
        p_cached = float(cfg.get("price_cached") or 0)
        if p_in or p_out:
            if p_cached <= 0:
                p_cached = p_in
            billable_in = max(0, tin - cached)
            entry["cost"] = round(
                billable_in / 1e6 * p_in + cached / 1e6 * p_cached + tout / 1e6 * p_out, 6
            )
            entry["cost_known"] = True
            if cached and float(cfg.get("price_cached") or 0) > 0:
                entry["cached_savings"] = round(cached / 1e6 * (p_in - p_cached), 6)


@app.middleware("http")
async def log_calls_middleware(request: Request, call_next):
    """记录 /v1/chat/completions 调用：时间/状态/耗时/档位/模型/来源/文本摘要/Token/费用。"""
    if request.url.path != "/v1/chat/completions":
        return await call_next(request)
    body_bytes = b""
    try:
        body_bytes = await request.body()  # Starlette 会缓存到 _body，路由内 request.json() 仍可用
    except Exception:
        pass
    text = _body_text_summary(body_bytes)
    start = time.time()
    entry = {
        "time": time.strftime("%H:%M:%S"),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": 0,
        "elapsed_s": 0.0,
        "tier": "",
        "model": "",
        "source": "",
        "provider": "",
        "degraded": False,
        "decision_id": "",
        "session_key": "",
        "turn_index": 0,
        "text": text,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "cached_tokens": 0,
        "cached_savings": 0.0,
        "cost": 0.0,
        "cost_known": False,  # 模型配置了价格才算得出费用
    }
    request.state.call_entry = entry
    response = await call_next(request)
    elapsed = round(time.time() - start, 3)
    entry["elapsed_s"] = elapsed
    entry["status"] = response.status_code
    entry["tier"] = response.headers.get("X-TokenSaver-Tier", "")
    entry["model"] = response.headers.get("X-TokenSaver-Upstream-Model", "")
    entry["source"] = response.headers.get("X-TokenSaver-Source", "")
    entry["provider"] = response.headers.get("X-TokenSaver-Provider", "")
    entry["degraded"] = response.headers.get("X-TokenSaver-Degraded", "") == "1"
    entry["decision_id"] = response.headers.get("X-TokenSaver-Decision-Id", "")
    entry["session_key"] = response.headers.get("X-TokenSaver-Session-Key", "")
    entry["turn_index"] = int(response.headers.get("X-TokenSaver-Turn-Index", "0") or 0)
    CALL_LOG.appendleft(entry)
    # 流式请求的 usage 在响应体消费时才解析，延后到 gen() 里落盘；非流式这里已完整
    if not (response.headers.get("content-type") or "").startswith("text/event-stream"):
        _persist_call_entry(entry)
    return response
    return response


def _p95(sorted_vals: list[float]) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(len(sorted_vals) * 0.95)))
    return round(sorted_vals[idx], 3)


def _is_high_end_model(model: str) -> bool:
    """粗略判断高档模型（费用贵）：模型名含 pro/think/reason/max/ultra/plus。"""
    m = (model or "").lower()
    return any(k in m for k in ["pro", "think", "reason", "max", "ultra", "plus"])


@app.get("/admin/api/usage")
async def admin_usage(period: str = "today") -> dict:
    """按日期范围聚合费用/调用（持久化 calls-*.jsonl）；实时明细/告警仍取内存最近。"""
    period = (period or "today").strip().lower()
    if period not in ("today", "7d", "30d", "all"):
        period = "today"
    rows = _load_call_rows(period)
    total = len(rows)
    ok = sum(1 for c in rows if c.get("status") == 200)
    elapseds = sorted(float(c.get("elapsed_s") or 0) for c in rows)
    by_model: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_provider: dict[str, int] = {}
    by_model_cost: dict[str, float] = {}
    trend: dict[str, dict] = {}
    total_tokens = 0
    total_cost = 0.0
    total_cached = 0
    cached_savings = 0.0
    for c in rows:
        m = c.get("model") or "?"
        t = c.get("tier") or "?"
        p = c.get("provider") or "?"
        by_model[m] = by_model.get(m, 0) + 1
        by_tier[t] = by_tier.get(t, 0) + 1
        by_provider[p] = by_provider.get(p, 0) + 1
        day = str(c.get("ts") or "")[:10] or "unknown"
        d = trend.setdefault(day, {"calls": 0, "cost": 0.0, "tokens": 0, "cached": 0})
        d["calls"] += 1
        d["cost"] = round(d["cost"] + (c.get("cost") or 0), 6)
        d["tokens"] += int(c.get("tokens_total") or 0)
        d["cached"] += int(c.get("cached_tokens") or 0)
        total_tokens += int(c.get("tokens_total") or 0)
        total_cost += c.get("cost") or 0
        total_cached += int(c.get("cached_tokens") or 0)
        cached_savings += c.get("cached_savings") or 0
        if c.get("cost"):
            by_model_cost[m] = round(by_model_cost.get(m, 0) + c.get("cost", 0), 6)
    cost_known = any(c.get("cost_known") for c in rows)
    # 实时告警/明细仍用内存最近记录
    live = list(CALL_LOG)
    fail_calls = [c for c in live if c["status"] != 200][:10]
    degraded_calls = [c for c in live if c.get("degraded")][:10]
    # 浪费告警：判档简单（c0/c1）却走了高档模型（可能是升级词误触/配置问题）
    waste_calls = [
        c for c in live
        if c["status"] == 200 and c.get("tier") in ("c0", "c1") and _is_high_end_model(c.get("model"))
    ][:10]
    trend_sorted = [{"date": k, **v} for k, v in sorted(trend.items())]
    return {
        "period": period,
        "calls": live[:50],
        "fail_calls": fail_calls,
        "degraded_calls": degraded_calls,
        "waste_calls": waste_calls,
        "stats": {
            "total": total,
            "ok": ok,
            "ok_rate": round(ok / total, 3) if total else 0,
            "avg_elapsed": round(sum(elapseds) / len(elapseds), 3) if elapseds else 0,
            "p95_elapsed": _p95(elapseds),
            "max_elapsed": round(elapseds[-1], 3) if elapseds else 0,
            "by_model": by_model,
            "by_tier": by_tier,
            "by_provider": by_provider,
            "by_model_cost": by_model_cost,
            "fail_count": len(fail_calls),
            "degraded_count": len(degraded_calls),
            "waste_count": len(waste_calls),
            "total_tokens": total_tokens,
            "total_cost": round(total_cost, 6),
            "cost_known": cost_known,
            "total_cached_tokens": total_cached,
            "cached_savings": round(cached_savings, 6),
            "trend": trend_sorted,
        },
    }


@app.get("/v1/models")
async def list_models() -> dict:
    """列出网关支持的模型（含 auto）：模型池 + 默认映射。"""
    pool_names = sorted({m.get("model") for m in load_models_pool() if m.get("model")})
    all_models = ["auto"] + pool_names + sorted(set(TIER_MODEL_MAP.values()))
    seen: set[str] = set()
    data = []
    for m in all_models:
        if m in seen:
            continue
        seen.add(m)
        data.append({"id": m, "object": "model", "owned_by": "tokensaver", "context_window": 1000000})
    return {"object": "list", "data": data}


# ---------- 后台管理 API ----------
def _pool_public(m: dict) -> dict:
    """对外展示模型配置，key 脱敏。"""
    pub = dict(m)
    key = resolve_model_key(m)
    pub["api_key_masked"] = mask_key(key)
    pub["api_key"] = ""  # 不回显明文
    return pub


def _new_model_id() -> str:
    return "m-" + uuid.uuid4().hex[:10]


@app.get("/admin/api/pool")
async def admin_list_pool() -> dict:
    return {"models": [_pool_public(m) for m in load_models_pool()]}


@app.post("/admin/api/pool")
async def admin_add_pool(body: dict) -> dict:
    if not isinstance(body, dict) or not (body.get("name") or "").strip():
        return JSONResponse(status_code=400, content={"error": "name 必填"})
    if not (body.get("base_url") or "").strip():
        return JSONResponse(status_code=400, content={"error": "base_url 必填"})
    if not (body.get("model") or "").strip():
        return JSONResponse(status_code=400, content={"error": "model 必填"})
    tiers = body.get("tiers") or []
    if isinstance(tiers, str):
        tiers = [t.strip() for t in tiers.replace("，", ",").split(",") if t.strip()]
    if not tiers:
        return JSONResponse(status_code=400, content={"error": "tiers 必填（如 c0,c1）"})
    m = {
        "id": _new_model_id(),
        "name": (body.get("name") or "").strip(),
        "base_url": (body.get("base_url") or "").strip(),
        "api_key_file": (body.get("api_key_file") or "").strip(),
        "api_key": (body.get("api_key") or "").strip(),
        "model": (body.get("model") or "").strip(),
        "model_type": (body.get("model_type") or "").strip() or infer_model_type(body.get("model") or ""),
        "context_window": (body.get("context_window") or infer_context_window(body.get("model") or "") or ""),
        "price_in": float(body.get("price_in") or 0) if (body.get("price_in") not in (None, "")) else 0.0,
        "price_out": float(body.get("price_out") or 0) if (body.get("price_out") not in (None, "")) else 0.0,
        "price_cached": float(body.get("price_cached") or 0) if (body.get("price_cached") not in (None, "")) else 0.0,
        "tiers": tiers,
        "priority": int(body.get("priority", 100) or 100),
        "enabled": bool(body.get("enabled", True)),
        "note": (body.get("note") or "").strip(),
        "provider_id": (body.get("provider_id") or "").strip(),
    }
    models = load_models_pool()
    models.append(m)
    save_models_pool(models)
    logger.info("ADMIN add model id=%s name=%s model=%s tiers=%s", m["id"], m["name"], m["model"], tiers)
    return {"ok": True, "model": _pool_public(m)}


@app.put("/admin/api/pool/{model_id}")
async def admin_update_pool(model_id: str, body: dict) -> dict:
    models = load_models_pool()
    for m in models:
        if m.get("id") == model_id:
            for field in ("name", "base_url", "api_key_file", "api_key", "model", "note"):
                if field in body:
                    m[field] = (body.get(field) or "").strip()
            if "price_in" in body:
                m["price_in"] = float(body.get("price_in") or 0) if (body.get("price_in") not in (None, "")) else 0.0
            if "price_out" in body:
                m["price_out"] = float(body.get("price_out") or 0) if (body.get("price_out") not in (None, "")) else 0.0
            if "price_cached" in body:
                m["price_cached"] = float(body.get("price_cached") or 0) if (body.get("price_cached") not in (None, "")) else 0.0
            if "tiers" in body:
                t = body.get("tiers") or []
                if isinstance(t, str):
                    t = [x.strip() for x in t.replace("，", ",").split(",") if x.strip()]
                m["tiers"] = t
            if "priority" in body:
                m["priority"] = int(body.get("priority", 100) or 100)
            if "enabled" in body:
                m["enabled"] = bool(body.get("enabled", True))
            save_models_pool(models)
            logger.info("ADMIN update model id=%s", model_id)
            return {"ok": True, "model": _pool_public(m)}
    return JSONResponse(status_code=404, content={"error": "model not found"})


@app.delete("/admin/api/pool/{model_id}")
async def admin_delete_pool(model_id: str) -> dict:
    models = load_models_pool()
    before = len(models)
    models = [m for m in models if m.get("id") != model_id]
    if len(models) == before:
        return JSONResponse(status_code=404, content={"error": "model not found"})
    save_models_pool(models)
    logger.info("ADMIN delete model id=%s", model_id)
    return {"ok": True}


@app.post("/admin/api/pool/{model_id}/test")
async def admin_test_pool(model_id: str) -> dict:
    """功能验证：按模型类型分派。

    - text/embedding：发短消息「1+1=?」要求直接回答，校验回复 content 非空且含答案「2」
    - image：调用 /v1/images/generations 生一张小图，校验返回 data 非空
    通过/失败写入模型 last_test（按钮变绿/红持久显示）。
    """
    models = load_models_pool()
    target = next((m for m in models if m.get("id") == model_id), None)
    if target is None:
        return JSONResponse(status_code=404, content={"error": "model not found"})
    key = resolve_model_key(target)
    mtype = (target.get("model_type") or "").strip() or infer_model_type(target.get("model") or "")

    started = time.time()
    content = ""
    status = 0
    err_detail = ""

    if mtype == "image":
        # ---- 生图测试 ----
        base = (target.get("base_url") or "").strip().rstrip("/")
        url_candidates = [f"{base}/images/generations"]
        if "/v1" not in base:
            url_candidates.append(f"{base}/v1/images/generations")
        payload = {
            "model": target.get("model"),
            "prompt": "a small red circle on white background",
            "n": 1,
            "size": "256x256",
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10.0, read=90)) as client:
            for url in url_candidates:
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except Exception as exc:
                    err_detail = f"{type(exc).__name__}: {exc}"
                    continue
                status = resp.status_code
                if status != 200:
                    err_detail = resp.text[:200]
                    continue
                try:
                    data = resp.json()
                    items = data.get("data") or []
                    if items:
                        first = items[0]
                        content = str(first.get("url") or (f"b64:{len(first.get('b64_json') or '')}chars" if first.get("b64_json") else ""))
                        break
                    else:
                        err_detail = "HTTP 200 但 data 为空"
                except Exception as exc:
                    err_detail = f"响应非 JSON: {exc}"
        elapsed = round(time.time() - started, 3)
        if status == 200 and content:
            ok = True
            detail = f"生图正常：{content[:60]!r}"
        elif status != 200:
            ok = False
            detail = f"HTTP {status} 不通：{err_detail or '未知错误'}"
        else:
            ok = False
            detail = f"生图失败：{err_detail or '无返回图片'}"
    else:
        # ---- 文本/向量模型：1+1=? 验证 ----
        base = (target.get("base_url") or "").strip().rstrip("/")
        url_candidates = [f"{base}/chat/completions"]
        if "/v1" not in base:
            url_candidates.append(f"{base}/v1/chat/completions")

        payload = {
            "model": target.get("model"),
            "messages": [
                {"role": "system", "content": "你是简单计算器。只输出最终答案数字，不要输出任何解释、思考过程或其他内容。"},
                {"role": "user", "content": "1+1等于几？"},
            ],
            "max_tokens": 200,
            "temperature": 0,
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10.0, read=60)) as client:
            for url in url_candidates:
                try:
                    resp = await client.post(url, json=payload, headers=headers)
                except Exception as exc:
                    err_detail = f"{type(exc).__name__}: {exc}"
                    continue
                status = resp.status_code
                if status != 200:
                    err_detail = resp.text[:200]
                    continue
                try:
                    data = resp.json()
                    content = ((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()
                except Exception as exc:
                    err_detail = f"响应非 JSON: {exc}"
                    content = ""
                if content:
                    break
                err_detail = f"HTTP 200 但无回复内容（{url}）"

        elapsed = round(time.time() - started, 3)

        if status != 200:
            ok = False
            detail = f"HTTP {status} 不通：{err_detail or '未知错误'}"
        elif not content:
            ok = False
            detail = f"HTTP {status} 但无回复内容（可能被思考截断/模型不支持文本）"
        elif "2" not in content:
            ok = False
            detail = f"回复内容异常：{content[:80]!r}（期望 1+1=2）"
        else:
            ok = True
            detail = f"回复正常：{content[:80]!r}"

    # ---- 写回 last_test（按钮持久变色） ----
    target["last_test"] = {
        "ok": ok,
        "status": status,
        "elapsed_s": elapsed,
        "time": time.strftime("%H:%M:%S"),
        "detail": detail,
        "content": content[:100],
        "model_type": mtype,
    }
    save_models_pool(models)

    return {
        "ok": ok,
        "status": status,
        "elapsed_s": elapsed,
        "detail": detail,
        "content": content[:200],
        "model_type": mtype,
    }


@app.get("/admin/api/providers")
async def admin_list_providers() -> dict:
    return {"providers": [provider_public(p) for p in load_providers()]}


@app.post("/admin/api/providers")
async def admin_add_provider(body: dict) -> dict:
    if not isinstance(body, dict) or not (body.get("name") or "").strip():
        return JSONResponse(status_code=400, content={"error": "name 必填"})
    if not (body.get("base_url") or "").strip():
        return JSONResponse(status_code=400, content={"error": "base_url 必填"})
    p = {
        "id": "p-" + uuid.uuid4().hex[:10],
        "name": (body.get("name") or "").strip(),
        "base_url": (body.get("base_url") or "").strip().rstrip("/"),
        "api_key_file": (body.get("api_key_file") or "").strip(),
        "api_key": (body.get("api_key") or "").strip(),
        "note": (body.get("note") or "").strip(),
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    providers = load_providers()
    providers.append(p)
    save_providers(providers)
    logger.info("ADMIN add provider id=%s name=%s base_url=%s", p["id"], p["name"], p["base_url"])
    return {"ok": True, "provider": provider_public(p)}


@app.put("/admin/api/providers/{provider_id}")
async def admin_update_provider(provider_id: str, body: dict) -> dict:
    providers = load_providers()
    for p in providers:
        if p.get("id") == provider_id:
            for field in ("name", "base_url", "api_key_file", "api_key", "note"):
                if field in body:
                    p[field] = (body.get(field) or "").strip()
            save_providers(providers)
            logger.info("ADMIN update provider id=%s", provider_id)
            return {"ok": True, "provider": provider_public(p)}
    return JSONResponse(status_code=404, content={"error": "provider not found"})


@app.delete("/admin/api/providers/{provider_id}")
async def admin_delete_provider(provider_id: str) -> dict:
    providers = load_providers()
    before = len(providers)
    providers = [p for p in providers if p.get("id") != provider_id]
    if len(providers) == before:
        return JSONResponse(status_code=404, content={"error": "provider not found"})
    save_providers(providers)
    # 池内模型解除关联（保留原 base_url/key 快照，不影响路由）
    models = load_models_pool()
    changed = False
    for m in models:
        if m.get("provider_id") == provider_id:
            m["provider_id"] = ""
            changed = True
    if changed:
        save_models_pool(models)
    logger.info("ADMIN delete provider id=%s", provider_id)
    return {"ok": True}


@app.post("/admin/api/discover")
async def admin_discover(body: dict) -> dict:
    """填 base_url + key → 调该服务 /v1/models 拉取模型列表，并自动保存供应商。

    - key 解析：api_key_file 优先（项目内 600 文件），api_key 仅兜底
    - base_url 容错：先试 {base}/models，若失败且不含 /v1 再试 {base}/v1/models
    - 成功后自动保存/复用供应商（同 base_url 复用，否则新建）
    """
    base_url = (body.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        return JSONResponse(status_code=400, content={"error": "base_url 必填"})
    key = ""
    kf = (body.get("api_key_file") or "").strip()
    if kf:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), kf)
        try:
            with open(path, "r", encoding="utf-8") as f:
                key = f.read().strip()
        except Exception:
            pass
    if not key:
        key = (body.get("api_key") or "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    candidates = [f"{base_url}/models"]
    if "/v1" not in base_url:
        candidates.append(f"{base_url}/v1/models")

    last_status = 0
    last_detail = "no candidate tried"
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10.0, read=30)) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
            except Exception as exc:
                last_status = 0
                last_detail = f"{type(exc).__name__}: {exc}"
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception as exc:
                    last_status = 200
                    last_detail = f"响应非 JSON: {exc}"
                    continue  # 200 但非 JSON（可能是 HTML 页），试下一个候选
                raw_models = data.get("data") or []
                out = []
                for m in raw_models:
                    mid = m.get("id") or m.get("model")
                    if mid:
                        ctx = m.get("context_window") or m.get("contextWindow") or infer_context_window(str(mid)) or ""
                        out.append({
                            "id": str(mid),
                            "owned_by": m.get("owned_by") or "",
                            "context_window": ctx,
                            "model_type": infer_model_type(str(mid)),
                        })
                # ---- 自动保存/复用供应商 ----
                # 规范化 base_url：若成功的是 {base}/v1/models（用户填的 base 不带 /v1），
                # 则保存 base + "/v1"，保证 chat 请求走 /v1/chat/completions
                saved_base = base_url
                if "/v1" not in base_url and url.rstrip("/").endswith("/v1/models"):
                    saved_base = base_url.rstrip("/") + "/v1"
                provider_id = ""
                provider = find_provider_by_base_url(saved_base)
                if provider is None:
                    provider = {
                        "id": "p-" + uuid.uuid4().hex[:10],
                        "name": (body.get("provider_name") or "").strip() or _host_of(saved_base),
                        "base_url": saved_base,
                        "api_key_file": kf,
                        "api_key": kf and "" or key,  # 用文件则不留明文
                        "note": "自动发现保存",
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }
                    providers = load_providers()
                    providers.append(provider)
                    save_providers(providers)
                    logger.info("ADMIN discover auto-save provider id=%s name=%s base=%s", provider["id"], provider["name"], saved_base)
                provider_id = provider["id"]
                return {
                    "ok": True,
                    "models": out,
                    "total": len(out),
                    "provider_id": provider_id,
                    "provider_name": provider.get("name", ""),
                    "base_url": saved_base,
                }
            last_status = resp.status_code
            last_detail = resp.text[:300]
    return {"ok": False, "status": last_status, "detail": last_detail}


def _host_of(base_url: str) -> str:
    """从 base_url 提取主机名作为默认供应商名。"""
    from urllib.parse import urlparse
    try:
        host = urlparse(base_url).netloc or base_url
        return host.split(":")[0]
    except Exception:
        return base_url




@app.get("/admin/api/dsh/status")
async def admin_dsh_status() -> dict:
    return _dsh_status_info()


@app.post("/admin/api/dsh/setup")
async def admin_dsh_setup(body: dict) -> dict:
    """一键接入/断开 DSH：action = enable（设 router9/auto 为默认）| disable（还原）。"""
    action = (body or {}).get("action", "enable")
    data = _read_dsh_settings()
    if not data:
        return JSONResponse(status_code=500, content={"error": f"无法读取 DSH 配置：{DSH_SETTINGS}"})

    pi = data.setdefault("llm-pi-ai", {})
    provs = pi.setdefault("providers", {})
    if TOKENSAVER_PROVIDER not in provs:
        provs[TOKENSAVER_PROVIDER] = {
            "api": "openai-completions",
            "apiKeyEnv": "ROUTER9_API_KEY",
            "baseURL": "http://localhost:20130/v1",
            "displayName": "TokenSaver 智能路由",
            "models": [
                {"id": "auto", "contextWindow": 1000000},
                {"id": "deepseek-v4-flash", "contextWindow": 1000000},
                {"id": "deepseek-v4-pro", "contextWindow": 1000000},
                {"id": "deepseek-v4-flash-0731", "contextWindow": 1000000},
            ],
        }

    am = data.setdefault("agent-default-model", {})
    if action == "disable":
        prev = (data.get("_tokensaver_prev_default") or {})
        am["provider"] = prev.get("provider", "deepseek-official")
        am["model"] = prev.get("model", "deepseek-v4-flash-0731")
        data.pop("_tokensaver_prev_default", None)
    else:
        if not data.get("_tokensaver_prev_default"):
            data["_tokensaver_prev_default"] = {"provider": am.get("provider"), "model": am.get("model")}
        am["provider"] = TOKENSAVER_PROVIDER
        am["model"] = TOKENSAVER_MODEL
        am.pop("reasoningEffort", None)

    try:
        _write_dsh_settings(data)
    except Exception as exc:
        return JSONResponse(status_code=500, content={"error": f"写入 DSH 配置失败：{exc}"})

    logger.info("ADMIN dsh setup action=%s -> provider=%s model=%s", action, am.get("provider"), am.get("model"))
    return {"ok": True, "action": action, "status": _dsh_status_info()}


# ---------- 自学习管理（状态/纠错/训练） ----------
SELF_LEARN_MIN_SAMPLES = int(os.getenv("TOKENSAVER_SELFLEARN_MIN_SAMPLES", "200"))
_train_job: dict[str, Any] = {"running": False, "started_at": None, "finished_at": None, "result": None, "error": None}


def _self_learning_config() -> SimpleNamespace:
    """网关侧自学习旋钮：手动触发训练不卡 idle/cooldown，尊重样本量门控。"""
    return SimpleNamespace(
        enabled=True,
        train_min_samples=SELF_LEARN_MIN_SAMPLES,
        idle_hours=0.0,
        cooldown_hours=0.0,
        retention_days=30,
        min_feedback_monitor_samples=5,
        num_boost_round=60,
        learning_rate=0.05,
        train_timeout_seconds=900,
        golden_eval_path=None,
        cost_tolerance_pct=5.0,
        max_critical_under_routing=0.30,
        holdout_min_size=30,
        min_golden_agreement=0.5,
    )


def _train_job_result_payload(result: Any) -> dict:
    return {
        "ran": bool(getattr(result, "ran", False)),
        "reason": str(getattr(result, "reason", "") or ""),
        "version": getattr(result, "version", None),
        "promoted": bool(getattr(result, "promoted", False)),
        "gate_reason": getattr(result, "gate_reason", None),
        "error": getattr(result, "error", None),
    }


def _run_train_job() -> None:
    global _train_job
    try:
        base_dir = _resolve_active_bundle_dir() or str(default_bundle_dir())
        router_cfg = SimpleNamespace(self_learning=_self_learning_config())
        result = maybe_run_update_router(
            SELF_LEARN_AGENT_ID,
            router_cfg=router_cfg,
            base_dir=base_dir,
        )
        _train_job = {
            "running": False,
            "started_at": _train_job.get("started_at"),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": _train_job_result_payload(result),
            "error": None,
        }
        logger.info("SELFLEARN train job done: %s", _train_job["result"])
    except Exception as exc:
        logger.warning("SELFLEARN train job failed: %s", exc)
        _train_job = {
            "running": False,
            "started_at": _train_job.get("started_at"),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "result": None,
            "error": str(exc),
        }


@app.get("/admin/api/selflearning/status")
async def admin_selflearning_status() -> dict:
    stats = scan_event_store(SELF_LEARN_AGENT_ID)
    state = load_train_state(SELF_LEARN_AGENT_ID)
    cfg = _self_learning_config()
    gate = evaluate_training_gates(config=cfg, state=state, stats=stats)
    try:
        fb = scan_feedback_stats(SELF_LEARN_AGENT_ID)
        fb_dict = {"total": fb.total, "up": fb.up, "down": fb.down, "total_single": fb.total_single, "down_single": fb.down_single, "downvote_rate": round(fb.downvote_rate, 4)}
    except Exception:
        fb_dict = {}
    return {
        "agent_id": SELF_LEARN_AGENT_ID,
        "data_root": str(agent_data_dir(SELF_LEARN_AGENT_ID)),
        "kill_switch": self_learning_disabled_by_env(),
        "active_pointer": read_active(),
        "stats": {
            "total": stats.total,
            "high_value": stats.high_value,
            "complaints": stats.complaints,
            "distinct_classes": stats.distinct_classes,
            "last_ts": stats.last_ts,
            "dominant_schema_version": stats.dominant_schema_version,
        },
        "state": state.to_json(),
        "feedback": fb_dict,
        "gate": {
            "should_train": gate.should_train,
            "reason": gate.reason,
            "effective_min_samples": gate.effective_min_samples,
            "detail": gate.stats,
        },
        "train_job": _train_job,
    }


@app.post("/admin/api/selflearning/feedback")
async def admin_selflearning_feedback(body: dict) -> dict:
    rating = str((body or {}).get("rating") or "").strip()
    decision_id = str((body or {}).get("decision_id") or "").strip()
    if rating not in ("up", "down", "neutral"):
        return JSONResponse(status_code=400, content={"error": "rating 必须是 up/down/neutral"})
    if not decision_id:
        return JSONResponse(status_code=400, content={"error": "缺少 decision_id"})
    session_key = str((body or {}).get("session_key") or "default").strip() or "default"
    try:
        turn_index = int((body or {}).get("turn_index") or 0)
    except (TypeError, ValueError):
        turn_index = 0
    try:
        path = write_feedback(
            SELF_LEARN_AGENT_ID,
            decision_id=decision_id,
            session_key=session_key,
            turn_index=turn_index,
            rating=rating,
        )
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    logger.info("SELFLEARN feedback rating=%s decision=%s session=%s turn=%s", rating, decision_id, session_key, turn_index)
    return {"ok": True, "path": str(path)}


@app.post("/admin/api/selflearning/train")
async def admin_selflearning_train() -> dict:
    if self_learning_disabled_by_env():
        return JSONResponse(status_code=400, content={"error": "自学习已被环境变量禁用"})
    if _train_job.get("running"):
        return JSONResponse(status_code=409, content={"error": "训练已在运行中"})
    _train_job.update({
        "running": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "finished_at": None,
        "result": None,
        "error": None,
    })
    threading.Thread(target=_run_train_job, daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/admin/api/status")
async def admin_status() -> dict:
    strategy = await get_strategy()
    models = load_models_pool()
    tier_preview: dict[str, dict] = {}
    for tier in VALID_TIERS:
        name, cfg = pick_model_from_pool(tier)
        tier_preview[tier] = {
            "model": name or TIER_MODEL_MAP.get(tier, TIER_MODEL_MAP[DEFAULT_TIER]),
            "pool": name is not None,
            "provider": (cfg or {}).get("name", "默认"),
        }
    return {
        "router_available": bool(strategy._available),
        "router_version": strategy._model_version,
        "upstream": UPSTREAM_BASE,
        "pool_size": len(models),
        "tier_preview": tier_preview,
        "time": time.time(),
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    return HTMLResponse(ADMIN_HTML)


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TokenSaver 智能路由后台</title>
<style>
:root { --bg:#0f1419; --card:#1a222c; --line:#2a3644; --fg:#e6edf3; --mut:#8b98a5; --acc:#4da3ff; --ok:#3fb950; --err:#f85149; }
* { box-sizing:border-box; }
body { background:var(--bg); color:var(--fg); font:14px/1.6 -apple-system,"PingFang SC",sans-serif; margin:0; padding:24px; }
h1 { font-size:20px; margin:0 0 4px; }
h2 { font-size:15px; margin:24px 0 8px; border-bottom:1px solid var(--line); padding-bottom:6px; }
.card h2 { display:flex; align-items:center; justify-content:space-between; margin-top:0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:16px; margin-bottom:16px; }
.card.collapsed > *:not(h2) { display:none; }
.collapse-btn { background:transparent; border:1px solid var(--line); color:var(--mut); border-radius:6px;
  width:26px; height:24px; cursor:pointer; font-size:14px; line-height:1; flex:none; transition:all .15s; }
.collapse-btn:hover { color:var(--fg); border-color:var(--acc); }
.mut { color:var(--mut); font-size:12px; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th,td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--mut); font-weight:500; }
.tag { display:inline-block; background:#243447; color:#9ecbff; border-radius:4px; padding:1px 6px; margin:1px; font-size:12px; }
.badge { display:inline-block; border-radius:10px; padding:1px 8px; font-size:12px; }
.badge.ok { background:#1b3a24; color:var(--ok); } .badge.off { background:#3a1b1b; color:var(--err); }
button { background:var(--acc); color:#fff; border:0; border-radius:6px; padding:5px 12px; cursor:pointer; font-size:13px; }
button.ghost { background:transparent; border:1px solid var(--line); color:var(--fg); }
button.danger { background:#3a1b1b; color:var(--err); border:1px solid #5c2a2a; }
button.test-ok { background:#1b3a24; color:var(--ok); border:1px solid var(--ok); }
button.test-fail { background:#3a1b1b; color:var(--err); border:1px solid #5c2a2a; }
button:disabled { opacity:.5; cursor:not-allowed; }
button:hover:not(:disabled) { filter:brightness(1.1); }
input,select,textarea { background:#0f1419; border:1px solid var(--line); color:var(--fg); border-radius:6px; padding:6px 8px; font-size:13px; width:100%; }
.form-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:10px; }
.form-grid label { font-size:12px; color:var(--mut); display:block; margin-bottom:3px; }
.row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
#result { white-space:pre-wrap; font-family:ui-monospace,Menlo,monospace; font-size:12px; }
.hidden { display:none; }
.discover-item { display:flex; gap:10px; align-items:center; padding:6px 8px; border-bottom:1px solid var(--line); font-size:13px; }
.discover-item input[type=checkbox] { width:auto; }
.discover-item .tiers { width:110px; flex:none; }
.discover-item .pri { width:70px; flex:none; }
details { margin-top:8px; }
summary { cursor:pointer; color:var(--acc); font-size:13px; }

/* ===== 统计卡 ===== */
.stat-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:16px; }
.stat { background:linear-gradient(160deg,#1d2733,#182029); border:1px solid var(--line); border-radius:12px; padding:14px 16px; text-align:center; }
.stat-num { font-size:26px; font-weight:700; color:var(--fg); letter-spacing:.5px; }
.stat-label { font-size:12px; color:var(--mut); margin-top:2px; }
/* ===== 分布条 ===== */
.dist-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-bottom:16px; }
.dist-title { font-size:12px; color:var(--mut); margin-bottom:6px; }
.dist-row { display:flex; align-items:center; gap:8px; margin-bottom:4px; font-size:12px; }
.dist-name { width:110px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--fg); }
.dist-track { flex:1; height:8px; background:#0f1419; border-radius:4px; overflow:hidden; }
.dist-fill { height:100%; border-radius:4px; transition:width .4s; }
.dist-fill.c-model { background:linear-gradient(90deg,#4da3ff,#7b5cff); }
.dist-fill.c-tier { background:linear-gradient(90deg,#3fb950,#4da3ff); }
.dist-fill.c-provider { background:linear-gradient(90deg,#f0883e,#f85149); }
.dist-fill.c-cost { background:linear-gradient(90deg,#f85149,#ff9f43); }
.dist-val { width:30px; text-align:right; color:var(--mut); }
/* ===== 告警 ===== */
.alert { border-radius:10px; padding:10px 14px; margin-bottom:10px; font-size:13px; display:flex; flex-wrap:wrap; gap:6px 14px; }
.alert-fail { background:#2a1416; border:1px solid #5c2a2a; color:var(--err); }
.alert-warn { background:#2a2410; border:1px solid #5c4a1a; color:#e3b341; }
.alert b { width:100%; }
.alert-item { color:var(--fg); opacity:.85; }
/* ===== tabs ===== */
.tabs { display:flex; gap:8px; margin:12px 0 10px; flex-wrap:wrap; }
.tab { background:transparent; border:1px solid var(--line); color:var(--mut); padding:5px 14px; border-radius:20px; font-size:12px; cursor:pointer; }
.tab.active { background:var(--acc); border-color:var(--acc); color:#fff; }
/* ===== 耗时着色 ===== */
.el-mid { color:#e3b341; font-weight:600; }
.el-slow { color:var(--err); font-weight:700; }
/* ===== 档位 pill（可勾选） ===== */
.tier-pill { display:inline-block; border:1px solid var(--line); background:transparent; color:var(--mut);
  border-radius:12px; padding:1px 8px; margin:1px; font-size:11px; cursor:pointer; transition:all .15s; }
.tier-pill:hover { border-color:var(--acc); color:var(--acc); }
.tier-pill.on { background:var(--acc); border-color:var(--acc); color:#fff; }
</style>
</head>
<body>
<h1>⚡ TokenSaver 智能路由后台</h1>
<div class="mut" id="statusLine">加载状态中…</div>

<div class="card">
  <h2 style="margin-top:0">路由状态</h2>
  <div class="row" id="tierPreview"></div>
</div>

<div class="card" id="dshCard">
  <h2 style="margin-top:0">🤖 接入 DSH（一键设置）</h2>
  <div id="dshStatus" class="mut">检测中…</div>
  <div class="row" style="margin-top:10px">
    <button id="btnDshEnable">设为默认智能路由</button>
    <button id="btnDshDisable" class="ghost">还原默认</button>
    <span class="mut" id="dshHint"></span>
  </div>
</div>

<div class="card" id="slCard">
  <h2 style="margin-top:0">🧠 自学习</h2>
  <div id="slStatus" class="mut">加载中…</div>
  <div class="row" style="margin-top:10px">
    <button id="btnSlTrain">立即训练</button>
    <button id="btnSlRefresh" class="ghost">刷新</button>
    <span class="mut" id="slHint"></span>
  </div>
</div>

<div class="card">
  <h2 style="margin-top:0">快速发现模型</h2>
  <p class="mut">选已有供应商，或填新 Base URL + Key → 点「获取模型」自动拉取模型列表；供应商信息会自动保存，下次直接选。</p>
  <div class="form-grid">
    <div><label>已有供应商（选填）</label><select id="d_provider"><option value="">＋ 新供应商</option></select></div>
    <div><label>供应商名称（新供应商时填，默认=主机名）</label><input id="d_provider_name" placeholder="如：机缘"></div>
    <div><label>Base URL *（含 /v1 或服务根地址）</label><input id="d_base_url" placeholder="https://tokenrhythm.studio/v1"></div>
    <div><label>Key 文件（项目内，优先）</label><input id="d_api_key_file" placeholder="jy_api_key"></div>
    <div><label>或直接填 Key</label><input id="d_api_key" type="password" placeholder="sk-..." autocomplete="off"></div>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="btnDiscover">获取模型</button>
    <button id="btnAddSelected" class="hidden">批量添加所选</button>
    <span class="mut" id="discoverInfo"></span>
  </div>
  <div id="discoverList" class="hidden" style="margin-top:10px"></div>
</div>

<div class="card" id="manualCard">
  <details>
    <summary>手工填写模型（单个添加）</summary>
    <div style="margin-top:10px">
      <div class="form-grid">
        <div><label>供应商（选填，自动带出地址/Key）</label><select id="f_provider"><option value="">＋ 新供应商</option></select></div>
        <div><label>名称 *</label><input id="f_name" placeholder="如：机缘 V4 Flash"></div>
        <div><label>Base URL *</label><input id="f_base_url" placeholder="https://tokenrhythm.studio/v1"></div>
        <div><label>模型名 *（选供应商后自动列出）</label><input id="f_model" list="f_model_list" placeholder="选择或输入模型名"><datalist id="f_model_list"></datalist></div>
        <div><label>Key 文件（项目内，优先）</label><input id="f_api_key_file" placeholder="jy_api_key"></div>
        <div><label>或直接填 Key（存池文件 600）</label><input id="f_api_key" placeholder="sk-..." type="password"></div>
        <div><label>档位（逗号分隔）*</label><input id="f_tiers" placeholder="c0,c1"></div>
        <div><label>价格 入 ¥/M</label><input id="f_price_in" type="number" step="0.01" placeholder="如 1（每百万输入 token）"></div>
        <div><label>价格 出 ¥/M</label><input id="f_price_out" type="number" step="0.01" placeholder="如 2（每百万输出 token）"></div>
        <div><label>价格 缓存 ¥/M</label><input id="f_price_cached" type="number" step="0.01" placeholder="如 0.1（缓存命中输入价，缺省=输入价）"></div>
        <div><label>优先级（小优先）</label><input id="f_priority" type="number" value="100"></div>
        <div><label>备注</label><input id="f_note" placeholder="可选"></div>
      </div>
      <div class="row" style="margin-top:10px">
        <input id="f_enabled" type="checkbox" checked> <label for="f_enabled">启用</label>
        <button id="btnSave">添加模型</button>
        <button id="btnCancel" class="ghost hidden">取消编辑</button>
      </div>
    </div>
  </details>
</div>

<div class="card">
  <h2 style="margin-top:0">📊 实时调用</h2>
  <div class="stat-grid">
    <div class="stat"><div class="stat-num" id="st_total">0</div><div class="stat-label">总调用</div></div>
    <div class="stat"><div class="stat-num" id="st_ok">—</div><div class="stat-label">成功率</div></div>
    <div class="stat"><div class="stat-num" id="st_avg">—</div><div class="stat-label">平均耗时</div></div>
    <div class="stat"><div class="stat-num" id="st_p95">—</div><div class="stat-label">P95 耗时</div></div>
    <div class="stat"><div class="stat-num" id="st_tokens">0</div><div class="stat-label">总 Token</div></div>
    <div class="stat"><div class="stat-num" id="st_cached">0</div><div class="stat-label">缓存命中 Token</div></div>
    <div class="stat"><div class="stat-num" id="st_saved" style="color:var(--ok)">¥0</div><div class="stat-label">缓存已节省</div></div>
    <div class="stat"><div class="stat-num" id="st_cost">¥0</div><div class="stat-label">总费用</div></div>
  </div>
  <div class="tabs" id="rangeTabs">
    <button class="tab active" data-r="today">今天</button>
    <button class="tab" data-r="7d">7天</button>
    <button class="tab" data-r="30d">30天</button>
    <button class="tab" data-r="all">全部</button>
    <span class="mut" id="rangeHint" style="margin-left:10px"></span>
  </div>
  <div class="dist-grid">
    <div><div class="dist-title">按模型</div><div class="dist-bars" id="dist_model"></div></div>
    <div><div class="dist-title">按档位</div><div class="dist-bars" id="dist_tier"></div></div>
    <div><div class="dist-title">按供应商</div><div class="dist-bars" id="dist_provider"></div></div>
    <div><div class="dist-title">按费用</div><div class="dist-bars" id="dist_cost"></div></div>
  </div>
  <div id="alertZone"></div>
  <div class="tabs">
    <button class="tab active" data-f="all">全部</button>
    <button class="tab" data-f="ok">✅ 成功</button>
    <button class="tab" data-f="fail">❌ 失败</button>
    <button class="tab" data-f="degraded">⚠️ 降级</button>
    <button class="tab" data-f="slow">🐢 慢&gt;10s</button>
  </div>
  <table><thead><tr>
    <th>时间</th><th>状态</th><th>耗时</th><th>档位</th><th>供应商</th><th>模型</th><th>Token 入/出</th><th>费用</th><th>请求摘要</th><th>纠错</th>
  </tr></thead><tbody id="usageBody"><tr><td colspan="10" class="mut">暂无调用，发一个请求试试</td></tr></tbody></table>
</div>

<div class="card">
  <h2 style="margin-top:0">模型池</h2>
  <table><thead><tr>
    <th>名称</th><th>模型</th><th>上下文</th><th>Base URL</th><th>Key</th><th>档位</th><th>优先级</th><th>状态</th><th>操作</th>
  </tr></thead><tbody id="poolBody"></tbody></table>
</div>

<div class="card">
  <h2 style="margin-top:0">供应商库</h2>
  <p class="mut">发现模型时自动保存，或在此维护。删除供应商不会影响已添加的模型（保留地址/Key 快照）。</p>
  <table><thead><tr>
    <th>名称</th><th>Base URL</th><th>Key</th><th>备注</th><th>操作</th>
  </tr></thead><tbody id="providerBody"></tbody></table>
</div>

<div class="card hidden" id="resultCard"><h2 style="margin-top:0">测试结果</h2><div id="result"></div></div>

<script>
let EDIT_ID = null;
let DISCOVERED = [];
let CURRENT_PROVIDER_ID = '';
let PROVIDERS = [];
async function api(path, opts={}) {
  const r = await fetch(path, {headers:{'Content-Type':'application/json'}, ...opts});
  const d = await r.json().catch(()=>({}));
  if (!r.ok) throw new Error(d.error || ('HTTP '+r.status));
  return d;
}
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function tiersHtml(t){ return (t||[]).map(x=>'<span class="tag">'+esc(x)+'</span>').join(''); }
const ALL_TIERS = ['c0','c1','c2','c3'];
function tierPills(id, tiers){
  return ALL_TIERS.map(t=>{
    const on = (tiers||[]).includes(t);
    return `<button class="tier-pill ${on?'on':''}" data-model="${esc(id)}" data-tier="${t}"
      onclick="toggleTier('${esc(id)}','${t}',this)">${t}</button>`;
  }).join(' ');
}
async function toggleTier(id, tier, btn){
  btn.disabled = true;
  try {
    const m = poolCache.find(x=>x.id===id);
    const cur = new Set(m.tiers||[]);
    if (cur.has(tier)) cur.delete(tier); else cur.add(tier);
    const next = ALL_TIERS.filter(t=>cur.has(t));
    if (!next.length) { alert('至少保留一个档位'); btn.disabled=false; return; }
    await api('/admin/api/pool/'+id, {method:'PUT', body:JSON.stringify({tiers: next})});
    await refreshPool(); await refreshStatus();
  } catch(e){ alert('档位保存失败: '+e.message); btn.disabled=false; }
}

async function refreshProviders(){
  const d = await api('/admin/api/providers');
  PROVIDERS = d.providers || [];
  const opts = PROVIDERS.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  document.getElementById('d_provider').innerHTML = '<option value="">＋ 新供应商</option>' + opts;
  document.getElementById('f_provider').innerHTML = '<option value="">＋ 新供应商</option>' + opts;
  const tb = document.getElementById('providerBody');
  tb.innerHTML = PROVIDERS.map(p=>`<tr>
    <td><b>${esc(p.name)}</b></td>
    <td style="font-size:12px">${esc(p.base_url)}</td>
    <td>${esc(p.api_key_masked||'(无)')}</td>
    <td class="mut">${esc(p.note||'')}</td>
    <td class="row">
      <button class="ghost" onclick="editProvider('${esc(p.id)}')">编辑</button>
      <button class="danger" onclick="delProvider('${esc(p.id)}')">删除</button>
    </td>
  </tr>`).join('') || '<tr><td colspan="5" class="mut">暂无供应商，发现模型后自动保存</td></tr>';
}
function fillFromProvider(pid, prefix){
  const p = PROVIDERS.find(x=>x.id===pid);
  if (!p) return;
  document.getElementById(prefix+'_base_url').value = p.base_url||'';
  document.getElementById(prefix+'_api_key_file').value = p.api_key_file||'';
  document.getElementById(prefix+'_api_key').value = '';
  const pn = document.getElementById(prefix+'_provider_name');
  if (pn) pn.value = p.name||'';  // 手工表单无此框，跳过
}
async function loadModelList(prefix){
  // 根据表单当前 base_url/key 拉取该供应商模型列表，填充 datalist（可下拉选也可手输）
  const base = document.getElementById(prefix+'_base_url').value.trim();
  if (!base) { document.getElementById(prefix+'_model_list').innerHTML = ''; return; }
  const kf = document.getElementById(prefix+'_api_key_file').value.trim();
  const key = kf ? '' : document.getElementById(prefix+'_api_key').value;
  const dl = document.getElementById(prefix+'_model_list');
  dl.innerHTML = '<option value="">加载中…</option>';
  try {
    const r = await api('/admin/api/discover', {method:'POST', body:JSON.stringify({
      base_url: base, api_key_file: kf, api_key: key,
    })});
    if (r.ok && r.models) {
      dl.innerHTML = r.models.map(m=>
        `<option value="${esc(m.id)}">${esc(m.id)}（${esc(m.model_type||'?')}${m.context_window?' · ctx '+esc(fmtCtx(m.context_window)):''}）</option>`
      ).join('');
    } else {
      dl.innerHTML = '';
    }
  } catch(e){ dl.innerHTML = ''; }
}
document.getElementById('d_provider').onchange = function(){
  if (this.value) fillFromProvider(this.value, 'd');
};
document.getElementById('f_provider').onchange = function(){
  if (this.value) fillFromProvider(this.value, 'f');
  loadModelList('f');
};

async function refreshStatus(){
  try {
    const s = await api('/admin/api/status');
    document.getElementById('statusLine').textContent =
      `路由分类器: ${s.router_available?'✅ 可用':'❌ 不可用'} (v${s.router_version}) · 上游: ${s.upstream} · 池内模型: ${s.pool_size} 个`;
    document.getElementById('tierPreview').innerHTML = Object.entries(s.tier_preview).map(([t,v])=>
      `<span class="tag">${esc(t)}</span> → ${esc(v.model)} <span class="mut">(${esc(v.provider)}${v.pool?'·池':''})</span>`
    ).join(' ');
  } catch(e){ document.getElementById('statusLine').textContent = '状态加载失败: '+e.message; }
}

function fmtCtx(ctx){
  if (!ctx) return '<span class="mut">未知</span>';
  const n = Number(ctx);
  if (n >= 1000000) return (n/1000000).toFixed(n%1000000?1:0)+'M';
  if (n >= 1000) return (n/1000).toFixed(n%1000?1:0)+'K';
  return String(n);
}

async function refreshPool(){
  const d = await api('/admin/api/pool');
  const pname = id => { const p = PROVIDERS.find(x=>x.id===id); return p ? p.name : ''; };
  const tb = document.getElementById('poolBody');
  tb.innerHTML = d.models.map(m=>{
    const lt = m.last_test;
    let testBtn = '<button class="ghost" onclick="testModel(\''+esc(m.id)+'\')">测试</button>';
    if (lt) {
      testBtn = lt.ok
        ? '<button class="test-ok" title="'+esc(lt.detail)+'" onclick="testModel(\''+esc(m.id)+'\')">✓ 通过 '+esc(lt.elapsed_s)+'s</button>'
        : '<button class="test-fail" title="'+esc(lt.detail)+'" onclick="testModel(\''+esc(m.id)+'\')">✗ 失败</button>';
    }
    return `<tr>
    <td><b>${esc(m.name)}</b><br><span class="mut">${esc(m.note||'')}</span></td>
    <td>${esc(m.model)}<br><span class="tag">${m.model_type==='image'?'🖼 生图':(m.model_type==='embedding'?'📊 向量':'💬 文本')}</span></td>
    <td>${fmtCtx(m.context_window)}</td>
    <td style="font-size:12px">${esc(m.base_url)}<br><span class="mut">${pname(m.provider_id)?'供应商: '+esc(pname(m.provider_id)):''}</span></td>
    <td>${esc(m.api_key_masked||'(无)')}</td>
    <td>${tierPills(m.id, m.tiers||[])}</td>
    <td><input type="number" style="width:70px" value="${esc(m.priority)}" min="1"
         title="优先级：数字小优先，输入后回车/失焦保存"
         onchange="updatePriority('${esc(m.id)}', this.value)"></td>
    <td>${m.enabled?'<button class="badge ok" style="cursor:pointer" onclick="toggleEnabled(\''+esc(m.id)+'\')" title="点击切换禁用">启用</button>':'<button class="badge off" style="cursor:pointer" onclick="toggleEnabled(\''+esc(m.id)+'\')" title="点击切换启用">禁用</button>'}</td>
    <td class="row">${testBtn}
      <button class="ghost" onclick="editModel('${esc(m.id)}')" title="编辑完整配置（名称/地址/Key/模型名/档位/备注）">编辑</button>
      <button class="danger" onclick="delModel('${esc(m.id)}')">删除</button>
    </td>
  </tr>`;
  }).join('') || '<tr><td colspan="9" class="mut">池为空，添加模型后自动路由生效</td></tr>';
  poolCache = d.models;  // 保持全局缓存最新（否则新添加的模型在 toggleTier/updatePriority 里找不到）
  return d.models;
}

async function updatePriority(id, val){
  const n = parseInt(val, 10);
  if (isNaN(n) || n < 1) { alert('优先级需为 ≥1 的数字'); await refreshPool(); return; }
  try {
    await api('/admin/api/pool/'+id, {method:'PUT', body:JSON.stringify({priority: n})});
    await refreshStatus();
  } catch(e){ alert('保存优先级失败: '+e.message); await refreshPool(); }
}

async function discover(){
  const btn = document.getElementById('btnDiscover');
  btn.disabled = true; btn.textContent = '获取中…';
  document.getElementById('discoverInfo').textContent = '';
  try {
    const kf = document.getElementById('d_api_key_file').value.trim();
    const r = await api('/admin/api/discover', {method:'POST', body:JSON.stringify({
      base_url: document.getElementById('d_base_url').value,
      api_key_file: kf,
      // 填了 key 文件就用文件，api_key 框忽略（避免浏览器自动填充导致 401）
      api_key: kf ? '' : document.getElementById('d_api_key').value,
      provider_name: document.getElementById('d_provider_name').value,
    })});
    if (!r.ok) {
      const msg = r.status === 401
        ? '未认证：Key 无效或已过期，请检查 Key 文件 / Key 是否正确（或清空 Key 输入框避免自动填充干扰）'
        : (r.detail || '获取失败');
      document.getElementById('discoverInfo').textContent = '❌ ' + msg;
      return;
    }
    DISCOVERED = r.models;
    CURRENT_PROVIDER_ID = r.provider_id || '';
    renderDiscover();
    const saved = r.provider_name ? `（已保存供应商：${r.provider_name}）` : '';
    document.getElementById('discoverInfo').textContent = '✅ 发现 ' + r.total + ' 个模型 ' + saved;
    await refreshProviders();
  } catch(e){ document.getElementById('discoverInfo').textContent = '❌ ' + e.message; }
  finally { btn.disabled = false; btn.textContent = '获取模型'; }
}

function renderDiscover(){
  const box = document.getElementById('discoverList');
  box.classList.remove('hidden');
  document.getElementById('btnAddSelected').classList.remove('hidden');
  box.innerHTML = DISCOVERED.map((m,i)=>`
    <div class="discover-item">
      <input type="checkbox" id="disc_${i}" checked>
      <div style="flex:1"><b>${esc(m.id)}</b> <span class="mut">${esc(m.owned_by||'')}${m.context_window?' · ctx '+esc(m.context_window):''}</span></div>
      <input class="tiers" id="disc_tiers_${i}" placeholder="c0,c1" value="c0,c1" title="档位">
      <input class="pri" id="disc_pri_${i}" type="number" value="100" title="优先级">
    </div>`).join('');
}

async function addSelected(){
  const box = document.getElementById('discoverList');
  const checks = box.querySelectorAll('input[type=checkbox]:checked');
  if (!checks.length) { alert('没有勾选模型'); return; }
  const btn = document.getElementById('btnAddSelected');
  btn.disabled = true; btn.textContent = '添加中…';
  let ok = 0, skip = 0, fail = 0;
  const pool = await api('/admin/api/pool');
  const existing = new Set(pool.models.map(m=>m.model));
  for (const cb of checks) {
    const i = parseInt(cb.id.split('_')[1], 10);
    const m = DISCOVERED[i];
    if (existing.has(m.id)) { skip++; continue; }
    try {
      await api('/admin/api/pool', {method:'POST', body:JSON.stringify({
        name: m.id,
        base_url: document.getElementById('d_base_url').value.trim(),
        api_key_file: document.getElementById('d_api_key_file').value.trim(),
        api_key: document.getElementById('d_api_key').value.trim(),
        model: m.id,
        tiers: document.getElementById('disc_tiers_'+i).value,
        priority: document.getElementById('disc_pri_'+i).value || 100,
        enabled: true,
        note: m.owned_by || '发现添加',
        provider_id: CURRENT_PROVIDER_ID,
      })});
      ok++;
    } catch(e){ fail++; console.error(e); }
  }
  btn.disabled = false; btn.textContent = '批量添加所选';
  document.getElementById('discoverInfo').textContent = `✅ 添加 ${ok}，跳过(已有) ${skip}${fail?'，失败 '+fail:''}`;
  await refreshPool(); await refreshStatus();
}

function formData(){
  return {
    name: document.getElementById('f_name').value,
    base_url: document.getElementById('f_base_url').value,
    model: document.getElementById('f_model').value,
    api_key_file: document.getElementById('f_api_key_file').value,
    api_key: document.getElementById('f_api_key').value,
    tiers: document.getElementById('f_tiers').value,
    priority: document.getElementById('f_priority').value,
    price_in: document.getElementById('f_price_in').value,
    price_out: document.getElementById('f_price_out').value,
    price_cached: document.getElementById('f_price_cached').value,
    enabled: document.getElementById('f_enabled').checked,
    note: document.getElementById('f_note').value,
    provider_id: document.getElementById('f_provider').value,
  };
}
function fillForm(m){
  EDIT_ID = m.id;
  document.getElementById('f_name').value = m.name||'';
  document.getElementById('f_base_url').value = m.base_url||'';
  document.getElementById('f_model').value = m.model||'';
  document.getElementById('f_api_key_file').value = m.api_key_file||'';
  document.getElementById('f_api_key').value = '';
  document.getElementById('f_tiers').value = (m.tiers||[]).join(',');
  document.getElementById('f_priority').value = m.priority||100;
  document.getElementById('f_price_in').value = m.price_in || '';
  document.getElementById('f_price_out').value = m.price_out || '';
  document.getElementById('f_price_cached').value = m.price_cached || '';
  document.getElementById('f_enabled').checked = !!m.enabled;
  document.getElementById('f_note').value = m.note||'';
  document.getElementById('f_provider').value = m.provider_id || '';
  document.getElementById('btnSave').textContent = '保存修改';
  document.getElementById('btnCancel').classList.remove('hidden');
  loadModelList('f');  // 加载该供应商模型列表到下拉
}
async function save(){
  try {
    const d = formData();
    if (EDIT_ID) await api('/admin/api/pool/'+EDIT_ID, {method:'PUT', body:JSON.stringify(d)});
    else await api('/admin/api/pool', {method:'POST', body:JSON.stringify(d)});
    resetForm(); await refreshPool(); await refreshStatus();
  } catch(e){ alert('保存失败: '+e.message); }
}
function resetForm(){
  EDIT_ID = null;
  ['f_name','f_base_url','f_model','f_api_key_file','f_api_key','f_tiers','f_note'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('f_priority').value='100';
  document.getElementById('f_price_in').value='';
  document.getElementById('f_price_out').value='';
  document.getElementById('f_price_cached').value='';
  document.getElementById('f_enabled').checked=true;
  document.getElementById('btnSave').textContent='添加模型';
  document.getElementById('btnCancel').classList.add('hidden');
}
function editModel(id){
  const m = poolCache.find(x=>x.id===id);
  if (!m) { alert('模型信息未加载，请刷新页面'); return; }
  fillForm(m);
  // 展开手工表单并滚动到可见位置（否则用户以为没反应）
  const det = document.querySelector('#manualCard details');
  if (det) det.open = true;
  const card = document.getElementById('manualCard');
  if (card) card.scrollIntoView({behavior:'smooth', block:'start'});
  document.getElementById('btnSave').scrollIntoView({behavior:'smooth', block:'nearest'});
}
async function toggleEnabled(id){
  const m = poolCache.find(x=>x.id===id);
  if (!m) return;
  try {
    await api('/admin/api/pool/'+id, {method:'PUT', body:JSON.stringify({enabled: !m.enabled})});
    await refreshPool(); await refreshStatus();
  } catch(e){ alert('切换失败: '+e.message); }
}
async function delModel(id){
  if(!confirm('删除该模型？')) return;
  try { await api('/admin/api/pool/'+id, {method:'DELETE'}); await refreshPool(); await refreshStatus(); }
  catch(e){ alert('删除失败: '+e.message); }
}
async function testModel(id){
  const card = document.getElementById('resultCard');
  card.classList.remove('hidden');
  document.getElementById('result').textContent = '测试中…（发 1+1=? 验证回复，最长 60s）';
  card.scrollIntoView({behavior:'smooth', block:'nearest'});
  // 找到对应按钮，转测试中状态
  const btn = document.querySelector(`button[onclick="testModel('${id}')"]`);
  if (btn) { btn.disabled = true; btn.textContent = '测试中…'; btn.classList.remove('test-ok','test-fail'); }
  try {
    const r = await api('/admin/api/pool/'+id+'/test', {method:'POST'});
    document.getElementById('result').textContent =
      `${r.ok?'✅ 验证通过':'❌ 验证失败'} · HTTP ${r.status} · ${r.elapsed_s}s\n${r.detail||''}`;
  } catch(e){
    document.getElementById('result').textContent = '测试失败: '+e.message;
  } finally {
    await refreshPool();  // 按钮按 last_test 变绿/红
  }
}
function fmtCost(v){
  if (v == null) return '—';
  let s = Number(v).toFixed(6).replace(/0+$/,'');
  if (s.endsWith('.')) s += '0';
  return '¥'+s;
}
function fmtTokens(n){
  if (!n) return '0';
  if (n >= 1000000) return (n/1000000).toFixed(n%1000000?1:0)+'M';
  if (n >= 1000) return (n/1000).toFixed(n%1000?1:0)+'K';
  return String(n);
}
let USAGE_FILTER = 'all';
let USAGE_RANGE = 'today';
const pnameById = id => { const p = PROVIDERS.find(x=>x.id===id); return p ? p.name : (id||''); };
function elapsedClass(s){
  if (s < 3) return '';
  if (s < 10) return 'el-mid';
  return 'el-slow';
}
function renderDist(elId, dist, color){
  const el = document.getElementById(elId);
  const entries = Object.entries(dist).map(([k,v])=>[pnameById(k), v]).sort((a,b)=>b[1]-a[1]).slice(0,6);
  if (!entries.length){ el.innerHTML = '<span class="mut">暂无</span>'; return; }
  const max = Math.max(...entries.map(e=>e[1]));
  el.innerHTML = entries.map(([k,v])=>`
    <div class="dist-row">
      <span class="dist-name">${esc(k)}</span>
      <div class="dist-track"><div class="dist-fill ${color}" style="width:${(v/max*100).toFixed(1)}%"></div></div>
      <span class="dist-val">${v}</span>
    </div>`).join('');
}
async function refreshUsage(){
  try {
    const u = await api('/admin/api/usage?period='+USAGE_RANGE);
    const s = u.stats;
    document.getElementById('st_total').textContent = s.total;
    document.getElementById('st_ok').textContent = s.total ? (s.ok_rate*100).toFixed(1)+'%' : '—';
    document.getElementById('st_avg').textContent = s.total ? s.avg_elapsed+'s' : '—';
    document.getElementById('st_p95').textContent = s.total ? s.p95_elapsed+'s' : '—';
    document.getElementById('st_tokens').textContent = s.total_tokens ? fmtTokens(s.total_tokens) : '0';
    document.getElementById('st_cached').textContent = s.total_cached_tokens ? fmtTokens(s.total_cached_tokens) : '0';
    document.getElementById('st_saved').textContent = s.cached_savings>0 ? fmtCost(s.cached_savings) : '¥0';
    document.getElementById('st_cost').textContent = s.cost_known ? fmtCost(s.total_cost) : '—';
    document.getElementById('rangeHint').textContent = (s.total_cached_tokens>0 && s.cached_savings<=0) ? '⚠️ 缓存模型未配缓存价，费用按全价计（高估）' : '';
    renderDist('dist_model', s.by_model, 'c-model');
    renderDist('dist_tier', s.by_tier, 'c-tier');
    renderDist('dist_provider', s.by_provider, 'c-provider');
    // 费用分布（金额）
    const costEl = document.getElementById('dist_cost');
    const costEntries = Object.entries(s.by_model_cost||{}).map(([k,v])=>[pnameById(k), v]).sort((a,b)=>b[1]-a[1]).slice(0,6);
    if (!costEntries.length) costEl.innerHTML = '<span class="mut">暂无（模型未配价格）</span>';
    else {
      const maxC = Math.max(...costEntries.map(e=>e[1]));
      costEl.innerHTML = costEntries.map(([k,v])=>`
        <div class="dist-row">
          <span class="dist-name">${esc(k)}</span>
          <div class="dist-track"><div class="dist-fill c-cost" style="width:${(v/maxC*100).toFixed(1)}%"></div></div>
          <span class="dist-val">${fmtCost(v)}</span>
        </div>`).join('');
    }

    // 告警区：失败 + 降级 + 浪费
    const az = document.getElementById('alertZone');
    let alerts = '';
    if (u.fail_calls && u.fail_calls.length) {
      alerts += `<div class="alert alert-fail"><b>❌ 失败 ${u.fail_calls.length} 条</b>${u.fail_calls.map(c=>
        `<span class="alert-item">${esc(c.time)} ${esc(c.model||'?')} HTTP ${c.status} ${esc(c.elapsed_s)}s${c.text?' ·「'+esc(c.text)+'」':''}</span>`).join('')}</div>`;
    }
    if (u.degraded_calls && u.degraded_calls.length) {
      alerts += `<div class="alert alert-warn"><b>⚠️ 降级 ${u.degraded_calls.length} 条</b>${u.degraded_calls.map(c=>
        `<span class="alert-item">${esc(c.time)} ${esc(c.tier||'?')}→${esc(c.model||'?')} ${esc(c.elapsed_s)}s${c.text?' ·「'+esc(c.text)+'」':''}</span>`).join('')}</div>`;
    }
    if (u.waste_calls && u.waste_calls.length) {
      alerts += `<div class="alert alert-warn"><b>💸 简单任务走高档模型 ${u.waste_calls.length} 条（可能升级词误触/配置问题）</b>${u.waste_calls.map(c=>
        `<span class="alert-item">${esc(c.time)} ${esc(c.tier||'?')}→${esc(c.model||'?')} ${fmtCost(c.cost)}${c.text?' ·「'+esc(c.text)+'」':''}</span>`).join('')}</div>`;
    }
    az.innerHTML = alerts;

    // 明细（带过滤）
    let calls = u.calls || [];
    if (USAGE_FILTER === 'ok') calls = calls.filter(c=>c.status===200);
    else if (USAGE_FILTER === 'fail') calls = calls.filter(c=>c.status!==200);
    else if (USAGE_FILTER === 'degraded') calls = calls.filter(c=>c.degraded);
    else if (USAGE_FILTER === 'slow') calls = calls.filter(c=>c.elapsed_s>10);
    const tb = document.getElementById('usageBody');
    tb.innerHTML = calls.map(c=>`<tr>
      <td>${esc(c.time)}</td>
      <td>${c.status==200?'<span class="badge ok">'+esc(c.status)+'</span>':'<span class="badge off">'+esc(c.status)+'</span>'}</td>
      <td class="${elapsedClass(c.elapsed_s)}">${esc(c.elapsed_s)}s${c.degraded?' <span class="tag" title="降级">⚠</span>':''}</td>
      <td>${esc(c.tier||'—')}</td>
      <td class="mut">${esc(pnameById(c.provider))}</td>
      <td>${esc(c.model||'—')}</td>
      <td class="mut">${fmtTokens(c.tokens_in)}/${fmtTokens(c.tokens_out)}</td>
      <td>${c.cost_known?fmtCost(c.cost):'<span class="mut">—</span>'}</td>
      <td class="mut" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(c.text||'')}">${esc(c.text||'—')}</td>
      <td>${c.decision_id?`<span class="fb-btns" data-dec="${esc(c.decision_id)}" data-sess="${esc(c.session_key||'')}" data-turn="${c.turn_index||0}"><button class="fb-up" title="路由正确">👍</button><button class="fb-down" title="路由错误">👎</button><button class="fb-neutral" title="撤销纠错">↩️</button></span>`:'<span class="mut">—</span>'}</td>
    </tr>`).join('') || '<tr><td colspan="10" class="mut">当前筛选无记录</td></tr>';
    tb.querySelectorAll('.fb-btns').forEach(span=>{
      const dec = span.dataset.dec, sess = span.dataset.sess, turn = span.dataset.turn;
      span.querySelector('.fb-up').onclick = ()=>sendFeedback(dec, sess, turn, 'up', span);
      span.querySelector('.fb-down').onclick = ()=>sendFeedback(dec, sess, turn, 'down', span);
      span.querySelector('.fb-neutral').onclick = ()=>sendFeedback(dec, sess, turn, 'neutral', span);
    });
  } catch(e){ /* 静默，下次再刷 */ }
}

async function refreshDshStatus(){
  try {
    const s = await api('/admin/api/dsh/status');
    const el = document.getElementById('dshStatus');
    el.innerHTML = s.enabled
      ? `✅ 已接入：DSH 默认模型 = <b>${esc(s.provider_name)}/auto</b>（智能路由生效中）<br><span class="mut">配置：${esc(s.settings_path)}</span>`
      : `ℹ️ 未接入：DSH 默认模型 = <b>${esc(s.default_provider)}/${esc(s.default_model)}</b>（直连，不走智能路由）<br><span class="mut">router9 provider ${s.provider_configured?'已配置':'未配置'} · ${esc(s.settings_path)}</span>`;
    document.getElementById('dshHint').textContent = '改完后需重启 DSH 生效';
  } catch(e){ document.getElementById('dshStatus').textContent = '状态获取失败：'+e.message; }
}
async function dshSetup(action){
  try {
    const r = await api('/admin/api/dsh/setup', {method:'POST', body:JSON.stringify({action})});
    document.getElementById('dshHint').textContent = (r.ok?'✅ 已保存，请重启 DSH 生效':'❌ 失败') + (r.status&&r.status.enabled?'（当前：智能路由）':'（当前：直连）');
    await refreshDshStatus();
  } catch(e){ alert('设置失败: '+e.message); }
}
async function refreshSlStatus(){
  try {
    const s = await api('/admin/api/selflearning/status');
    const g = s.gate||{}, st = s.stats||{}, stt = s.state||{}, fb = s.feedback||{}, job = s.train_job||{};
    let jobHtml = '';
    if (job.running) jobHtml = '<div class="alert alert-warn"><b>⏳ 训练中…</b> 开始于 '+esc(job.started_at||'')+'（后台进行，网关不受影响）</div>';
    else if (job.error) jobHtml = '<div class="alert alert-fail"><b>❌ 训练失败：</b>'+esc(job.error)+'</div>';
    else if (job.result && job.result.ran) jobHtml = '<div class="alert alert-warn"><b>✅ 训练完成：</b>'+esc(job.result.reason||'')+(job.result.version?' · 版本 '+esc(job.result.version):'')+(job.result.promoted?' · 已上线':'')+'</div>';
    const el = document.getElementById('slStatus');
    el.innerHTML = jobHtml +
      '<div class="stat-grid" style="grid-template-columns:repeat(4,1fr)">'
      +'<div class="stat"><div class="stat-num">'+(st.total||0)+'</div><div class="stat-label">样本数</div></div>'
      +'<div class="stat"><div class="stat-num">'+(st.high_value||0)+'</div><div class="stat-label">高价值</div></div>'
      +'<div class="stat"><div class="stat-num">'+(st.distinct_classes||0)+'</div><div class="stat-label">类别</div></div>'
      +'<div class="stat"><div class="stat-num">'+(fb.down||0)+'/'+(fb.total||0)+'</div><div class="stat-label">纠错↓/总</div></div>'
      +'</div>'
      +'<div class="row mut" style="margin-top:8px">'
      +'<span>门控：'+(g.should_train?'<b class="ok">可训练</b>':'<b>'+esc(g.reason||'')+'</b>')+'（有效阈值 '+(g.effective_min_samples||200)+'）</span>'
      +'<span style="margin-left:14px">上次训练：'+esc(stt.last_train_ts||'从未')+'</span>'
      +'<span style="margin-left:14px">模型版本：'+esc(s.active_pointer||'baseline')+(stt.active_version?'（learned '+esc(stt.active_version)+'）':'')+'</span>'
      +'</div>'
      +'<div class="mut" style="margin-top:6px;font-size:12px">数据目录：'+esc(s.data_root||'')+'</div>';
    document.getElementById('slHint').textContent = s.kill_switch ? '⚠️ 环境变量已禁用自学习' : '';
    document.getElementById('btnSlTrain').disabled = !!job.running || !!s.kill_switch;
  } catch(e){ document.getElementById('slStatus').textContent = '状态获取失败：'+e.message; }
}
async function sendFeedback(decisionId, sessionKey, turnIndex, rating, btnEl){
  try {
    const r = await api('/admin/api/selflearning/feedback', {method:'POST', body:JSON.stringify({decision_id:decisionId, session_key:sessionKey, turn_index:parseInt(turnIndex||0,10), rating})});
    if (btnEl) btnEl.innerHTML = '<span class="tag">✓已记录</span>';
    if (r && r.ok) await refreshUsage();
  } catch(e){ alert('纠错失败: '+e.message); }
}
async function slTrain(){
  try {
    const r = await api('/admin/api/selflearning/train', {method:'POST', body:'{}'});
    document.getElementById('slHint').textContent = r.ok ? '⏳ 已触发训练，请稍候刷新状态' : ('❌ '+(r.error||'触发失败'));
    await refreshSlStatus();
  } catch(e){ alert('触发训练失败: '+e.message); }
}
let poolCache = [];
function editProvider(id){
  const p = PROVIDERS.find(x=>x.id===id);
  if (!p) return;
  // 回填到发现表单，切到编辑模式
  document.getElementById('d_provider').value = id;
  document.getElementById('d_provider_name').value = p.name||'';
  document.getElementById('d_base_url').value = p.base_url||'';
  document.getElementById('d_api_key_file').value = p.api_key_file||'';
  document.getElementById('d_api_key').value = '';
  document.getElementById('discoverList').classList.add('hidden');
  document.getElementById('btnAddSelected').classList.add('hidden');
  document.getElementById('discoverInfo').textContent = '已载入供应商「'+p.name+'」，可修改后重新获取，或删除该供应商';
}
async function delProvider(id){
  if(!confirm('删除该供应商？池内已添加的模型不受影响（保留地址/Key 快照）。')) return;
  try {
    await api('/admin/api/providers/'+id, {method:'DELETE'});
    await refreshProviders(); await refreshPool();
  } catch(e){ alert('删除失败: '+e.message); }
}
document.getElementById('btnSave').onclick = save;
document.getElementById('btnCancel').onclick = resetForm;
document.getElementById('btnDiscover').onclick = discover;
document.getElementById('btnAddSelected').onclick = addSelected;
document.getElementById('btnDshEnable').onclick = () => dshSetup('enable');
document.getElementById('btnDshDisable').onclick = () => dshSetup('disable');
document.getElementById('btnSlTrain').onclick = slTrain;
document.getElementById('btnSlRefresh').onclick = refreshSlStatus;
document.querySelectorAll('.tab').forEach(t=>{
  t.onclick = ()=>{ USAGE_FILTER = t.dataset.f; document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active')); t.classList.add('active'); refreshUsage(); };
});
document.querySelectorAll('#rangeTabs .tab').forEach(t=>{
  t.onclick = ()=>{ USAGE_RANGE = t.dataset.r; document.querySelectorAll('#rangeTabs .tab').forEach(x=>x.classList.remove('active')); t.classList.add('active'); refreshUsage(); };
});
(async function init(){
  // 给每个卡片 h2 注入折叠按钮（记忆状态）
  document.querySelectorAll('.card').forEach(card=>{
    const h2 = card.querySelector('h2');
    if (!h2 || card.querySelector('.collapse-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'collapse-btn';
    btn.textContent = '−';
    btn.title = '折叠 / 展开';
    h2.appendChild(btn);
    const storageKey = 'ts-collapsed-' + (card.querySelector('h2') ? card.querySelector('h2').textContent.trim().slice(0,6) : Math.random().toString(36).slice(2,6));
    const applyState = ()=>{
      const on = card.classList.contains('collapsed');
      btn.textContent = on ? '+' : '−';
    };
    btn.onclick = ()=>{
      card.classList.toggle('collapsed');
      applyState();
      try { localStorage.setItem(storageKey, card.classList.contains('collapsed') ? '1' : '0'); } catch(e){}
    };
    try { if (localStorage.getItem(storageKey) === '1') card.classList.add('collapsed'); } catch(e){}
    applyState();
  });
  try { poolCache = (await api('/admin/api/pool')).models; } catch(e){}
  await refreshProviders(); await refreshPool(); await refreshStatus(); await refreshUsage(); await refreshDshStatus(); await refreshSlStatus();
  setInterval(refreshUsage, 3000);  // 实时调用每 3 秒刷新
  setInterval(refreshSlStatus, 5000);  // 自学习状态每 5 秒刷新
})();
</script>
</body>
</html>"""


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
    if has_new_image(messages):
        # 图片请求：直接路由日日新视觉模型（免费至 2026-08-31），不走复杂度判档
        tier = "img"
        upstream_model, selected_cfg = pick_model_from_pool(tier)
        if upstream_model is None:
            upstream_model = "sensenova-6.8-flash-lite"
            selected_cfg = None
        route_info = {
            "tier": tier,
            "confidence": 1.0,
            "source": "image-vlm",
            "route_class": "IMG",
            "difficulty": 0.0,
            "upstream_model": upstream_model,
        }
        logger.info(
            "ROUTE(image) client_model=%s upstream_model=%s",
            client_model, upstream_model,
        )
    elif client_model.lower() in AUTO_MODEL_TOKENS:
        decision_id = uuid.uuid4().hex
        session_key = str(request.headers.get("x-session-key", "") or "").strip() or "default"
        turn_index = _next_turn_index(session_key)
        llm_tier: str | None = None
        llm_task: dict = {}
        if LLM_CLASSIFIER_ENABLED:
            llm_task = await classify_task(text)
            if llm_task:
                llm_tier = classify_to_tier(llm_task)
                logger.info(
                    "CLASSIFIER llm_tier=%s task_type=%s depth=%s precision=%s risk=%s text=%r",
                    llm_tier, llm_task.get("task_type"), llm_task.get("reasoning_depth"),
                    llm_task.get("precision_req"), llm_task.get("safety_risk"), text[:80],
                )
        if llm_tier is not None:
            # 升级词检测：用户明示要最强/最好模型时，强制走最高档（尊重用户意图）
            tier = _apply_upgrade_hint(text, llm_tier)
            upstream_model, selected_cfg = pick_model_from_pool(tier)
            if upstream_model is None:
                upstream_model = TIER_MODEL_MAP.get(tier, TIER_MODEL_MAP[DEFAULT_TIER])
                selected_cfg = None
            route_info = {
                "tier": tier,
                "confidence": 0.0,
                "source": "llm-classifier",
                "route_class": llm_task.get("task_type"),
                "difficulty": round(float(llm_task.get("reasoning_depth", 0) or 0), 4),
                "upstream_model": upstream_model,
                "task_vector": {
                    "task_type": llm_task.get("task_type"),
                    "reasoning_depth": llm_task.get("reasoning_depth"),
                    "precision_req": llm_task.get("precision_req"),
                    "safety_risk": llm_task.get("safety_risk"),
                },
            }
            logger.info(
                "ROUTE(llm) client_model=%s tier=%s task_type=%s depth=%s upstream_model=%s text=%r",
                client_model, tier, llm_task.get("task_type"),
                llm_task.get("reasoning_depth"), upstream_model, text[:80],
            )
        else:
            strategy = await get_strategy()
            if text:
                tier, confidence, source, extra = await strategy.classify(
                    text, valid_tiers=VALID_TIERS
                )
            else:
                tier, confidence, source, extra = DEFAULT_TIER, 0.0, "empty", {}
            # 升级词检测：用户明示要最强/最好模型时，强制走最高档（尊重用户意图）
            tier = _apply_upgrade_hint(text, tier)
            # 自学习采集：只存特征向量（_train_features），不存原文；best-effort
            _capture_train_sample(
                decision_id=decision_id,
                session_key=session_key,
                tier=tier,
                confidence=float(confidence),
                source=source,
                extra=extra,
                turn_index=turn_index,
            )
            upstream_model, selected_cfg = pick_model_from_pool(tier)
            if upstream_model is None:
                upstream_model = TIER_MODEL_MAP.get(tier, TIER_MODEL_MAP[DEFAULT_TIER])
                selected_cfg = None
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
        route_info["decision_id"] = decision_id
        route_info["session_key"] = session_key
        route_info["turn_index"] = turn_index
    else:
        # 客户端显式指定模型：优先查池（匹配 model 名），否则走全局直连
        upstream_model = client_model
        selected_cfg = None
        for _m in load_models_pool():
            if _m.get("enabled", True) and (_m.get("model") or "") == client_model:
                selected_cfg = _m
                break
        tier = (selected_cfg.get("tiers") or ["c0"])[0] if selected_cfg else "c0"
        route_info = {"mode": "passthrough", "upstream_model": upstream_model, "tier": tier}
        logger.info("ROUTE passthrough model=%s text=%r", upstream_model, text[:80])

    # ---- 构造上游请求（带降级重试链） ----
    payload = dict(body)
    # 文字路由：剥掉历史消息里的图片段，避免文本模型收到 image_url 报错；识图路由保留
    if tier != "img":
        payload["messages"] = strip_image_parts(payload.get("messages") or [])
    # 强制上游返回 usage（流式）：DSH 等客户端常不带 stream_options.include_usage，
    # 不注入的话上游不返回 usage，流式调用就记不到 token/费用。
    if stream:
        payload.setdefault("stream_options", {})["include_usage"] = True

    # 流式一旦开始输出就不能降级（只能透传），故流式只尝试首选模型
    if tier == "img" and selected_cfg is not None:
        # 图片请求：只走视觉模型单链，不降级到文本模型
        upstream_chain = [(
            (selected_cfg.get("base_url") or UPSTREAM_BASE).rstrip("/"),
            resolve_model_key(selected_cfg),
            upstream_model,
        )]
    else:
        upstream_chain = build_upstream_chain(tier, upstream_model, selected_cfg, stream)

    last_err: str | None = None
    last_status: int | None = None
    used_model: str | None = None

    for idx, (base_url, key, model) in enumerate(upstream_chain):
        payload["model"] = model
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        upstream_url = f"{base_url}/chat/completions"
        logger.info(
            "SEND url=%s model=%s key_head=%s key_tail=%s keylen=%d",
            upstream_url, model, key[:4] or "EMPTY", key[-6:] or "EMPTY", len(key),
        )
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
            if idx < len(upstream_chain) - 1:
                logger.warning(
                    "UPSTREAM attempt %d failed model=%s err=%s -> fallback to %s",
                    idx + 1, model, exc, upstream_chain[idx + 1][2],
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
            if 500 <= upstream.status_code < 600 and idx < len(upstream_chain) - 1:
                logger.warning(
                    "UPSTREAM attempt %d HTTP %s model=%s -> fallback to %s",
                    idx + 1, upstream.status_code, model, upstream_chain[idx + 1][2],
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

    # ---- 响应头注入路由信息（不破坏 OpenAI body 兼容） ----
    resp_headers = {
        "X-TokenSaver-Tier": str(route_info.get("tier", "")),
        "X-TokenSaver-Upstream-Model": used_model,
        "X-TokenSaver-Source": str(route_info.get("source", route_info.get("mode", ""))),
        "X-TokenSaver-Decision-Id": str(route_info.get("decision_id", "")),
        "X-TokenSaver-Session-Key": str(route_info.get("session_key", "")),
        "X-TokenSaver-Turn-Index": str(route_info.get("turn_index", 0)),
    }

    # 降级发生时更新路由元数据，如实反映最终实际使用的模型
    if used_model != upstream_model:
        logger.warning(
            "ROUTE_DEGRADED original=%s used=%s tier=%s",
            upstream_model, used_model, route_info.get("tier"),
        )
        route_info["upstream_model"] = used_model
        route_info["degraded_from"] = upstream_model
        resp_headers["X-TokenSaver-Degraded"] = "1"

    # 供应商（实时调用展示）——header 只允许 ASCII，存 provider_id，前端映射回名字
    provider_id = ""
    if selected_cfg is not None:
        provider_id = (selected_cfg.get("provider_id") or "").strip()
    resp_headers["X-TokenSaver-Provider"] = provider_id

    if stream:
        async def gen():
            _usage_done = False
            _persisted = False
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    # 轻量解析 SSE 里的 usage（OpenAI 标准：流末尾 data: {...usage...}）
                    if not _usage_done and chunk:
                        try:
                            text = chunk.decode("utf-8", "replace")
                            for line in text.splitlines():
                                if not line.startswith("data: "):
                                    continue
                                payload = json.loads(line[6:].strip())
                                usage = payload.get("usage") if isinstance(payload, dict) else None
                                if usage:
                                    _entry = getattr(request.state, "call_entry", None)
                                    if _entry is not None:
                                        _apply_usage_to_entry(_entry, usage, selected_cfg)
                                        _persist_call_entry(_entry)
                                        _persisted = True
                                    _usage_done = True
                                    break
                        except Exception:
                            pass
            finally:
                await client.aclose()
                if not _persisted:
                    _entry = getattr(request.state, "call_entry", None)
                    if _entry is not None:
                        _persist_call_entry(_entry)

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

    # 记录 usage（Token 数）+ 按模型价格算费用
    _entry = getattr(request.state, "call_entry", None)
    if _entry is not None:
        usage = data.get("usage") or {}
        _apply_usage_to_entry(_entry, usage, selected_cfg)
        route_info["usage"] = {
            "tokens_in": _entry["tokens_in"],
            "tokens_out": _entry["tokens_out"],
            "tokens_total": _entry["tokens_total"],
            "cached_tokens": _entry["cached_tokens"],
            "cost": _entry["cost"],
            "cost_known": _entry["cost_known"],
        }

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
