import os
import json

# Try to import Anthropic – if it's missing, Claude will be disabled gracefully.
try:
    from anthropic import Anthropic
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if api_key:
        client = Anthropic(api_key=api_key)
        CLAUDE_ENABLED = True
    else:
        client = None
        CLAUDE_ENABLED = False
        print("⚠️ CLAUDE_API_KEY not set. Claude fallback disabled.")
except ImportError:
    client = None
    CLAUDE_ENABLED = False
    print("⚠️ anthropic package not installed. Claude fallback disabled. Install with: pip install anthropic")

async def call_claude(user_query: str) -> dict:
    """
    Send the user's query to Claude and return either:
    - {"type": "action", "data": {"intent": "...", "params": {...}}}
    - {"type": "text", "text": "..."}

    If Claude is not available, return a generic text message.
    """
    if not CLAUDE_ENABLED or client is None:
        return {
            "type": "text",
            "text": "I'm here to help! Try asking me to search for an item, book a service, or get store info."
        }

    system_prompt = """You are SEAI, the AI assistant for Admerce, a hyper‑local commerce platform.
You can help users with these actions:
- search for items near them (intent: search_items, params: {query: "..."})
- book a service (intent: book_service, params: {service: "..."})
- get info about a store (intent: get_store_info, params: {store: "..."})

If the user's request matches one of these actions, respond ONLY with a JSON object like:
{"intent": "search_items", "params": {"query": "pizza"}}
Otherwise, reply naturally with a friendly text answer.
Keep responses brief and helpful.
"""

    try:
        response = client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=150,
            system=system_prompt,
            messages=[{"role": "user", "content": user_query}],
        )
        content = response.content[0].text.strip()

        # Attempt to parse as JSON action
        try:
            action = json.loads(content)
            if "intent" in action:
                return {"type": "action", "data": action}
        except json.JSONDecodeError:
            pass

        # Otherwise, return as plain text
        return {"type": "text", "text": content}

    except Exception as e:
        print(f"❌ Claude error: {e}")
        return {
            "type": "text",
            "text": "I'm having trouble right now. Please try again."
        }