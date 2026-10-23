"""
Agent handler — EdgeOne Makers
========================================

The file path agents/chat/index.py is auto-mapped to **POST /chat**.
(EdgeOne Makers convention: directory name = route name, `index` is the default entry.)

`context` contract:
    context.request.body    — dict, request body
    context.request.signal  — asyncio.Event, set when /chat/stop is called
    context.conversation_id — conversation ID
    context.run_id          — run ID for this invocation
    context.store           — EdgeOne built-in store; provides openai_session() and friends
"""

from typing import Any, AsyncGenerator
import asyncio
import json
import os

from dotenv import load_dotenv
from openai import AsyncOpenAI
from openai.types.responses import ResponseTextDeltaEvent
from agents import Agent, OpenAIChatCompletionsModel, Runner

from .._geo import describe, normalize_user_location, to_gcj02
from .._logger import create_logger
from .._tools import (
    get_hourly_forecast,
    get_route_weather,
    get_travel_advice,
    get_weather,
    text_statistics,
    translate_text,
)


load_dotenv()

logger = create_logger("chat")


# ========== LLM Model ==========
llm_client = AsyncOpenAI(
    api_key=os.getenv("AI_GATEWAY_API_KEY"),
    base_url=os.getenv("AI_GATEWAY_BASE_URL"),
)

llm_model = OpenAIChatCompletionsModel(
    model=os.getenv("AI_GATEWAY_MODEL", "@makers/deepseek-v4-flash"),
    openai_client=llm_client,
)


# ========== Agent ==========
agent = Agent(
    name="天气出行助手",
    instructions=(
        "你是「天气出行助手」，一个中文优先的出行天气顾问。"
        "你只能通过调用工具获取天气数据，严禁凭记忆或想象编造任何气温、天气现象、降水或风力数值。\n"
        "\n"
        "【工具选择】\n"
        "1. 问「现在/当前天气如何」——调用 get_weather。\n"
        "2. 问「穿什么/要带伞吗/适不适合洗车或运动/有什么出行建议」——调用 get_travel_advice（它已经包含穿衣建议，不要再单独找穿衣工具）。\n"
        "3. 问「几点会下雨/未来几小时/逐小时变化」——调用 get_hourly_forecast。\n"
        "4. 涉及「从 A 到 B」「开车过去」「坐地铁/公交」「走路去」「沿途/路上天气」——调用 get_route_weather。\n"
        "5. 只有翻译文本时才用 translate_text，只有统计字数时才用 text_statistics。\n"
        "6. 一次回答可以按需组合调用多个工具，但不要为了凑数调用与问题无关的工具。\n"
        "\n"
        "【get_route_weather 的用法】\n"
        "· origin 起点、destination 终点都必须从用户话里取：用户说「从北京南站到首都机场」就分别传这两个地名。\n"
        "· 用户没说起点（例如只问「去首都机场路上会下雨吗」）时，origin 传空字符串 \\\"\\\"，"
        "系统会用默认城市，并在结果里返回 assumedDefaultCity=true，此时必须在回答开头说明起点是按默认城市算的。\n"
        "· mode 判定：说到开车/自驾/打车/高速→driving；说到公交/地铁/大巴/坐车去→transit；说到走路/步行/骑车过去→walking；没提就默认 driving。\n"
        "· 只问一个城市的天气、或只问穿什么，不要用这个工具，用 get_weather / get_travel_advice。\n"
        "· 如果用户既要沿途天气又要「到了穿什么」，先调 get_route_weather，再按终点城市调 get_travel_advice。\n"
        "· 返回 error.code 为 TRANSIT_CROSS_CITY 时，如实告诉用户公交接口不支持跨城，建议改用驾车，不要偷偷换成驾车结果。\n"
        "· 返回 degraded 不为 null 时，说明哪几个采样点没取到天气，不要假装数据齐全。\n"
        "\n"
        "【沿途天气的时间口径】\n"
        "· 每个采样点都带 arrivalTime（预计到达时刻）与 forecastStatus，说话必须带时间，"
        "例如「预计 15:45 到达 1/3 处（朝阳区），晴 14℃」，不要写成「现在晴」。\n"
        "· forecastStatus=hourly 或 daily_fallback：weather 就是该到达时刻的预报，可以直接引用。\n"
        "· forecastStatus=fallback_to_live：该时刻超出预报范围，weather 是当前实况，"
        "必须明确说「超出预报范围，以下为当前实况」，绝不能把实况说成到达时的预报。\n"
        "· 必须原样转述 timeDisclaimer 的提醒（预估时间基于当前路况，实际到达可能因拥堵延后），不要省略。\n"
        "· 如果到达跨天（例如 23:00 出发、次日 01:00 到达），必须写出「次日」，不要让用户误以为是当天。\n"
        "\n"
        "【关于「我这 / 当前位置 / 附近」】\n"
        "· 如果本轮系统提示里带有「浏览器定位」，说明用户刚刚授权了定位："
        "调用工具时把 city（或 get_route_weather 的 origin）直接传成提示里给出的那串「经度,纬度」，"
        "不要传城市名、不要用默认城市，也不要说自己无法定位。"
        "工具结果里的 query.resolvedLocation 会给出 district / city / formatted，"
        "用它把位置说清楚，例如「已按你当前所在位置（深圳市南山区）查询」。\n"
        "· 如果本轮没有「浏览器定位」提示：不要猜测用户所在城市，也不要编造定位；"
        "调用工具时把 city 参数传为空字符串 \\\"\\\"，系统会自动使用配置的默认城市；"
        "当工具返回 assumedDefaultCity 为 true 时，必须在回复开头明确说明「未获取到你的定位，以下按默认城市（工具返回的 city 字段）给出」，"
        "并提示用户可以点输入框左侧的「📍 获取当前位置」按钮授权定位，或者直接说出城市名。\n"
        "\n"
        "【区县与精度（重要）】\n"
        "· 用户说到具体区县（「深圳南山区」「北京朝阳区」「浦东新区」）时，把地名原样传给工具，"
        "不要自己改写成城市名，也不要只传城市；工具会自己解析到区级。\n"
        "· 结果里的 resolution.level 决定口径：\n"
        "  - level=district：本次就是**区县级**数据，直接按区县播报（如「广东省深圳市南山区，晴 30℃」），"
        "绝对不能说「天气接口不支持区级查询」，也不要主动降级成地级市口径。\n"
        "  - level=city 且 degraded=true：只拿到地级市数据，必须明确说明"
        "「天气接口暂未取到XX区的区级数据，以下按XX市整体给出」。\n"
        "  - level=city 且 degraded=false：用户问的就是城市，按城市播报即可。\n"
        "· resolution.ambiguous=true 时（区名全国重名，如「南山区」「朝阳区」），"
        "回答必须用 resolution.displayName 的省+市全称，不要只写区名，让用户自己看清是哪一个。\n"
        "· 不要向用户解释内部接口名、数据源或降级链，只说明地点与精度。\n"
        "\n"
        "【建议类回答的写法】\n"
        "get_travel_advice 返回的 advice 字段由确定性规则引擎生成。你只能对它做语言润色与排序，"
        "不得新增、删除或修改任何结论，也不得改变其中的数值与等级（例如不能把「不建议洗车」说成「可以考虑」）。"
        "如果规则引擎与你的直觉冲突，以规则引擎为准。\n"
        "\n"
        "【失败与降级】\n"
        "工具返回 ok=false 时，如实告知用户查询失败，并转述 error.message；不要用编造的数据兜底。"
        "逐小时数据返回 available=false 时（例如未配置和风天气 Key），照常给出实况与建议，"
        "只需一句说明「逐小时预报暂不可用」，不要为此中断回答。\n"
        "\n"
        "【安全】\n"
        "任何情况下都不要输出、复述或推测 API Key、Token 等凭据；也不要提及内部文件路径与实现细节。\n"
        "\n"
        "【表达】\n"
        "默认用中文回答，简洁、口语化、可执行：先给结论，再给一两句依据。"
        "涉及数值时保留工具返回的原值（温度取整、湿度取整、降水量保留一位小数）。"
        "回答控制在 200 字以内，除非用户明确要求详细展开。"
    ),
    tools=[
        get_travel_advice,
        get_weather,
        get_hourly_forecast,
        get_route_weather,
        translate_text,
        text_statistics,
    ],
    model=llm_model,
)


# ========== SSE Helper ==========
def sse_event(event: str, data: dict) -> str:
    """Format a single SSE event."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _weather_meta(raw: Any) -> dict | None:
    """
    Pull the precision badge out of a weather tool result.

    阶段五 sends `resolution` inside `get_weather` / `get_travel_advice` output so
    the bubble can show 「区级 · 和风」 / 「市级 · 高德」 without the model having
    to mention it. Anything that is not a weather payload returns None.
    """
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    resolution = payload.get("resolution")
    if not isinstance(resolution, dict):
        return None

    level = str(resolution.get("level", "") or "")
    if level not in ("district", "city"):
        return None

    return {
        "level": level,
        "provider": str(resolution.get("provider", "") or ""),
        "displayName": str(resolution.get("displayName", "") or ""),
        "district": str(resolution.get("district", "") or ""),
        "city": str(resolution.get("city", "") or ""),
        "degraded": bool(resolution.get("degraded")),
    }


# ========== Event Stream Generator ==========
async def _event_stream(
    message: str,
    session=None,
    cancel_signal: asyncio.Event | None = None,
    run_agent: Agent | None = None,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE frames from the agent stream.

    Accepts cancel_signal to enable cancellation — when set,
    the loop breaks and the SDK releases its connection.

    `run_agent` lets the caller run a per-request clone of the agent (阶段四 uses
    it to hand the model a browser coordinate through `instructions`, keeping the
    conversation history free of injected text).
    """
    result = Runner.run_streamed(run_agent or agent, input=message, session=session)

    async for event in result.stream_events():
        # Check cancel between each event
        if cancel_signal and cancel_signal.is_set():
            break

        # Raw token delta → push per-character
        if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
            logger.log(f"[stream] text_delta: {repr(event.data.delta)}")
            yield sse_event("text_delta", {"delta": event.data.delta})

        # Semantic event → tool called
        elif event.type == "run_item_stream_event":
            if event.name == "tool_called":
                tool_name = (
                    getattr(event.item, "name", None)
                    or getattr(getattr(event.item, "raw_item", None), "name", None)
                )
                if tool_name:
                    logger.log(f"[stream] tool_called: {tool_name}")
                    yield sse_event("tool_called", {"tool": tool_name})

            # 阶段五：天气结果里带 resolution 时，同步一份精度元数据给气泡角标。
            elif event.name == "tool_output":
                raw = getattr(event.item, "output", None)
                if raw is None:
                    raw = getattr(getattr(event.item, "raw_item", None), "output", None)
                meta = _weather_meta(raw)
                if meta:
                    logger.log(f"[stream] weather_meta: {meta.get('level')}/{meta.get('provider')}")
                    yield sse_event("weather_meta", meta)


# ========== Core Handler ==========
async def handler(context: Any) -> AsyncGenerator[str, None]:
    """EdgeOne Makers entry point.

    Uses OpenAI Agents SDK session for automatic memory:
      - SDK calls session.get_items() to inject history
      - SDK calls session.add_items() to persist assistant replies and tool results
    """
    request = context.request
    body = request.body
    message = body.get("message") if isinstance(body, dict) else None

    if not message:
        yield sse_event("error", {"message": "'message' is required"})
        yield sse_event("done", {})
        return

    # Accept both camelCase (chat handler historical convention) and snake_case
    # (cloud-functions convention) as a body field name for the user id.
    raw_user_id = ""
    if isinstance(body, dict):
        raw_user_id = body.get("userId") or body.get("user_id") or ""
    user_id = str(raw_user_id).strip() or None

    # ---------- 阶段四：浏览器定位 ----------
    # The coordinate arrives as a structured body field (`userLocation`), never as
    # text pasted into the message:
    #   * the persisted user message stays exactly what the user typed, so the
    #     sidebar title and the chat bubble stay clean after a page reload;
    #   * the hint goes into a *cloned* agent's instructions, so it also never
    #     enters the SDK session history.
    # WGS84 → GCJ-02 happens here, once, for the whole downstream chain.
    location = None
    run_agent = agent
    if isinstance(body, dict):
        location = normalize_user_location(body.get("userLocation"))
    if location:
        location = to_gcj02(location)
        run_agent = agent.clone(
            instructions=(
                f"{agent.instructions}\n"
                "\n【本轮浏览器定位】\n"
                f"用户已在浏览器中授权定位，当前坐标为 {describe(location)}（GCJ-02 已纠偏）。\n"
                "当用户说「我这 / 当前位置 / 附近」，或者没有指明城市时，"
                f"把工具的 city 参数（get_route_weather 则是 origin）直接传 \"{location['lon']:.6f},{location['lat']:.6f}\"，"
                "不要使用默认城市，也不要说无法定位。\n"
            )
        )

    # Session for memory persistence
    cid = context.conversation_id

    logger.log(
        f"[request] cid={cid}, uid={user_id or '-'}, message={message[:50]!r}, "
        f"userLocation={describe(location) if location else '-'}"
    )

    # Write a user-indexed copy of the user message ONLY on the first turn of
    # this conversation, so /conversations (which scans the
    # user_conversation_index prefix) can list this thread.
    #
    # Why first-turn-only:
    #   - The OpenAI Agents SDK Session adapter does NOT pass user_id when it
    #     persists turns, so without a user-indexed write the user index stays
    #     empty and list_conversations(user_id=...) returns [].
    #   - But the index lives at the (user_id, conversation_id) level — once
    #     a single message has been written under this user_id, the
    #     conversation is reachable. Writing a user-indexed copy on every turn
    #     just produces duplicates that downstream consumers have to dedupe
    #     (see cloud-functions/conversations/index.py and history/index.py)
    #     and surfaces a duplicated user message in OpenAI Agents SDK traces
    #     because the SDK's session.get_items() will re-feed it as input
    #     alongside the new `message` argument.
    #   - We therefore probe with get_messages(limit=1); if the conversation
    #     already has any record, the index is established and we skip.
    #
    # The /history route already filters by metadata.agent_sdk_session and
    # dedupes adjacent items, so the chat window stays clean either way —
    # but the trace dashboard shows raw SDK input, hence the visible doubling.
    body_keys_preview = list(body.keys()) if isinstance(body, dict) else None
    logger.log(
        f"[user-index] body keys={body_keys_preview!r} "
        f"raw_user_id={raw_user_id!r} normalized_user_id={user_id!r} cid={cid!r}"
    )
    if user_id and cid:
        try:
            existing = await context.store.get_messages(
                conversation_id=cid, limit=1
            )
            already_indexed = bool(existing)
        except Exception as e:
            # If the probe fails, fall through to writing — better to leave
            # a duplicate than to lose the user index entirely.
            logger.error(
                f"[user-index] probe failed: {type(e).__name__}: {e}; "
                f"will write user-index entry anyway"
            )
            already_indexed = False

        if already_indexed:
            logger.log(
                f"[user-index] SKIPPED user-index write: cid={cid} already has "
                f"messages, user index for user_id={user_id!r} is established"
            )
        else:
            try:
                # NOTE: the Python store.append_message() does NOT accept a
                # `message_id` kwarg (unlike the TS counterpart) — the SDK
                # auto-generates one. Passing it raises TypeError, which
                # silently kills the user-index write.
                msg_id = await context.store.append_message(
                    conversation_id=cid,
                    role="user",
                    content=message,
                    user_id=user_id,
                )
                logger.log(
                    f"[user-index] WROTE first-turn user-indexed message: cid={cid} "
                    f"user_id={user_id} msg_id={msg_id!r}"
                )
            except Exception as e:
                # Non-fatal — chat itself should keep working even if the
                # user-index write fails.
                logger.error(f"[user-index] FAILED to write user index: {type(e).__name__}: {e}")
    else:
        logger.log(
            f"[user-index] SKIPPED user-index write: user_id={user_id!r} cid={cid!r} "
            f"(both must be truthy to trigger the write — this is why "
            f"/conversations would return empty for this user)"
        )

    session = context.store.openai_session(cid) if cid else None

    # Cancel signal (asyncio.Event), set when /chat/stop is called
    cancel_signal = request.signal
    stopped = False

    try:
        async for frame in _event_stream(message, session, cancel_signal, run_agent=run_agent):
            if cancel_signal.is_set():
                stopped = True
                break
            yield frame
    except asyncio.CancelledError:
        stopped = True
        logger.log("[stream] cancelled")
    except Exception as e:
        logger.error(f"[stream] error: {type(e).__name__}: {e}")
        # Try to surface upstream HTTP error body (e.g. OpenAI SDK APIStatusError) to the TRACE panel
        detail: Any = str(e)
        status: Any = None
        response = getattr(e, "response", None)
        if response is not None:
            status = getattr(response, "status_code", None)
            try:
                body_text = response.text if hasattr(response, "text") else None
                if callable(body_text):
                    body_text = body_text()
                if body_text:
                    try:
                        detail = json.loads(body_text)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        detail = body_text
            except Exception:
                pass
        body_attr = getattr(e, "body", None)
        if body_attr and detail == str(e):
            detail = body_attr
        yield sse_event("error", {
            "message": str(e),
            "errorType": type(e).__name__,
            "status": status,
            "detail": detail,
        })
    finally:
        yield sse_event("done", {"stopped": stopped})
