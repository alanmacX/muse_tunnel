# Muse / Widgy

2026-10-12 实机截图显示：此前生成器误把 `36` 当成图层数组，导致 Layers:0 和 Invalid Name。`36` 实际为变量列表，视觉图层使用 `1`。用户原始变量导出保存在user-sample.json，不修改证据。

当前正式模板：design/muse-small-dark.json / muse-small-light.json，不含凭据。生成命令：python3 apple/widgy/build_small.py。保留用户已确认的小号配置0=29、6=2、a2=0；8个实际图层、空变量列表、背景置后。字段来自正式Apple接口。

按用户明确要求，带Apple专用只读key的文件由python3 apple/widgy/build_private.py生成到tunnel/data/widgy，0600并被Git忽略。Endpoint的21字典内配置Authorization: Bearer <Apple key>。生成器使用文件中的Authorization值验证HTTPS与6个数据字段；不打印真实key、不写进公共模板或Git。实际Widgy请求解释和字体/布局仍需要原生验证，网络验证不冒充实机验证。

muse-connection-test.json为单个真正图层，GET公开/health的mode，无需key。旧的十行错误draft已删除。设计PNG/HTML为示例数据，不是真机截图。

v3：顶部大字当前/下一节课程名与地点，底下后一个课程名；无后课留空，已核实当天无剩余课显示“上完了”。没有MUSE装饰或日期。接口维护改为10分钟、stale阈值25分钟；Muse任务由用户代发更新。
