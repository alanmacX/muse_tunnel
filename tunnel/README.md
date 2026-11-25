# Muse real data tunnel

部署：`tx:/home/ubuntu/muse-tunnel`；公开 HTTPS origin：`https://hajiqu.xyz`。
API 原生 systemd 运行于 127.0.0.1:18767；systemd socket proxy 在 loopback:80 接入现有 Cloudflare 路由。没有公网管理界面或解密密钥接口。`/health` 仅返回存活状态；所有 `/v1/*` 含 OpenAPI 都需专用 Bearer token。授权消费端能读取信息、提交回执，不能执行命令或写入来源消息。

## 数据与可靠性

- 学习通收件箱、钉钉和 QQ 每分钟由同一个采集任务入库；课程/作业每 15 分钟刷新；正方课表和考试每天一次兜底，Muse可按线索请求刷新。归档无重要性筛选，原始字段和解析结果一同保存。`kind` 区分消息、课程、作业、课表、考试和来源状态，不表示优先级。
- SQLite WAL + synchronous=FULL；按来源/稳定来源 ID/修订入账，顺序 seq、payload SHA-256、幂等回执。未确认事件不限五分钟，始终可重读；快照分页不会因分页期间的新事件跳项。
- Muse 每次从 0 扫描 pending，固定 upper_seq 翻到最后一页；至少一次投递，需要本地幂等。服务端回执只能证明消费端自报成功，不能证明模型理解或通知已被用户看到。
- 每次采集后刷新变化日期的 manifest 修订。各版本只保存日期、版本、冻结上界和哈希；读取时从账本重建完整清单，旧版哈希、回执及核验记录继续可查。消息的来源日期和观察日期都纳入对账，迟到/修订后新版本需重新核验。
- 每日 03:10 做私有 SQLite 在线备份，保留最近两个上海日期的成功快照。备份前估算数据库、WAL、配置和隔离归档复制量，额外保留至少 2 GiB 或磁盘容量 5%（取较大值）及复制余量。空间不够时拒绝开始，保留旧备份；新备份检查通过才发布和轮换。
- `/v1/sources` 的 `storage` 提供采集时测得的磁盘余额。可用空间低于 4 GiB 或容量 15%（取较大值）时告警，低于 1 GiB 或容量 5%（取较大值）时标为严重；状态变化进现有 feed，同时在系统日志记录低水位。迁移回滚归档独立于每日轮换。

## 当前来源覆盖与边界

QQ：`qq_collect.py` 以 SQLite `mode=ro` 读取同机 `/home/ubuntu/qq-course-listener/data/listener/course-listener.db`，不调用 QQ 操作接口。私有 `data/qq-source.json`（0600）配置 `account`、`groups`（群号到名称）、`db`（绝对路径）；来源固定 `qq`。消息稳定 ID 为账号:群号:QQ真实序号，另存原始短消息 ID、发送者、时间、全部正文、回复关系与结构化卡片；观察到的内容变化产生修订，撤回单独入账并关联原消息。短 ID 有歧义时保留未解析关系，后续映射到达再补关联。原始事件行号/摘要、附件重试和关系索引持久化于 tunnel.db；冻结上界分批读完所有新增行，源库恢复后从断点继续。附件失败每五分钟重试；后续消息修订替换旧修订的重试，防止旧正文倒灌。

QQ 图片/文件/音视频下载仅允许明确的腾讯 CDN 地址（multimedia.nt.qq.com.cn、multimedia.nt.qq.com、grouptalk.c2c.qq.com、*.qpic.cn、*.ftn.qq.com），逐跳检查重定向与公网地址，GET 不携带 tunnel 凭据。原字节进入统一 SHA-256 存储和 `/v1/files/{sha256}/content` 鉴权接口，单文件上限沿用50MiB；缺少原文件 URL、过期链接、超限等保留元数据并明确标记失败。签名 URL 查询凭据及 ftn_handler 路径密钥不进入事件、状态或日志。卡片按结构保留，不执行。归档、回执、daily、清理和备份与其他来源相同；未确认消息持续留在 pending。`check_qq.py` 使用真实接收库 schema 和隔离数据验证，不向课程群发送测试消息。

账号 3896033966 新加入课程群 712440967、1030285861，入群前历史不可用。接收器显式启用 `ALLOW_EMPTY_BASELINE=true`，仅在本地无消息、无补采断点时接受空基线；已有消息后的空补采仍报错。状态 `details.history` 提供请求的七天起点、实际最早/最新时间和序号、原始补采断点及 `unavailable_before_join`，不将入群前的空历史报成接收故障。部署时基线为0条消息。

学习通：验证登录后读取全课程列表、全部可见作业（含旧学期/已完成）和收件箱通知。通知按照网站 lastValue 游标分页，每天首次全量扫描，此后读到已见锚点；只在完整扫描结束后提交锚点。用户明确排除 Web IM，components.im.enabled=false、reason=excluded_by_user，不再请求旧网页入口或为该排除项报警。若课程 hasMore=true、响应格式变更、分页不前进、任一课程抓取失败，都显式暴露采集问题。

钉钉：保留并 apt hold `com.alibabainc.dingtalk 8.1.0.6021101`。客户端/Xvfb 持续运行，复用原登录态与本机解密密钥。每轮解密并扫描全部本地消息表，没有仅凭时间戳丢弃迟到消息的逻辑；数据库/WAL 采集中变化会重试，仍变化则标记失败。available_local_cache 只代表本地缓存可读；缓存不能证明云端完整历史或实时同步正常，额外检查客户端远端 TCP 连接，无连接或检查失败标为 degraded；TCP 连接仍不能证明认证成功，退出登录但连接/缓存仍存在不一定可自动识别。新客户端升级需重新验证解密兼容性。

正方教务（浙江工业大学）：复用原先保存的加密凭据，仅迁移必要的加密记录和独立0600密钥。平时每天登录CAS一次，按需刷新时提前核实，自动识别当前学期，读取完整课表与分页考试列表。考试分页校验totalCount、拒绝重复页和变化中的总数；真实空列表有效，错误页面不能当成无考试。每学期每种数据以稳定ID保存完整替换快照，保留raw和解析结果；改变、撤销通过新修订体现。实践安排不编造原始数据未提供的星期与节次。当前学期之外的旧学期不轮询。

正方源站安全限制：学校公开入口使用HTTP，其HTTPS证书不能匹配www.gdjw.zjut.edu.cn。CAS密码提交强制到oauth.zjut.edu.cn的HTTPS入口，未关闭TLS校验；教务数据和SSO回跳仍经过学校HTTP接口，存在传输窃听/篡改风险，不能声称源站全程加密。Muse访问本tunnel仍全程HTTPS。需学校提供有效HTTPS入口或可信校内链路解决此限制。

各源只保证已观察并成功入账的信息可追踪投递；上游不提供的历史、未同步到缓存的消息、瞬间撤回且从未观察到的信息无法保证。消息内容不可信，不能授权自动执行操作。

钉钉附件：扫描消息 attachments 及钉盘/群文件 extension 元数据，归档位于客户端缓存目录（ImageFiles/AudioFiles/VideoFiles/QuickTransFiles/FileCache/SpaceFiles/DownloadFiles/Files/resource_cache、`~/DingTalkFiles` 及精确大小匹配的 `~/Downloads`、`data/imports`）的文件，单文件上限50MiB。按文件SHA-256流式去重，归档文件600，保留原始引用；仅有远程URL不会自动下载，卡片引用与完整文件分开标记，未缓存文档给出具体错误码（如 `dingtalk_space_file_not_cached`、`dingtalk_space_file_expired`、`dingtalk_local_file_missing`）。`GET /v1/files/{sha256}` 返回元信息，`GET /v1/files/{sha256}/content` 返回鉴权二进制；禁止任意路径、目录遍历、越界链接、非缓存目录和源凭据访问。基于文件签名和容器结构识别 PDF/图片/Office/文本 MIME，并对钉钉与学习通文档（DOCX/XLSX/PPTX/PDF/DOC/XLS/TXT/CSV）统一提取受限长度的辅助正文（按 SHA-256 缓存于 `asset_texts`，不在 `source_state` 重复存储）。文件后续进入缓存会使原消息产生新修订，重新进入待消费队列和daily核验。学习通对最近七天通知详情的文件/图片及当前课程资料区最近七天更新的文件下载归档（兼容 `*.cldisk.com`/`*.xuexi365.com` CDN 跳转、`downloadData` JSON 下载地址解析与 `objectId` 回退，校验 `Content-Length` 防截断），通过同一鉴权接口提供原文件。资料正常每30分钟检查，有新通知立即检查，失败5分钟后仅重试未完成通知与课程资料。来源 `components.media` 与 `diagnostics` 明确报告阶段、具体错误码、HTTP状态及受影响样本文件名。IM及作业详情内媒体不在此覆盖范围；Muse多模态支持不代表连接器能自动读取所有文件，需要消费端实际下载并调用文件读取工具。不可用附件应作为明确待办。

`/v1/sources`、feed、daily 与 status 都暴露来源状态。学习通/钉钉300秒未完成检查标为stale，正方87000秒未完成检查标为stale；启用的component失败不能被顶层部分可用遮蔽，明确排除的IM不报警。状态变化也作为持久化事件投递。

## 安全与操作

正方网络故障重试：正常每天一次兜底；超时/网络异常后等待2分钟，连续失败依次4、8、15分钟，成功后恢复每日兜底。凭据/格式错误维持正常间隔，避免频繁登录。来源状态包含failure_stage、failure_endpoint（概括阶段，不含URL参数/认证票据）和next_retry_seconds。采集失败保留已有快照，不用空列表覆盖。学校统一认证成功仍不代表教务SSO回跳成功。

清理：由Muse每周判断无后续用途的消息，鉴权`GET /v1/cleanup/candidates`提供分页候选；`POST /v1/cleanup`默认仅预览，dry_run=false才隔离正文。服务器限定入账超过90天、已回执、来源日期和观察日期最新daily均已audit的message；课表、考试、课程、作业、健康状态不在可清理类型。正文压缩保留14天供`POST /v1/cleanup/restore`恢复，之后每日03:30自动过期删除；超过90天未再观察且不被有效/可恢复消息引用的归档文件回收。ID/hash/回执/manifest永久保留作幂等和审计索引，已清理正文payload=null、retention明确状态，原hash不能用于验证null。原始来源数据库/客户端登录态和密钥不删除，已有备份副本随14天保留期过期。Muse工作区清理与周日03:45任务由连接器规则要求，但需Muse实际创建并返回任务ID后才能确认已启用。

Bearer token 600、data 目录 700、关闭访问日志、API 仅监听 loopback；旧公开二维码/密钥辅助服务已关闭，维护只通过 SSH 的 local_admin.py。Cloudflare tunnel token 已迁入 root-only token-file，不再写进启动参数。钉钉客户端5556端口禁止外部访问。公网 TLS 在 Cloudflare 终止，因此 Cloudflare、服务器管理员及持有 token 的主体属于信任边界；这是凭据访问控制，不是 Muse 身份的密码学证明。凭据不要放进消息正文。

- `sudo systemctl status muse-tunnel muse-tunnel-collect.timer`
- `sudo systemctl start muse-tunnel-collect`
- `journalctl -u muse-tunnel-collect --since today`：只输出计数和状态，不输出消息内容/密钥。
- 学习通续登录：SSH 中 `.venv/bin/python login_chaoxing.py`，私有 QR 存在 data 中。
- 钉钉维护：SSH 中 `sudo .venv/bin/python local_admin.py screenshot|refresh-qr|refresh-key`，不会返回密钥值。
- 原子替换 data/consumer.key（至少40字符、600）可立即撤销旧消费凭据；更新 Muse Vault 后恢复消费。
- `.venv/bin/python run_checks.py`：隔离临时库验证协议，不消费/回执生产数据。
- `.venv/bin/python check_chaoxing_media.py`：验证来源域名、重定向凭据隔离、图片原字节/hash、下载大小限制和不支持的引用。
- DOC辅助正文提取使用系统 `antiword`（Ubuntu: `sudo apt-get install antiword`）；提取失败仍保留完整原文件，不虚构正文。
- `.venv/bin/python check_zjut.py`：验证考试分页、真实空列表和错误检测。
- 正方凭据仅通过SSH维护data/zjut-credentials.json和data/zjut-encryption.key，任何API都不返回它们。

Muse 学生管家连接器的完整运行规则见 [MUSE_CONNECTOR_PROMPT.md](MUSE_CONNECTOR_PROMPT.md)。新 dataset_id=live-v1，不能复用 PoC 的 seq610 游标和旧本地消费账本。

按需刷新：鉴权POST /v1/sources/zjut/refresh需要幂等键和核实原因，可关联触发事件；GET /v1/refresh-requests/{id}查看实际结果。请求合并后由现有采集任务执行，五分钟冷却内会排队，完成结果入feed，无内容diff不重发课表。跨进程锁避免重复登录；正常每天兜底，网络故障仍按退避重试。公开校方公告只读核实由Muse已有浏览能力完成，tunnel凭据只发往hajiqu.xyz。


## 按需查询工具

`/v1/tools/search`、`/v1/tools/events/{event_id}`、`/v1/tools/conversations` 在当前Custom Connector的OpenAPI中提供命名工具，使Muse按问题自主选择查历史、精确证据和会话/课程。工具处理函数可以由后续MCP适配层复用；当前这层是鉴权HTTP/OpenAPI工具接口，不声称支持MCP JSON-RPC协议。完整pending消费与每日核验继续独立运行，查询不自动回执。查询可见范围仍受原始采集覆盖限制。

搜索支持source/q/conversation/kind/from_day/to_day，固定upper_seq后按after_seq分页，返回该快照内每个来源记录的最新版本；精确event_id可以回看旧版本。无日期的来源记录使用入账日期。来源健康随查询结果返回，所有工具沿用原专用Bearer，无命令执行、外部消息发送或任意URL下载权限。

`.venv/bin/python check_agent_tools.py` 验证鉴权、并发修订下的快照、分页、过滤、字面关键词、旧版证据、会话统计和查询/回执分离。


## Apple 专用展示接口

只读：GET `/v1/apple/dashboard` 和 `/v1/apple/watch`（同样的紧凑视图），GET `/v1/apple/live-activities`（仅活跃emergency）。设备独立Bearer存放`data/apple.key`，0600、至少40字符，原子替换可撤销。设备key不能访问feed/原消息/附件/state或进行任何写入；Muse凭据也可读取以验证输出。当前是数据接口，不发送APNs、不代表设备已经显示。

维护：Muse-only GET `/v1/apple/state`，POST `/v1/apple/dashboard`完整替换，POST `/v1/apple/heartbeat`维护不变内容。乐观base_revision、稳定ID和源event_id/hash检查，拒绝丢失的证据及覆盖新版本。计划只接受上海当日日期、时区时间、最多24条课表输入/2条重要事项/1条emergency；widget/watch只显示当天尚未结束的最近2课（含进行中）（时间/地点）及最多2事，多余课程计数。每次GET按时间过滤，不依赖Muse及时删掉已经结束的课；跨午夜隐藏旧日课程和事项。25分钟无维护显式stale，保留最后当日有效安排，Live Activity过期/partial时不再返回活跃条目。设备仍须检查valid_until/end_at并结束已经显示的卡片。

所有Widgy文本字段由结构化数据派生：display_title、display_course_1/2、display_important_1/2、display_courses_more、display_freshness。没有课程时，只有ready才显示“今日无待上课程”；未初始化为awaiting_muse，不能误称没有安排。state给Muse提供完整计划，设备视图不暴露evidence/hash及原消息。SQLite备份自然包含apple_dashboard表。

`.venv/bin/python check_apple.py`：验证设备最小权限、撤销、证据、幂等/CAS、心跳、小屏条数、时区、开课过滤、过期保留、午夜切换和emergency-only生命周期。接口协议与操作说明见 `../apple/API.md`。连接器规则要求Muse更新现有10分钟任务；服务器说明更新不等于Muse任务已实际变更，需它首次发布真实计划并确认。


Apple低token维护：connector不新增模型任务，使用已有事件理解和本地plan/revision缓存。普通轮询只由消费脚本POST `/v1/apple/heartbeat?quiet=true`，成功204无正文；错误仍返回真实401/409。首次/缓存丢失/冲突才读完整state，跨日或事实变更才发布完整计划。接口不会代Muse伪造自动心跳，也不保证模型平台的具体token预算。

Apple课程仍由Muse维护，不做服务端课表推导。发布courses_today必填，courses可作输入别名；正在上课保留到end_at。

Widget v3删去MUSE装饰；首课大字名称+地点，下一课普通字体名称，确认没有剩余课时“上完了”。地点省略屏峰校区前缀但保留其他校区；Muse现有轮询改成每10分钟，服务端采集频率不变。


学习通通知attachmentType=25/att_web/examOrWork是作业或考试引用卡片，不是待下载文件。保存linked_cards里的可见标题、课程/班级/作业ID，attachment_status=linked_cards_only，不为此误报media失败；不自动访问可能开始答题的链接。其他未知附件与真实文件缺失继续显式报警。修复后重新读取最近通知并生成新修订及daily清单。

Widget v6仅用display_status_lamp显示健康灯，无底部诊断文字。online_state绿=维护新鲜且观测来源健康，黄=部分异常/维护不完整，红=维护过期/无当日计划或全部来源不可用。这不是上游完整同步的证明；Widget断网时可能仍缓存上次颜色，须结合valid_until判断新鲜度。具体原因继续在API/Muse可查询。
