const zh = {
  // Header
  "app.title": "天气出行助手",
  "app.subtitle": "高德实况 + 和风逐小时 · 规则引擎生成出行建议",

  // Empty state
  "empty.title": "天气出行助手",
  "empty.hint": "我可以查实时天气和逐小时预报，并给出穿衣、带伞、防晒、洗车、户外运动等出行建议。试试「北京现在天气怎么样」，或者直接问「我这要带伞吗」。",
  "empty.features": "高德地图 · 和风天气 · 规则引擎建议 · 会话记忆",

  // Chat input
  "chat.placeholder": "发消息…  ⏎ 发送 · Shift+⏎ 换行",
  "chat.hint": "数据来源：高德地图 / 和风天气 · 建议由确定性规则引擎生成",

  // Preset questions
  "preset.1": "北京现在天气怎么样，穿什么合适？",
  "preset.2": "我这要带伞吗？适合户外运动吗？",

  // Tool indicators
  "tool.weather": "实况天气",
  "tool.hourly": "逐小时",
  "tool.travel": "出行建议",
  "tool.translate": "文本翻译",
  "tool.statistics": "文本统计",

  // Status & errors
  "status.error": "⚠️ 请求失败，请检查后端服务是否启动。",
  "status.stopped": "⏹ *已停止生成*",
  "status.backendError": "⚠️ 后端中断请求失败，服务端可能仍在运行。",

  // Debug panel
  "debug.title": "传输流",
  "debug.events": "事件",
  "debug.clear": "清除",
  "debug.empty": "等待 SSE 事件...",
  "debug.emptyHint": "发送消息后，所有原始后端数据将在此处显示。",

  // Conversation sidebar
  "sidebar.label": "会话列表",
  "sidebar.title": "会话",
  "sidebar.newChat": "新建聊天",
  "sidebar.loading": "正在加载会话...",
  "sidebar.loadMore": "加载更多",
  "sidebar.loadingMore": "加载中...",
  "sidebar.emptyTitle": "暂无会话",
  "sidebar.emptyHint": "点击「新建聊天」开始第一段对话。",
  "sidebar.delete": "删除会话",
  "sidebar.deleteConfirm": "确定要永久删除这个会话吗？此操作不可恢复。",

  // Aria labels (button hover/screen-reader)
  "aria.send": "发送",
  "aria.clearHistory": "清除历史",
  "aria.stopGeneration": "停止生成",

  // Language toggle
  "lang.switch": "English",

  // ─── Floating bottom-right action badges ─────────────────────────────
  "floatingLink.deploy": "一键部署",
  "floatingLink.github": "GitHub",
} as const;

export default zh;
