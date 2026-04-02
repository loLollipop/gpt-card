import asyncio
import uuid
import time
import json
import os
import threading
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import requests

# 导入你原有的常量和部分辅助函数（假设整合在同一个文件或模块中）
# 这里省略了 _build_browser_fingerprint, register_fingerprint, init_checkout 等原函数定义
# 请将你 checkout.py 里的核心协议级函数原样粘贴或引入到这里
from checkout import (
    parse_checkout_url,
    init_checkout,
    fetch_elements_session,
    lookup_consumer,
    update_payment_page_address,
    send_telemetry_batch,
    extract_hcaptcha_config,
    create_payment_method,
    confirm_payment,
    poll_result,
    solve_hcaptcha,
    USER_AGENT,
    STRIPE_API,
    STRIPE_VERSION_BASE,
    KNOWN_PUBLISHABLE_KEYS,
    LOCALE_PROFILES
)

app = FastAPI(title="Stripe Protocol Checkout API")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_HANDOFF_TTL_SECONDS = int(os.getenv("MANUAL_HANDOFF_TTL_SECONDS", str(15 * 60)))
APP_HOST = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("APP_PORT", "8888"))
manual_handoff_store: Dict[str, Dict[str, Any]] = {}
manual_handoff_lock = threading.Lock()

# ==========================================
# 1. 定义 API 请求的数据模型 (Pydantic)
# ==========================================
class CardInfo(BaseModel):
    number: str
    exp_month: str
    exp_year: str
    cvc: str
    name: str
    email: str
    address: Dict[str, str]

class ProxyConfig(BaseModel):
    host: str
    port: int
    user: Optional[str] = ""
    password: Optional[str] = ""

class CheckoutRequest(BaseModel):
    checkout_url: str
    card: CardInfo
    publishable_key: Optional[str] = None  # 剥离无头浏览器后，可由客户端主动传入 PK
    proxy: Optional[ProxyConfig] = None
    captcha_token: Optional[str] = ""
    captcha_config: Optional[Dict[str, str]] = None # 例如 {"api_url": "...", "api_key": "..."}
    locale: str = "US"


class ManualHandoffCompleteRequest(BaseModel):
    handoff_id: str
    captcha_token: str
    captcha_ekey: Optional[str] = ""


class ManualHandoffStatusRequest(BaseModel):
    handoff_id: str

# ==========================================
# 2. 改造 publishable_key 获取逻辑 (剔除 Playwright)
# ==========================================
def fetch_publishable_key_pure(session: requests.Session, session_id: str, manual_pk: str = None) -> str:
    """纯协议获取 PK，不再降级调用浏览器"""
    if manual_pk:
        return manual_pk
        
    for acct_id_part, known_pk in KNOWN_PUBLISHABLE_KEYS.items():
        try:
            url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
            post_data = {"key": known_pk, "_stripe_version": STRIPE_VERSION_BASE, "browser_locale": "en-US"}
            test_resp = session.post(url, data=post_data, headers={"User-Agent": USER_AGENT}, timeout=15)
            if test_resp.status_code == 200:
                return known_pk
        except Exception:
            pass

    raise RuntimeError("无法通过纯协议探测到 publishable_key，请在请求参数中主动提供 publishable_key。")


def _cleanup_expired_handoffs() -> None:
    now = int(time.time())
    with manual_handoff_lock:
        expired_keys = [
            handoff_id
            for handoff_id, item in manual_handoff_store.items()
            if now - item.get("created_at", now) > MANUAL_HANDOFF_TTL_SECONDS
        ]
        for handoff_id in expired_keys:
            manual_handoff_store.pop(handoff_id, None)


def _build_manual_handoff(req: CheckoutRequest, session_id: str, hcaptcha_cfg: Dict[str, Any], reason: str) -> Dict[str, Any]:
    _cleanup_expired_handoffs()
    handoff_id = uuid.uuid4().hex
    created_at = int(time.time())
    with manual_handoff_lock:
        manual_handoff_store[handoff_id] = {
            "created_at": created_at,
            "session_id": session_id,
            "checkout_request": req.model_dump(),
            "hcaptcha_config": hcaptcha_cfg,
            "reason": reason,
        }
    return {
        "handoff_id": handoff_id,
        "session_id": session_id,
        "reason": reason,
        "expires_in_seconds": MANUAL_HANDOFF_TTL_SECONDS,
        "manual_verify_url": req.checkout_url,
        "hcaptcha": {
            "site_key": hcaptcha_cfg.get("site_key", ""),
            "rqdata": hcaptcha_cfg.get("rqdata", ""),
        },
    }


def _check_handoff_payment_status(handoff: Dict[str, Any]) -> Dict[str, Any]:
    """
    检查人工接管会话是否已经在 Stripe 页面被人工完成。
    注意：这里只检查 checkout 结果，不依赖 captcha token 回传。
    """
    checkout_payload = handoff.get("checkout_request", {})
    req = CheckoutRequest(**checkout_payload)
    session_id, _ = parse_checkout_url(req.checkout_url)
    http = requests.Session()
    http.headers.update({"User-Agent": USER_AGENT})
    if req.proxy:
        proxy_url = f"http://{req.proxy.user}:{req.proxy.password}@{req.proxy.host}:{req.proxy.port}" if req.proxy.user else f"http://{req.proxy.host}:{req.proxy.port}"
        http.proxies = {"http": proxy_url, "https": proxy_url}

    pk = fetch_publishable_key_pure(http, session_id, manual_pk=req.publishable_key)
    result = poll_result(http, pk, session_id, stripe_ver=STRIPE_VERSION_BASE)
    return result

# ==========================================
# 3. 封装核心执行逻辑 (供线程池调用)
# ==========================================
def run_checkout_sync(req: CheckoutRequest) -> dict:
    """这里整合你原有的 run() 函数的主体流程"""
    session_id, stripe_checkout_url = parse_checkout_url(req.checkout_url)
    
    http = requests.Session()
    http.headers.update({"User-Agent": USER_AGENT})

    # 配置代理
    if req.proxy:
        proxy_url = f"http://{req.proxy.user}:{req.proxy.password}@{req.proxy.host}:{req.proxy.port}" if req.proxy.user else f"http://{req.proxy.host}:{req.proxy.port}"
        http.proxies = {"http": proxy_url, "https": proxy_url}

    locale_profile = LOCALE_PROFILES.get(req.locale, LOCALE_PROFILES["US"])

    try:
        # 1. 纯协议获取 PK [已剔除 Playwright]
        pk = fetch_publishable_key_pure(http, session_id, manual_pk=req.publishable_key)

        # 2. 初始化 Checkout (原脚本逻辑)
        init_resp, stripe_ver, init_ctx = init_checkout(http, session_id, pk, locale_profile=locale_profile)
        init_ctx["page_load_ts"] = int(time.time() * 1000)
        
        # 补全指纹 (原脚本逻辑，需确保 register_fingerprint 等函数存在)
        # reg_guid, reg_muid, reg_sid = register_fingerprint(http)
        # ... 
        
        # 3. 获取 Elements 和消费者信息
        fetch_elements_session(http, pk, session_id, init_ctx, stripe_ver=stripe_ver, locale_profile=locale_profile)
        lookup_consumer(http, pk, req.card.email, stripe_ver=stripe_ver)

        # 4. 提交地址
        update_payment_page_address(http, pk, session_id, req.card.model_dump(), init_ctx, stripe_ver=stripe_ver)

        hcaptcha_cfg = extract_hcaptcha_config(init_resp)

        # 5. 创建支付方式与确认支付
        if req.captcha_token:
            pm_id = create_payment_method(http, pk, req.card.model_dump(), req.captcha_token, session_id, stripe_ver, ctx=init_ctx)
            confirm_payment(http, pk, session_id, pm_id, req.captcha_token, init_resp, stripe_ver, req.captcha_config, ctx=init_ctx, locale_profile=locale_profile)
        else:
            # 尝试无验证码提交，失败则调用 YesCaptcha API；若不可用则切换人工接管
            try:
                pm_id = create_payment_method(http, pk, req.card.model_dump(), "", session_id, stripe_ver, ctx=init_ctx)
                confirm_payment(http, pk, session_id, pm_id, "", init_resp, stripe_ver, req.captcha_config, ctx=init_ctx, locale_profile=locale_profile)
            except RuntimeError as e:
                if any(kw in str(e).lower() for kw in ["captcha", "hcaptcha"]):
                    can_auto_solve = bool(req.captcha_config and req.captcha_config.get("api_key"))
                    if can_auto_solve:
                        try:
                            captcha_token, captcha_ekey = solve_hcaptcha(req.captcha_config, hcaptcha_cfg)
                            pm_id = create_payment_method(http, pk, req.card.model_dump(), captcha_token, session_id, stripe_ver, ctx=init_ctx)
                            confirm_payment(http, pk, session_id, pm_id, captcha_token, init_resp, stripe_ver, req.captcha_config, captcha_ekey=captcha_ekey, ctx=init_ctx, locale_profile=locale_profile)
                        except Exception as solve_err:
                            handoff = _build_manual_handoff(req, session_id, hcaptcha_cfg, f"自动验证码服务失败: {solve_err}")
                            return {"status": "manual_required", "message": "自动 hCaptcha 失败，已切换人工接管。", "handoff": handoff}
                    else:
                        handoff = _build_manual_handoff(req, session_id, hcaptcha_cfg, "缺少 captcha_token 且未配置自动验证码服务")
                        return {"status": "manual_required", "message": "需要人工完成验证码后继续。", "handoff": handoff}
                else:
                    raise

        # 6. 轮询结果
        result = poll_result(http, pk, session_id, stripe_ver)
        return {"status": "success", "data": result}

    except Exception as e:
        return {"status": "error", "message": str(e)}

# ==========================================
# 4. Web 接口定义
# ==========================================
@app.get("/")
async def ui_home():
    return FileResponse(os.path.join(BASE_DIR, "ui.html"))


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ts": int(time.time())}


@app.post("/api/v1/checkout")
async def process_checkout_endpoint(request: CheckoutRequest):
    """
    处理 Checkout 支付请求。
    使用 asyncio.to_thread 避免 requests 和 time.sleep() 阻塞整个 Web 服务。
    """
    try:
        # 将同步的 HTTP 请求和休眠操作放入后台线程池执行
        result = await asyncio.to_thread(run_checkout_sync, request)
        
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        if result["status"] == "manual_required":
            return result
            
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/checkout/manual-handoff/complete")
async def complete_manual_handoff(request: ManualHandoffCompleteRequest):
    """人工完成 hCaptcha 后，回传 token 给服务端继续支付流程。"""
    _cleanup_expired_handoffs()
    with manual_handoff_lock:
        handoff = manual_handoff_store.get(request.handoff_id)
    if not handoff:
        raise HTTPException(status_code=404, detail="handoff_id 不存在或已过期，请重新发起 checkout")
    if not request.captcha_token.strip():
        raise HTTPException(status_code=400, detail="captcha_token 不能为空")

    checkout_payload = dict(handoff["checkout_request"])
    checkout_payload["captcha_token"] = request.captcha_token.strip()
    req = CheckoutRequest(**checkout_payload)
    with manual_handoff_lock:
        manual_handoff_store.pop(request.handoff_id, None)

    result = await asyncio.to_thread(run_checkout_sync, req)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.post("/api/v1/checkout/manual-handoff/status")
async def check_manual_handoff_status(request: ManualHandoffStatusRequest):
    """
    检查人工接管状态：
    - completed: 人工已在 Stripe 页面完成支付/认证
    - pending: 尚未完成，继续等待或使用 token 回传
    """
    _cleanup_expired_handoffs()
    with manual_handoff_lock:
        handoff = manual_handoff_store.get(request.handoff_id)
    if not handoff:
        raise HTTPException(status_code=404, detail="handoff_id 不存在或已过期，请重新发起 checkout")

    try:
        result = await asyncio.to_thread(_check_handoff_payment_status, handoff)
        # poll_result 返回结构可能因页面状态不同而变化，这里尽量宽松判断
        if result:
            status_text = json.dumps(result, ensure_ascii=False).lower()
            success_keywords = ["succeeded", "complete", "paid", "success"]
            if any(keyword in status_text for keyword in success_keywords):
                with manual_handoff_lock:
                    manual_handoff_store.pop(request.handoff_id, None)
                return {"status": "completed", "data": result}
        return {"status": "pending", "message": "尚未检测到人工完成结果，请继续认证或稍后重试。"}
    except Exception as e:
        return {"status": "pending", "message": f"暂未完成或检测失败: {e}"}

if __name__ == "__main__":
    import uvicorn
    # 启动命令: python server.py
    uvicorn.run(app, host=APP_HOST, port=APP_PORT)
