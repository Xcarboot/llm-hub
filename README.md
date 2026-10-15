# LLM Gateway MVP

面向中国大陆 B2B 场景的 OpenAI 兼容大模型 API 治理网关。使用 FastAPI、SQLAlchemy 2 async、PostgreSQL、Redis、LiteLLM；支持一个主模型和一个备用模型、管理员开户及人工充值，现已包含管理后台与客户自助门户。客户可激活账户、登录、创建及撤销 API Key、查看余额与用量，并在开发测试环境体验本机模拟充值和退款审批；真实支付宝支付尚未接通。

## 启动

要求 Python 3.12，或 Docker Compose。以下初始化流程仅用于全新数据库；已有数据库必须使用下文的增量升级流程，禁止重复执行 `gateway-admin bootstrap`。

```bash
cp .env.example .env
# 注意：原样复制后应用必定拒绝启动。必须分别生成并填写两个不同的随机值：
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # ADMIN_TOKEN
python -c 'import secrets; print(secrets.token_urlsafe(48))'  # API_KEY_HMAC_SECRET
# 同时替换所有其他 REPLACE_*：设置 POSTGRES_PASSWORD、供应商 API_BASE/API_KEY，
# 并在 litellm_config.yaml 中填写 primary/fallback 对应的真实 model。
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose run --rm api gateway-admin bootstrap
docker compose up -d api
```

本地开发：

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
alembic upgrade head
gateway-admin bootstrap
gateway-admin recharge <tenant-id> 100000000 initial-credit
uvicorn app.main:app --reload
```

`bootstrap` 只执行一次，建立示例租户、`enterprise-chat` 主/备路由、不可变价格版本、账本账户，并仅在终端显示一次虚拟 `gw_...` Key。数据库路由保存 `primary` / `fallback` 别名；应用从 `LITELLM_CONFIG_PATH`（默认 `litellm_config.yaml`）加载别名对应的 `model`、`api_base`、`api_key`，再将 YAML 中的 `os.environ/VAR` 从环境变量解析后调用 LiteLLM。请求不能覆盖这些配置参数；缺失环境变量、占位值、未知或重复别名及错误结构都会 fail-closed，且 YAML 不应写入真实密钥。

## 调用

```bash
curl http://localhost:8000/v1/models \
  -H 'Authorization: Bearer gw_REPLACE_WITH_BOOTSTRAP_OUTPUT'

curl http://localhost:8000/v1/chat/completions \
  -H 'Authorization: Bearer gw_REPLACE_WITH_BOOTSTRAP_OUTPUT' \
  -H 'Idempotency-Key: order-20250101-0001' \
  -H 'Content-Type: application/json' \
  -d '{"model":"enterprise-chat","messages":[{"role":"user","content":"你好"}],"max_tokens":256,"stream":false}'
```

管理接口位于 `/admin/*`，必须发送 `X-Admin-Token`。健康检查为 `/health/live` 和 `/health/ready`。

## 管理后台与已有数据库升级

React 管理后台使用 Vite、Material UI、SWR，包含运行总览、租户和一次性 API Key、模型及价格版本、人工充值、账本、请求记录与预占记录，并新增客户门户开户、充值订单与退款审批页面。数据直接读取现有 PostgreSQL 账本，不使用演示数据；本机模拟充值同样会写入这份账本，禁止用生产数据库进行模拟测试。

已有数据库应先备份并在隔离环境演练，安排停止写入后执行增量迁移，禁止重复 bootstrap。`alembic upgrade head` 会按当前数据库版本依次应用已有迁移，包含 `0003_commerce_funds`（版本 `0003`）和 `0004_sealed_ledger`（版本 `0004`），不应手工跳过或仅修改版本标记：

```bash
docker compose build api web
docker compose run --rm api alembic upgrade head
docker compose up -d api web
```

`0003` 新增客户用户、一次性激活令牌、会话、支付订单、退款申请、资金来源与分配等表，以及 `ProviderConfig` 预留表结构；迁移先检查历史余额与调用冻结、预占记录是否一致，不重写旧账。检查失败时须先对账，不能清空记录或重复 bootstrap 绕过。历史资金来源与退款账户由服务按需建立；历史余额及人工充值不会因此变成可在线退款的支付订单。财务迁移 `0003` 不支持降级。`0004` 在 PostgreSQL 中将账本分录限定为只能在创建其转账记录的同一事务内追加，迁移提交后历史转账也封账；原不可变账本及平衡校验仍保留。

打开 `http://localhost:3000`（管理员登录页为 `/login`），使用自行配置的 `ADMIN_TOKEN` 登录。令牌与查询缓存只存在当前页面内存；刷新页面、退出或会话失效后重新登录。不要把管理员令牌放入 URL、截图或构建环境变量。API Key 创建后只显示一次，关闭对话框会清空明文。

前端通过 Nginx 的 `/api/` 同源代理访问后端，无需启用 CORS；例如浏览器调用 `/api/portal/login` 会代理到后端 `/portal/login`。管理界面默认仅绑定 `127.0.0.1:3000`，不直接暴露公网；Compose 的后端 `8000` 端口并非仅绑定回环地址，生产必须另行限制访问，不能因此假定管理员 API 已隔离。金额以 micro-CNY 整数计算和无损解析，显示时保留六位小数。

前端本地开发和验证：

```bash
npm --prefix frontend ci
npm --prefix frontend run dev
npm --prefix frontend test -- --run
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

## 客户门户开户、登录与自助功能

客户登录入口为 `http://localhost:3000/portal/login`，与管理员 `/login` 独立。管理员先在 `/tenants` 或“客户门户开户” `/customers` 创建或选择有效租户，打开租户详情并点击“开通客户门户”，填写客户邮箱，生成一次性激活链接。对应后端接口是 `POST /admin/tenants/{tenant_id}/customer-activation`，使用 `X-Admin-Token` 鉴权，请求体为邮箱；不会自动发送邮件。每个租户当前只有一个客户账户，邮箱须唯一，已激活账户不能通过此入口重复激活或重设密码。

没有默认客户密码，bootstrap 也不会创建客户登录密码。激活链接指向 `/portal/activate#token=...`，默认有效期为 `ACTIVATION_TTL_SECONDS=86400` 秒，成功激活后只能使用一次；未激活账户重新生成链接会使旧链接失效。管理员对话框仅展示一次链接，关闭后清空，须通过安全渠道交付，勿放入日志、截图、公共群或项目文件。客户自行设置至少 12 位密码，后端使用 Argon2id 保存密码哈希；前端读取激活片段后立即从地址栏移除，只保存在当前页面内存，刷新未完成的激活页面时需重新打开尚未失效的原链接。

后端 `POST /portal/activate` 激活后直接建立会话，`POST /portal/login` 使用邮箱和自设密码登录，`GET /portal/session` 恢复有效会话，`POST /portal/logout` 撤销会话并清除 Cookie。会话 Cookie 为 `portal_session`，使用 HttpOnly、SameSite=Lax、Path=/，默认有效期 `CUSTOMER_SESSION_TTL_SECONDS=3600` 秒；前端不把密码、会话凭据或 CSRF 令牌持久化到浏览器存储。客户账户或租户被停用、会话过期或撤销后，访问会被拒绝；客户身份由服务端会话决定，不能通过请求参数切换租户。

客户首页 `/portal`（也可访问 `/portal/home`）显示可用余额、调用冻结、退款冻结及账本；`/portal/keys` 可创建和撤销自己的 Key，明文仅创建时返回一次；`/portal/usage` 查看请求与用量；`/portal/docs` 提供接入说明。后端对应 `GET /portal/balance`、`GET /portal/ledger/transfers`、`GET /portal/requests`、`GET /portal/models`，以及 `GET/POST /portal/keys`、`POST /portal/keys/{key_id}/revoke`。客户门户会话不能代替网关 Bearer API Key，也不使用管理员令牌。

登录、激活以及所有已登录客户写操作都校验 `Origin` 是否精确匹配 `CUSTOMER_ALLOWED_ORIGINS`；缺失或不匹配返回 403。已登录写操作还必须携带会话接口返回的 `X-CSRF-Token`，前端会自动附带。`CUSTOMER_ALLOWED_ORIGINS` 为逗号分隔的完整来源（协议、主机及端口，无路径或末尾斜线），`localhost` 与 `127.0.0.1` 是不同来源，首项同时用于生成激活链接，应设置成客户实际访问的门户来源。门户应保持前后端同源代理，Vite 开发服务器也通过 `/api` 代理本机 `8000`；修改 `CORS_ORIGINS` 不能替代门户 Origin 校验或开启跨站 Cookie 支持。

`.env.example` 默认保留 `CUSTOMER_COOKIE_SECURE=true`。若仅在本机使用明文 HTTP 测试门户，可在自己的本地配置中设置 `APP_ENV=development`（或 `test`）、`CUSTOMER_COOKIE_SECURE=false`，并让 `CUSTOMER_ALLOWED_ORIGINS` 与实际 `http://localhost:3000` 或 `http://127.0.0.1:3000` 一致；不要依赖浏览器对 localhost 的 Secure Cookie 特例。配置变更后需重启或重新创建后端容器。生产及其他非开发测试环境必须使用 TLS 与 `CUSTOMER_COOKIE_SECURE=true`、设置真实 HTTPS Origin 和正确的 `TRUSTED_HOSTS`，由受信任代理保留正确的 Origin；应用在非 development/test 环境关闭 Secure Cookie 时会拒绝启动，不能将生产伪装成开发环境绕过。

## 充值订单与退款审批的模拟边界

默认 `PAYMENT_MODE=disabled`，客户 `/portal/payments` 与 `/portal/refunds` 可查看记录，但不能进行模拟充值或批准退款执行。只有同时满足 `APP_ENV=development` 或 `test` 且 `PAYMENT_MODE=mock`，本机模拟资金流程才会启用；其他环境设置 mock 会拒绝启动。模拟必须使用隔离测试数据库，不收取或退回真实资金，不跳转支付宝，也不存在真实支付页面、支付通知验签、查单或退款网络调用。单次充值及退款申请均要求正数整分金额，最低 0.01 元、最高 1000 元（`1000000000` micro-CNY），这只是测试限制，不是支付宝限额。

客户创建订单调用 `POST /portal/payments`，可通过 `GET /portal/payments` 和 `GET /portal/payments/{order_id}` 查询；`GET /portal/payments/config` 返回启用状态与明确的模式提示。创建订单仅得到 `PENDING` 状态，不代表到账；点击“模拟完成支付”才调用 `POST /portal/payments/{order_id}/mock-confirm`，由后端原子记账并变为 `PAID`。充值与退款申请的请求体使用字符串 `amount_micro_cny` 与 `idempotency_key`，退款还需 `payment_order_id` 和 `reason`；重试同一意图应复用原幂等键，不同内容复用同一键返回 409，重复确认不会重复入账。

客户通过 `POST /portal/refunds` 对本人已支付订单尚未消耗、未冻结且可退的整分资金申请退款，通过 `GET /portal/refunds` 和 `GET /portal/refunds/{refund_id}` 跟踪。申请先将相应余额转入退款冻结并进入 `PENDING_APPROVAL`，提交不代表退款成功；不能把其他订单、历史余额或人工充值当作本订单可退额度。管理员在 `/payments` 查看充值订单，在 `/refunds` 核对租户、原订单、金额及原因后填写审批说明，分别调用 `POST /admin/refunds/{refund_id}/approve` 或 `/reject`；订单与申请列表对应 `GET /admin/payments` 和 `GET /admin/refunds`。mock 批准仅在本机完成退款记账，拒绝会释放该申请的冻结金额；处理中的或结果未知的申请不能当作成功，也不能通过拒绝强行解冻。

`PAYMENT_MODE=alipay_sandbox` 只是预留模式，当前明确提示“支付宝沙箱尚未接通”：有效的创建支付订单或退款执行请求会返回 HTTP 503，前端禁用不可用操作，不模拟成功。若数据库已有沙箱退款申请，批准失败后仍保留冻结，不宣称已退款。真实支付宝、沙箱交易、回调验签、自动对账与支付重试任务均尚未实现；配置 `ALIPAY_*` 不会接通支付。

## 供应商配置现状

`ProviderConfig` 当前仅有表结构及输入模型预留，尚无供应商密钥配置页面、管理读写路由或运行时接入。`PROVIDER_ENCRYPTION_KEY`、`PROVIDER_ALLOWED_HOST_SUFFIXES` 也只是预留设置，不代表已完成密钥加密管理或出站域名安全校验。实际网关仍使用原有 `litellm_config.yaml` 的主/备别名与 `PRIMARY_PROVIDER_*`、`FALLBACK_PROVIDER_*` 环境变量配置；不要把真实供应商密钥写入 README、YAML、前端构建变量或版本库。

## 计费与失败语义

金额均为整数微元；PostgreSQL 追加式双重记账是唯一余额真相，Redis 只做限流。

上游前将完整 `messages` 紧凑序列化，按 UTF-8 字节数加每条消息及固定协议开销，再乘安全系数并向上取整；这是不除以 4 的偏保守 byte-level token 上界。输出估算仍严格以请求 `max_tokens` 为上限，再按主/备路由最高绑定价格预占。

预占事务提交后才调用上游；调用期间不持有数据库事务。只允许在首个响应块之前切换备用模型；一旦有输出绝不重试。

有完整 usage 时按该路由在预占时绑定的不可变价格版本结算并退回差额。若供应商意外报告的成本超过预占，则不再调用任何上游，保守结算全部预占，保存 usage/overage 标记并返回可解释的计量异常。

**保守结算为全部预占上限**：usage 缺失、连接错误等上游结果是否计费不确定、流式连接取消、首字节后异常。只有 `AuthenticationError`、`BadRequestError`、`NotFoundError` 或显式 `no_charge=True` 才视为明确未计费并允许首字节前 fallback/释放。

流式请求在标记上游已派发后、创建上游 stream 前立即启动心跳，并每 `CONCURRENCY_LEASE_SECONDS/3` 通过原子 Lua 续租，直至响应消费进入清理；创建 stream 和等待首包都受 `REQUEST_TIMEOUT_SECONDS` 限制。首包超时、usage 结构异常、续租失败、首字节后异常或客户端取消都停止 fallback、尝试 `aclose` 上游并按预占上限保守结算。`prepare_stream` 返回拥有清理职责的生命周期包装器；受控响应在 ASGI 响应头或正文发送失败时都会显式调用该包装器的 `aclose`，因此即使事件生成器尚未首次迭代，也会由独立任务完成上游关闭、心跳停止、保守结算及 lease 释放，并保证只执行一次。非流式请求仍仅使用请求超时（该值必须小于并发租约时长），不启动心跳。

超时任务只能释放仍为 `RESERVED` 的记录，不能自动释放 `DISPATCHED`。冲正只追加反向分录。

非流式完成结果可按同一 `Idempotency-Key` 重放；不同 payload 或处理中请求返回 409。流式不重放，但键会阻止重复扣费。

## 检查

```bash
ruff check .
mypy app
pytest
# PostgreSQL/Redis 集成用例另以 integration 标记运行
```

## 安全边界与非生产事项

API Key 只保存前缀和 HMAC-SHA256，明文只返回一次；日志会脱敏 Authorization、API Key、messages/prompt/content；CORS 默认关闭，TrustedHost、请求体、消息字符数及输出 token 均有限制。Redis 故障限流 fail-closed，数据库预调用阶段异常不会访问上游。所有环境都会拒绝长度小于 32 或含 `REPLACE`、`CHANGE_ME`、`EXAMPLE` 等占位语义的 `ADMIN_TOKEN` / `API_KEY_HMAC_SECRET`，这两个秘密应分别随机生成、仅在部署环境保存。

客户登录与激活按客户端地址和账户身份限流，Redis 不可用时返回 503 而非绕过验证；门户及管理 API 响应使用 `Cache-Control: no-store`。客户密码、激活令牌、Cookie、CSRF 令牌和一次性 Key 都属于敏感凭据，不应写入日志、公共渠道或持久化查询缓存；管理员与客户的认证和前端缓存相互独立。

这是包含管理后台与客户自助门户的 MVP，不包含公开注册、自助密码重置、真实在线支付（支付宝沙箱也未接通）、供应商密钥配置页面、自动清结算或正式 SLA。本机模拟资金流程不代表收付款能力，不可面向客户收取真实资金。生产部署仍需真实密钥管理/KMS、TLS 与网关 WAF、管理员网络隔离、正确的门户 Origin 与 Secure Cookie、PostgreSQL 备份与监控、迁移演练、供应商合规评估、审计留存策略、告警和容量压测；真实支付接入及验签、退款查询和对账还需另行实现验证。不得直接使用示例占位秘密或模型名。
