# 最小widget设计 v1

设计预览：muse-small-preview.png；交互预览：preview.html。五个状态与158/173pt密度检查；这是设计稿/示例数据，不是Widgy真机截图。没有把个人实时计划写入预览，预览不联网。

v3布局：删去MUSE装饰与日期；第一课程名大字、下方地点；下一课普通字号名字（没有则空白）；分隔线；最多两件短标题事项；维护时间。所有课程结束且已核实才显示“上完了”。无课、数据待补齐和信息过期分别表达，不能混为一谈。正在上的课保留到结束；屏峰地点只显示楼/教室，其他校区保留校区名。重要事项长标题需Muse在已有判断时写成适合一行的短标题，完整action仍在接口；不能在Widget盲目截断后声称完整呈现。短事项不代表另一次分类/模型调用。

使用Apple专用只读key，在Widgy鉴权输入单独配置Authorization/Bearer。JSON模板不含真实key，也没有猜测Vault的存储格式。

导入：muse-small-dark.json（深色）或muse-small-light.json（浅色）。用户已确认原始user-sample.json就是小号，模板原样保留其文档类型/尺寸配置0=29、6=2、a2=0，不需要再次导出或猜测枚举。修正后图层存放在1数组，36仅为变量列表且为空；8个图层，背景置后。图层和实际JSON Endpoint绑定已静态验证；实际字体/背景/字距与鉴权读取仍需导入后在Widgy验证，设计预览不能冒充实机截图。

接口新增display_date、display_course_1_time/location、display_course_2_time/location、display_widget_footer；均由已发布的结构化数据确定性派生，没有新增Muse维护字段或模型任务。字段正好适配左右时间/地点列。display_courses_more由服务器计算。

之前误写变量区的十行draft已删除。Watch/Live类型未验证，不能用手机候选冒充其他类型。

内置只读key的v3文件在tunnel/data/widgy/muse-small-dark-v3.json及light-v3.json，0600且Git忽略。Muse任务频率改为每10分钟需用户代发指令并由Muse实际确认，服务器不直接操控Muse。

v4：地点与维护状态不再使用uicol_gray，改用与实机已显示的课程名一致的黑/白色。接口已确认地点非空、字段绑定正确；样式修正仍需实机确认。内置key文件版本号v4。


v5：首选跟随系统版本muse-small-auto-v5.json，前景为uicol_label、背景为uicol_systemBackground（Widgy/UIKit语义动态色；导入后的原生切换仍需确认），固定黑/白版本作为备用。没有分割线。小鸡由用户提供图经imagegen透明抠图，保存在tunnel/assets/widget-chicken-v1.png；固定公开HTTPS图片地址仅提供非私密装饰，图片位于右下角、文字层之后、背景层之前，尺寸约31×36pt。文本保持最上层，没有新增Muse字段或模型任务。

底部“待核实”过去是过于笼统的维护/来源健康说明。现在对仅学习通media失败而通知/作业可用的情况显示“附件缺失”，其他来源失败显示“来源异常”，维护partial但无已知来源错误显示“部分未确认”；过期与缺计划仍各自提示。状态不代表课程本身一定未核实。带key文件仍只在tunnel/data/widgy，绝不进Git。图片是透明PNG，不依赖设备本地照片权限。

v6：底部仅一个状态灯，使用display_status_lamp（🟢/🟡/🔴），不显示诊断文字。维护与来源状态由服务器合并，具体诊断保留在API。不宣称缓存的绿色等于设备网络实时在线。自动色、小鸡和无分割线保持。私有文件版本v6。
