export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  /**
   * True while the assistant is actively producing this message
   * (between the first text_delta and the final done/error event).
   * Drives the in-bubble blinking caret to give the user feedback
   * that more content is still streaming. Cleared once done/error fires.
   */
  streaming?: boolean;
  /**
   * 阶段五：本条回答所用天气数据的精度档位，由后端 `weather_meta` SSE 事件带下来。
   * 只用于气泡下角标展示「区级 · 和风」/「市级 · 高德」，不参与推理。
   */
  weatherMeta?: WeatherMeta;
}

/**
 * Precision badge payload (阶段五).
 *
 * `level` is what the data actually is, `provider` is where it came from.
 * `degraded` is true only when the user asked for a district and the chain fell
 * back to the prefecture-level city — that is the case the text must announce.
 */
export interface WeatherMeta {
  level: 'district' | 'city';
  provider: 'qweather' | 'amap' | string;
  displayName: string;
  district?: string;
  city?: string;
  degraded: boolean;
}

export interface ToolLampState {
  id: string;
  label: string;
  icon: string;
  active: boolean;
  animKey: number;   // Incremented on each activation to remount and replay animation
}

/**
 * One browser position fix (阶段四).
 *
 * `coordSys` is always 'wgs84' here — the browser's datum. The backend converts
 * it to GCJ-02 once (`agents/_geo.py`) before any Amap call.
 */
export interface GeoPoint {
  lon: number;
  lat: number;
  /** Horizontal accuracy in metres, as reported by the browser. */
  accuracy?: number;
  coordSys: 'wgs84';
  /** Epoch ms when the fix was taken (used for the client-side cache TTL). */
  ts: number;
}

/**
 * Lightweight summary of a conversation, returned by /conversations.
 * Used to render the left sidebar — does NOT contain full message content.
 */
export interface ConversationSummary {
  id: string;
  title: string;
  preview?: string;
  lastMessageAt?: number;
  createdAt?: number;
  userId?: string;
  messageCount?: number;
}

export interface ListConversationsParams {
  userId: string;
  limit?: number;
  order?: 'asc' | 'desc';
  after?: string;
  before?: string;
}

export interface ListConversationsResponse {
  conversations: ConversationSummary[];
  nextCursor?: string;
  previousCursor?: string;
}
