# Apple 专用接口 v1

正式地址：`https://hajiqu.xyz/v1/apple/dashboard`。

## 设备只读

Authorization: Bearer <Apple-only key>。不要使用Muse完整key。

- GET /v1/apple/dashboard：最小widget的数据。
- GET /v1/apple/watch：同一小屏协议。
- GET /v1/apple/live-activities：只返回仍有效的emergency卡片，最多1条。

widget/watch返回：date、timezone、revision、maintained_at、valid_until、status、stale、courses（最多2条）、remaining_courses_count、important_events（最多2条）、source_issues、live_activities（最多1条）及display_*字符串。courses只含今天未开始的课，按实际开课时间排序；时间/地点为首要显示字段。每次GET自动过滤已开始的课程，无需等待Muse下一轮。

Widgy绑定：

| 图层 | JSON路径（导出中的23数组） |
|---|---|
| 第一节课时间+地点 | `["display_course_1"]` |
| 第二节课时间+地点 | `["display_course_2"]` |
| 第一件重要事项 | `["display_important_1"]` |
| 第二件重要事项 | `["display_important_2"]` |
| 其他课程数量 | `["display_courses_more"]` |
| 维护时间/故障/过期 | `["display_freshness"]` |

最小widget不要显示原消息正文、全部课程标题、全部待办或来源诊断详情。此处规定数据协议，不声明现有手机模板的数字尺寸标识已经确认是小号；实际Widgy样本需选择小号重新验证布局。Watch可以按空间进一步少显示一行，但不更改事实。

状态：awaiting_muse / awaiting_today_plan / ready / partial / stale。没有计划和真的没有课程是两回事。15分钟维护过期时保留当日最后课程/事项、明确标过期；跨午夜不显示昨天事项。设备必须用valid_until进行本地过期判断，不能把缓存的ready一直当成最新状态；源站问题独立通过source_issues表达。

## Muse 维护

使用已有Muse Vault Bearer。GET /v1/apple/state取得revision、plan。POST /v1/apple/dashboard需要：

- base_revision、date（必须Asia/Shanghai当天）、maintenance_status=ok/partial。
- courses_today最多24条，id/title/start_at/end_at/location/evidence。
- important_events最多2条，id/title/action/reason/due_at（可null）/status/evidence。
- live_activities最多1条，id/title/severity=emergency/action/reason/start_at/end_at/status/evidence。
- evidence是非空 `[{event_id:真实来源事件ID,hash:实际事件hash}]`。详细输入约束见鉴权OpenAPI。

这是完整替换。先合并课表基线、当前通知与已有事项；不因为只收到一条新消息就把其他安排丢掉。稳定ID用于去重与取消，改变内容才增长revision，同内容重试幂等且维护心跳。409须重读合并，不能无条件覆盖。成功返回device_push_sent=false，明确只是保存数据。

每5分钟核实，内容不变时POST /v1/apple/heartbeat：`{"base_revision":N,"maintenance_status":"ok"}`，部分未完成则partial。跨午夜必须发布今日计划。每日核验和pending消费继续独立，Apple维护成功不代表消息已经理解、提醒任务已经创建或设备已经展示。

Live Activity只允许emergency：已核实、明确影响本人、现在必须处理，延迟会造成明显损失。普通time_sensitive、普通截止倒计时、下一节课倒计时不允许。每条结束时间最多距开始8小时；结束/取消/计划过期或partial不返回活跃条目。客户端须对以前显示但当前消失的ID、end_at/valid_until到期的条目执行结束。当前没有ActivityKit/APNs适配层，Widgy仅凭GET不能被宣称已实现后台实时推送。

## 凭据维护

服务器`data/apple.key`独立0600，目录700；可原子轮换立即撤销。只通过SSH私有文件传递给设备，不写进聊天、分享模板或日志。设备key只开放三个展示GET，其余endpoint拒绝401。原始消息、附件、数据库、登录态仍是Muse/管理端可见。
