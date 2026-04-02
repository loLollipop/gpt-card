import asyncio
import uuid
import time
import json
import os
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
            # 尝试无验证码提交，失败则调用 YesCaptcha API
            try:
                pm_id = create_payment_method(http, pk, req.card.model_dump(), "", session_id, stripe_ver, ctx=init_ctx)
                confirm_payment(http, pk, session_id, pm_id, "", init_resp, stripe_ver, req.captcha_config, ctx=init_ctx, locale_profile=locale_profile)
            except RuntimeError as e:
                if any(kw in str(e).lower() for kw in ["captcha", "hcaptcha"]):
                    captcha_token, captcha_ekey = solve_hcaptcha(req.captcha_config, hcaptcha_cfg)
                    pm_id = create_payment_method(http, pk, req.card.model_dump(), captcha_token, session_id, stripe_ver, ctx=init_ctx)
                    confirm_payment(http, pk, session_id, pm_id, captcha_token, init_resp, stripe_ver, req.captcha_config, captcha_ekey=captcha_ekey, ctx=init_ctx, locale_profile=locale_profile)
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
            
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    # 启动命令: python server.py
    uvicorn.run(app, host="0.0.0.0", port=8888)
