const en = {
  // Header
  "app.title": "Weather Travel Assistant",
  "app.subtitle": "Amap live + QWeather hourly · rule-engine travel advice",

  // Empty state
  "empty.title": "Weather Travel Assistant",
  "empty.hint": "I can look up live weather and hourly forecasts, then give you actionable travel advice — what to wear, whether to bring an umbrella, sun protection, car washing, outdoor sports. Try \"What's the weather in Beijing?\" or just ask \"Do I need an umbrella?\"",
  "empty.features": "Amap · QWeather · Rule engine · Session memory",

  // Chat input
  "chat.placeholder": "Type a message...  ⏎ Send · Shift+⏎ Newline",
  "chat.hint": "Data: Amap / QWeather · Advice produced by a deterministic rule engine",

  // Preset questions
  "preset.1": "What's the weather in Beijing right now? What should I wear?",
  "preset.2": "Do I need an umbrella? Is it good for outdoor sports?",

  // Tool indicators
  "tool.weather": "Live weather",
  "tool.hourly": "Hourly",
  "tool.travel": "Travel advice",
  "tool.route": "Route weather",
  "tool.translate": "Translate",
  "tool.statistics": "Statistics",

  // Status & errors
  "status.error": "Request failed. Please check if the backend service is running.",
  "status.stopped": "⏹ *Generation stopped*",
  "status.backendError": "Backend abort request failed. The server may still be running.",

  // Debug panel
  "debug.title": "Trace",
  "debug.events": "events",
  "debug.clear": "Clear",
  "debug.empty": "Waiting for SSE events...",
  "debug.emptyHint": "After sending a message, all raw backend data will be displayed here.",

  // Conversation sidebar
  "sidebar.label": "Conversation list",
  "sidebar.title": "Chats",
  "sidebar.newChat": "New chat",
  "sidebar.loading": "Loading conversations...",
  "sidebar.loadMore": "Load more",
  "sidebar.loadingMore": "Loading...",
  "sidebar.emptyTitle": "No conversations yet",
  "sidebar.emptyHint": "Click \"New chat\" to start your first conversation.",
  "sidebar.delete": "Delete conversation",
  "sidebar.deleteConfirm": "Permanently delete this conversation? This cannot be undone.",

  // Geolocation (阶段四)
  "geo.button": "📍 Use my location",
  "geo.locating": "Locating…",
  "geo.retry": "📍 Locate again",
  "geo.ready": "Located",
  "geo.clear": "Clear location",
  "geo.accuracy": "acc.",
  "geo.hintLocated": "Location saved — it will be attached to the next message. Just ask \"how's the weather here?\".",
  "geo.error.denied": "⚠️ Location permission denied — please type a city instead",
  "geo.error.insecure": "⚠️ Geolocation needs https or localhost — please type a city instead",
  "geo.error.unavailable": "⚠️ Location service unavailable — please type a city instead",
  "geo.error.timeout": "⚠️ Location request timed out — retry or type a city instead",
  "geo.error.unsupported": "⚠️ This browser does not support geolocation — please type a city instead",
  "geo.error.unknown": "⚠️ Could not get your location — please type a city instead",
  "weatherMeta.district": "District",
  "weatherMeta.city": "City",
  "weatherMeta.qweather": "QWeather",
  "weatherMeta.amap": "Amap",
  "weatherMeta.degraded": "District unavailable, city-level used",
  "weatherMeta.title": "Precision of the weather data in this answer",

  // Aria labels (button hover/screen-reader)
  "aria.send": "Send",
  "aria.clearHistory": "Clear history",
  "aria.stopGeneration": "Stop generation",
  "aria.geolocate": "Use my location",

  // Language toggle
  "lang.switch": "中文",

  // ─── Floating bottom-right action badges ─────────────────────────────
  "floatingLink.deploy": "Deploy",
  "floatingLink.github": "GitHub",
} as const;

export default en;
