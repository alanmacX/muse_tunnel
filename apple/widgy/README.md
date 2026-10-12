# Muse Tunnel / Widgy 模板草稿

基于用户当前 Widgy 导出（文档版本 `0=29`，图层数组 `36`）生成，不使用同名第三方 App 的格式。

## 先测试

在 Widgy 的 Import / 导入中选择 `muse-connection-test.json`（如该版本只支持粘贴 JSON，则粘贴文件全文）。它保留原样本的单图层与位置，仅改了组件名称、图层标签和返回字段：GET `https://hajiqu.xyz/health` 的 `mode`，应显示 `live`。不需要真实 key。这个 endpoint 已验证返回 200。

## 今日安排草稿

`muse-today-dashboard.draft.json`：十个文字层，显示今日课程前三项、今日截止前两项、其余数量、更新时间和健康提示。尺寸/组件种类沿用用户原样本。使用常见 Widgy 0..1600 设计坐标构建；当前版本的实际布局、文字大小、换行与导入兼容性还需要在原生编辑器验证，不能声称已在 iPhone 上渲染成功。

正式接口 `https://hajiqu.xyz/v1/apple/dashboard` 已实现部署，见 ../API.md。此前这个布局草稿使用较大的十行设计及旧display_deadline字段，不符合最新最小widget协议；不要把它当成成品。最终模板以API.md规定的两节课+最多两件事为准，用户已确认原样本是小号，最终小号模板见design/muse-small-dark.json及muse-small-light.json。未由Muse首次发布时服务器明确返回awaiting_muse，不冒充真实日程。`apple-dashboard.example.json` 是本地 synthetic 示例，不会写入生产 tunnel。

Muse维护结构化的 courses_today / important_events / live_activities，设备读取courses / important_events / live_activities，同时派生稳定的 display_* 文本字段供 Widgy 绑定，不需要让 Widgy 拼日期、排序、计算调课或解析任意数组。文本由服务器根据结构化事项生成，Muse不直接维护两份互相可能矛盾的列表。空槽返回空字符串，多余事项显示数量；前几项为当前仍相关事项，取消/结束状态不能继续显示成待处理。

display_freshness 必须包含实际 Muse 维护时间、过期提示以及影响安排可信度的来源异常。设备缓存仍可能过期：动态过期判断需要在 Widgy 时间源或客户端另行验证，不能把固定文本当作新鲜度保证。

样本确认了 Authorization/Bearer 的配置元信息，并没有展示实际凭据存储格式，因此生成器仅保留元信息、不猜测密钥字段。正式接入后须在 Widgy 中单独配置 Apple 专用只读 key，并验证返回200；不使用 Muse consumer.key，不把真实 key 写进分享模板。

Watch 和 Live Activity 的文档类型、尺寸标识尚未获得实际样本；不能通过随意改数字将手机模板声称为 Watch/Live 模板。可在相应编辑器创建文本 JSON Endpoint 层并导出，以同样方法生成。Widgy模板也不等于服务器能后台推送更新Live Activity。

生成命令：`python3 apple/widgy/build_templates.py`。仅验证 JSON、路径、图层ID，不调用线上消息回执、不包含凭据、不访问来源站点。
