# gpt-card

## 人工接管 hCaptcha（新增）

当请求没有 `captcha_token` 且自动解题不可用/失败时，接口会返回：

- `status: "manual_required"`
- `handoff.handoff_id`
- `handoff.manual_verify_url`（可直接打开进行人工验证）
- 当前会话的 hCaptcha 参数（`site_key` / `rqdata`）

人工完成验证码后，把 token 回传到：

`POST /api/v1/checkout/manual-handoff/complete`

请求体示例：

```json
{
  "handoff_id": "xxx",
  "captcha_token": "xxx"
}
```

也可以不手动回传 token，直接让服务端检测人工是否已在 Stripe 页面完成：

`POST /api/v1/checkout/manual-handoff/status`

```json
{
  "handoff_id": "xxx"
}
```

> 说明：点击 `manual_verify_url` 后通常会进入该 Checkout 会话页面，但是否需要补填信息由 Stripe 会话状态决定（不是本项目可完全控制）。
