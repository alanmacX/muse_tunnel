# 最小widget设计 v1

设计预览：muse-small-preview.png；交互预览：preview.html。五个状态与158/173pt密度检查；这是设计稿/示例数据，不是Widgy真机截图。没有把个人实时计划写入预览，预览不联网。

信息布局：MUSE与短日期；最多两行时间+地点；分隔线；最多两件短标题事项；维护时间与其他课程数量。无课、数据待补齐和信息过期分别表达，不能混为一谈。正在上课的时间槽显示“进行中”，地点仍保留。重要事项长标题需Muse在已有判断时写成适合一行的短标题，完整action仍在接口；不能在Widget盲目截断后声称完整呈现。短事项不代表另一次分类/模型调用。

使用Apple专用只读key，在Widgy鉴权输入单独配置Authorization/Bearer。JSON模板不含真实key，也没有猜测Vault的存储格式。

导入候选：muse-dark-size-0.json等。候选基于用户v29导出图层结构和已确认的JSON Endpoint字段绑定；保留框架，新增常见text/shape样式。root 6的0/1/2是待验证的尺寸候选，不能声称已确定0对应小号。选导入后Widgy明确标为Small/小号的文件。当前字体/颜色/背景样式取自旧版公开导出，v29行为仍需原生验证；若任何图层位置/样式失效，不能把预览当作实机效果。

要生成唯一最终版，所需最小原生样本：在当前Widgy新建Small小号，保留一个带JSON Endpoint的文本层；设置所需字体和背景后导出。以实际root尺寸标识和style字段替换生成器中的待验证候选，然后在手机检查字距/换行与鉴权200。无需透露真实凭据。

接口新增display_date、display_course_1_time/location、display_course_2_time/location、display_widget_footer；均由已发布的结构化数据确定性派生，没有新增Muse维护字段或模型任务。字段正好适配左右时间/地点列。display_courses_more由服务器计算。

不要使用之前的十行muse-today-dashboard.draft.json作为这个设计的最终模板。Watch/Live类型未验证，不能用手机候选冒充其他类型。
