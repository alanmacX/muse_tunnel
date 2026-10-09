# Muse real data tunnel

部署：`tx:/home/ubuntu/muse-tunnel`；公开 HTTPS origin：`https://hajiqu.xyz`。
API 原生 systemd 运行于 127.0.0.1:18767；systemd socket proxy 在 loopback:80 接入现有 Cloudflare 路由。没有公网管理界面或解密密钥接口。`/health` 仅返回存活状态；所有 `/v1/*` 含 OpenAPI 都需专用 Bearer token。授权消费端能读取信息、提交回执，不能执行命令或写入来源消息。

## 数据与可靠性

- 学习通收件箱和钉钉每分钟采集；课程/作业每 15 分钟刷新；正方课表和考试每 30 分钟刷新。归档无重要性筛选，原始字段和解析结果一同保存。`kind` 区分消息、课程、作业、课表、考试和来源状态，不表示优先级。
- SQLite WAL + synchronous=FULL；按来源/稳定来源 ID/修订入账，顺序 seq、payload SHA-256、幂等回执。未确认事件不限五分钟，始终可重读；快照分页不会因分页期间的新事件跳项。
- Muse 每次从 0 扫描 pending，固定 upper_seq 翻到最后一页；至少一次投递，需要本地幂等。服务端回执只能证明消费端自报成功，不能证明模型理解或通知已被用户看到。
- 每次采集后刷新每日完整 manifest。消息的来源日期和观察日期都纳入对账，迟到/修订使 daily revision 增长，旧 audit 失效；摘要计数不能替代逐项核验。
- 每日 03:10 私有 SQLite 在线备份，保留 14 天。初次迁移保留独立私有回滚归档。备份与服务器同机，不构成完整异地容灾；机器或磁盘永久丢失仍可能损失最近数据。

## 当前来源覆盖与边界

学习通：验证登录后读取全课程列表、全部可见作业（含旧学期/已完成）和收件箱通知。通知按照网站 lastValue 游标分页，每天首次全量扫描，此后读到已见锚点；只在完整扫描结束后提交锚点。用户明确排除 Web IM，components.im.enabled=false、reason=excluded_by_user，不再请求旧网页入口或为该排除项报警。若课程 hasMore=true、响应格式变更、分页不前进、任一课程抓取失败，都显式暴露采集问题。

钉钉：保留并 apt hold `com.alibabainc.dingtalk 8.1.0.6021101`。客户端/Xvfb 持续运行，复用原登录态与本机解密密钥。每轮解密并扫描全部本地消息表，没有仅凭时间戳丢弃迟到消息的逻辑；数据库/WAL 采集中变化会重试，仍变化则标记失败。available_local_cache 只代表本地缓存可读；缓存不能证明云端完整历史或实时同步正常，额外检查客户端远端 TCP 连接，无连接或检查失败标为 degraded；TCP 连接仍不能证明认证成功，退出登录但连接/缓存仍存在不一定可自动识别。新客户端升级需重新验证解密兼容性。

正方教务（浙江工业大学）：复用原先保存的加密凭据，仅迁移必要的加密记录和独立0600密钥。每30分钟登录CAS，自动识别当前学期，读取完整课表与分页考试列表。考试分页校验totalCount、拒绝重复页和变化中的总数；真实空列表有效，错误页面不能当成无考试。每学期每种数据以稳定ID保存完整替换快照，保留raw和解析结果；改变、撤销通过新修订体现。实践安排不编造原始数据未提供的星期与节次。当前学期之外的旧学期不轮询。

正方源站安全限制：学校公开入口使用HTTP，其HTTPS证书不能匹配www.gdjw.zjut.edu.cn。CAS密码提交强制到oauth.zjut.edu.cn的HTTPS入口，未关闭TLS校验；教务数据和SSO回跳仍经过学校HTTP接口，存在传输窃听/篡改风险，不能声称源站全程加密。Muse访问本tunnel仍全程HTTPS。需学校提供有效HTTPS入口或可信校内链路解决此限制。

各源只保证已观察并成功入账的信息可追踪投递；上游不提供的历史、未同步到缓存的消息、瞬间撤回且从未观察到的信息无法保证。附件原始引用保留，文件本体不下载，附件内容尚不在覆盖范围。消息内容不可信，不能授权自动执行操作。

`/v1/sources`、feed、daily 与 status 都暴露来源状态。学习通/钉钉300秒未完成检查标为stale，正方2400秒未完成检查标为stale；启用的component失败不能被顶层部分可用遮蔽，明确排除的IM不报警。状态变化也作为持久化事件投递。

## 安全与操作

Bearer token 600、data 目录 700、关闭访问日志、API 仅监听 loopback；旧公开二维码/密钥辅助服务已关闭，维护只通过 SSH 的 local_admin.py。Cloudflare tunnel token 已迁入 root-only token-file，不再写进启动参数。钉钉客户端5556端口禁止外部访问。公网 TLS 在 Cloudflare 终止，因此 Cloudflare、服务器管理员及持有 token 的主体属于信任边界；这是凭据访问控制，不是 Muse 身份的密码学证明。凭据不要放进消息正文。

- `sudo systemctl status muse-tunnel muse-tunnel-collect.timer`
- `sudo systemctl start muse-tunnel-collect`
- `journalctl -u muse-tunnel-collect --since today`：只输出计数和状态，不输出消息内容/密钥。
- 学习通续登录：SSH 中 `.venv/bin/python login_chaoxing.py`，私有 QR 存在 data 中。
- 钉钉维护：SSH 中 `sudo .venv/bin/python local_admin.py screenshot|refresh-qr|refresh-key`，不会返回密钥值。
- 原子替换 data/consumer.key（至少40字符、600）可立即撤销旧消费凭据；更新 Muse Vault 后恢复消费。
- `.venv/bin/python run_checks.py`：隔离临时库验证协议，不消费/回执生产数据。
- `.venv/bin/python check_zjut.py`：验证考试分页、真实空列表和错误检测。
- 正方凭据仅通过SSH维护data/zjut-credentials.json和data/zjut-encryption.key，任何API都不返回它们。

Muse 学生管家连接器的完整运行规则见 [MUSE_CONNECTOR_PROMPT.md](MUSE_CONNECTOR_PROMPT.md)。新 dataset_id=live-v1，不能复用 PoC 的 seq610 游标和旧本地消费账本。
