# 学习通 Web IM 故障定位

实测时间：2026-10-09 15:07–15:10（Asia/Shanghai）。

结论：当前账号登录后，官方 Web IM 入口 https://im.chaoxing.com/webim/me 返回 HTTP 200 的信息提示页，正文为“系统维护中，功能暂时无法使用”。没有 myTuid、myPuid、myToken，也没有聊天脚本。因此旧 repo 的第一步 IM 参数获取就已经被上游页面阻断，尚未进入会话列表/历史消息请求。没有证据支持将此问题诊断为消费 tunnel、分页、模型判断或 Python HTML 提取错误。

| 差分检查 | 结果 |
|---|---|
| tx，不带登录 cookie | 正常 302/301 跳到 passport2 登录页 |
| tx，当前有效登录 cookie，桌面 UA | 无重定向，HTTP 200，维护信息页，1424 bytes |
| tx，当前有效登录 cookie，手机 UA | 与桌面维护页 SHA-256 相同 |
| tx，增加 query 参数 | 相同维护页 |
| 本机网络，复用当前账号登录态，curl | HTTP 200，相同维护文案，无 IM token；排除只发生在 tx 出口的故障 |
| 当前账号课程、收件箱、作业 | 可读取真实数据，登录有效 |
| 官方个人空间菜单 | 仍包含 changeMenuName('消息','/webim/me',`Messages`) |
| /webim/ 与 / 根路径 | 404 / 400，不构成可用替代入口 |

用户已确认同一账号手机 App 群聊可正常打开并看到新消息。因此并非整个 IM 服务不可用，故障范围限定为当前账号访问的网页 IM 入口；尚无法确定是短期维护、Web IM 长期停用还是账号/机构策略限制。未找到官方维护时间公告；不能把提示页当成“全平台所有用户都在维护”的证据。后续应调查客户端 IM 认证/取数路径。当前 Web token 不能从提示页获得，课程登录 cookie 本身不能代替环信 IM token。

来源状态保持 degraded，details.components.im.available=false，reason=web_im_entry_maintenance；通知/课程/作业仍独立采集。此状态只对应网页采集入口，不代表学习通所有消息均不可用。

可复跑差分探测：tunnel/diagnostics/im_probe.py。输出只有状态、路径、页面标题、hash 和 token 字段是否存在，不输出 token/cookie 值。诊断临时拷贝的登录资料已删除。

本机已安装官方学习通桌面客户端 com.chaoxing.cxstudy 1.4.2。只读检查其 app.asar 中的发布代码发现独立的环信 IM SDK、/token、/messageroaming、/sdk/message/roaming/ 等调用；说明客户端存在不同于旧网页 myToken 提取的路径。此发现仅证明有客户端实现可供继续研究，尚未证明课程 cookie 可取得该 token 或服务端采集已接通。客户端分析输出私有保存于 tunnel/data/client-analysis，不提交账号数据。

## 复查：历史与兼容性排除

旧服务私有备份数据库有 16 条真实会话探测签名。最后写入 updated_at=2026-08-17T15:47:12.047124，原代码使用 datetime.utcnow() 写入，即北京时间 2026-08-17 23:47:12。代码路径只有成功获取网页 IM 参数与会话后才产生 probes，因此这是“以前确实可用”的历史证据（按原实现及原数据库可信这一前提），不应表述成一直不可用。数据库没有失败开始时间，所以不能定位停用日期；已知窗口为该成功记录之后至 10 月 9 日首次失败观测之间。

复查移除 route/JSESSIONID、仅使用核心认证 cookie、切换桌面/手机 UA、增加多种客户端 query 参数，HTTPS 仍收到相同 SHA-256 的维护页。排除了已测试的客户端参数和旧路由 cookie 兼容性原因，但不能排除账号/机构策略。

桌面客户端发布代码明确使用 MainWindowHelper.getImInfo() 的 tuid/hxToken 调用 tokenLogin，而不是解析网页 myToken；具体认证获取逻辑在打包的主进程模块内，尚未验证服务端替代接入。
