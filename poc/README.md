# Muse Tunnel synthetic PoC

已部署到 SSH 主机 `tx`，目录 `/home/ubuntu/muse-tunnel-poc`。代码位于 Student-Agent 的本地 `muse-tunnel` 分支。独立 FastAPI + SQLite 服务，没有真实上游凭据、采集、附件下载或 LLM 摘要生成。

## 访问与 Muse 接入

当前 HTTPS 地址： https://graduates-carb-appendix-investors.trycloudflare.com

健康检查公开：`GET /health`，只显示 synthetic 状态。所有 `/v1/*` 包括 OpenAPI 都要求 Authorization Bearer；查询参数 token 不受支持，没有 CORS 放行。

专用凭据在服务器 `data/consumer.key`，本地副本在 `poc/data/consumer.key`；目录 0700、文件 0600，Git 已忽略。不要提交、分享或放进聊天。通过 Muse Secure Credentials Store 的安全授权流程提供。当前凭据仅代表单个消费者的持有者，并非不可转移的 VM 身份证明。

将 `MUSE_CONNECTOR_PROMPT.md` 的内容交给 Muse 建立连接器。用户已在个人 Muse 完成首次全量测试；服务端独立核对结果见下方实测记录。定时任务已由用户转述创建成功，尚需观察后续实际触发。

Cloudflare Quick Tunnel 用于本次实验。进程重启后域名可能改变，无生产可用性承诺。换域名时读取该服务日志里的 trycloudflare 地址，更新连接器域名授权和提示词。正式部署应换固定域名/命名隧道。

## 已实现的协议

- 608 个模拟事件版本，18 会话、相同时间戳、同文不同 ID、修改、撤回和迟到消息。
- SQLite WAL + synchronous FULL；事件唯一键按来源/源 ID/版本，入库序号升序。
- 固定上界分页；pending_only 默认 true，每轮从 0 重扫未确认数据。
- 只有已交付且 hash 匹配的事件可 ACK；回执批次整体验证并事务提交，重复 ACK 幂等。
- 每日确定性完整 manifest、原文分页、hash、未回执差集；旧日期事件按源日期和采集日期分别纳入，补录生成新 revision。
- 每日审计拒绝未确认项和旧 revision，不删除历史包。概况中的文字摘要仅是确定性计数，完整清单才是核验依据。
- 每分钟由 timer 补生成缺少的日期包/刷新修订；未来真实采集器应提供明确采集完成水位，这个 PoC 不宣称 source coverage。
- 没有删除、后台配置、任意 SQL、任意 URL fetch 和真实上游写入 API。

回执和审计是消费者自报结果；不能证明模型理解或长期记忆。GET 记录交付候选不是网络送达证明。未确认事件永远可再次读取。每日完整清单不能检测从未采集到的信息。

## 运行与管理

```sh
ssh tx 'systemctl is-active muse-tunnel-poc muse-tunnel-poc-https muse-tunnel-poc-daily.timer'
ssh tx 'sudo journalctl -u muse-tunnel-poc -n 30 --no-pager'
ssh tx 'sudo journalctl -u muse-tunnel-poc-https --no-pager | rg trycloudflare'
```

服务监听 `127.0.0.1:18765`；HTTPS 隧道独立于已有 cloudflared 服务。systemd 限制文件写入到专用 data 目录；消费者 key 原子替换后旧 key 即失效。没有改动现有 Student-Agent 配置或应用代码。部署补装 python3.12-venv 时，系统包管理器同时更新了 Python 3.12 相关包，并自动重启了 dingtalk-qr、rewards-rerun 服务；最终已核对服务状态。

本地管理命令（在服务器 PoC 目录执行）：

```sh
.venv/bin/python provision.py refresh
.venv/bin/python provision.py append-late
```

append-late 是幂等的单条补录测试，不通过公网提供。会增加第 609 个事件并修改相关每日包 revision。在线 Muse 测试初始数据尚未执行这个操作。

停用 PoC：

```sh
ssh tx 'sudo systemctl disable --now muse-tunnel-poc-https muse-tunnel-poc-daily.timer muse-tunnel-poc'
```

## 验证

```sh
cd /home/ubuntu/muse-tunnel-poc
.venv/bin/python run_checks.py
```

测试使用临时数据库与独立 loopback 端口 18766；不会提前 ACK Muse 需要消费的正式模拟数据。已通过：认证与 query token 拒绝、分页边界、未交付/hash 错误回执拒绝、批次回滚、重复 ACK、未 ACK 重放、608 事件完整分页、相同正文不同 ID、迟到事件、manifest hash、每日审计、进程强杀后状态持久化、补录 revision、旧审计拒绝、key 替换即时撤销。

公网已验证无凭据 401、正确凭据读取状态与 OpenAPI；正式模拟数据初始 pending=608。测试没有证明真实上游完整性、Muse 调度 SLA、提示注入防御或机器损坏后的数据恢复。没有生产备份、业务告警或 OAuth；均为后续阶段工作。


## 2026-10-09 个人 Muse 实测记录

用户报告：7 页获取 608/608 个事件版本，payload hash 全部通过，本地持久化账本 pending=0；2026-10-09 的 608 条和 2026-10-08 的 1 条补录每日包均核验通过。

服务端独立查询确认：events=608、receipts=608、pending=0，学习通 305 / 钉钉 303；两个日期 revision=1 的 audit 都存在，服务器记录换算为北京时间分别在 14:08:11 和 14:08:15 完成。此证据证明消费协议跑通，不证明每条信息的语义理解和长期记忆。

用户报告 Muse 已设置每 5 分钟静默轮询、每天 00:15 审核前一天；尚未独立验证定时任务未来实际执行、连接器重试或长期运行。

Muse 环境中的 Python urllib 带凭据请求发生挂起，用户报告已改为 curl 并通过测试。保留该实现选择，不建议切回 urllib。原因尚未定位；本地与服务端 Python 的测试成功不能推断 Muse VM 中相同请求也正常。

用户报告凭据只在 Secure Vault，正文视为不可信数据；服务端不能核实 Muse VM 内的凭据存储或提示注入防护。`/v1/status` 中 `muse_runtime_verified=false` 是第一版静态字段，不随回执更新，不能当作当前实测结论。

下一步建议：用单条迟到补录验证定时增量消费与每日包新 revision 的重新审计；再换固定域名，逐个引入真实采集器。尚未向在线实例注入第 609 条事件。


## 高优先级增量通知测试

2026-10-09 14:11:55（Asia/Shanghai），经用户授权，通过本地管理命令 `provision.py alert-test` 注入 `dingtalk:mock-608:v1`（seq=609，ALERT-TEST-001）。模拟截止时间为 14:26:55，priority=high、kind=alert，明确声明无真实作业。重复命令不会再次入库。

服务端记录 Muse 于 14:13:42 确认该事件，距注入约 1 分 47 秒；证明增量读取与回执成功。是否触发系统通知、通知内容和设备展示仍待用户确认。服务端 ACK 不等于通知送达。


2026-10-09 14:24:53，用户转述 Muse 已修复 notable 分级和通知规则后，再注入 `dingtalk:mock-609:v1`（seq=610，ALERT-TEST-002）。同时满足 kind=alert、priority=high、30分钟内到期（14:54:53）三项触发条件。明确标为无真实作业的通知测试。注入成功，实际通知送达待用户确认；不能仅从载荷保证 Muse 发出推送。
