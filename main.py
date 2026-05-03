from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os
import re
import time
import httpx

app = FastAPI()

# Allow the WordPress widget to call this server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to Claude
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Slack config
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID")

# Notion config
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_PAGE_ID = os.environ.get("NOTION_PAGE_ID")
NOTION_VERSION = "2022-06-28"

# Cache for the Notion brain so we don't fetch it on every single message
# Refreshes automatically every 60 seconds
_brain_cache = {"text": None, "fetched_at": 0}
BRAIN_CACHE_SECONDS = 60

# Fallback brain if Notion is ever unreachable — keeps Satoshi alive even if the
# integration breaks. Update Notion to make real changes.
FALLBACK_BRAIN = """
You are Satoshi, the UIG Enrollment Assistant for the Underdog Investor Group.
Your job is to help website visitors decide if UIG or Fast Track is right for them.

Be warm, conversational, never pushy. Keep responses 2-4 sentences max.
Get the visitor's name early. Ask for email when they seem interested or hesitant.
When someone says they're in, send the link and stop selling.

UIG: $97/month, $997/year, $2,500 lifetime
Buy: https://lucasrubix.samcart.com/products/c-labs-membership

Fast Track: 3-12 months, $4,000-$25,000
Apply: https://cryptolabsresearch.com/fasttrackapplication

Free content: https://www.youtube.com/@CryptolabsResearch
Cancel: https://application743432.typeform.com/to/ZoaGeplB
Support: info@cryptolabsresearch.com

If asked if you're a real person, be honest — you're an AI agent built for UIG.
Never promise specific returns. Never mention competitors by name.
For cancellations or refunds, get name + email and tell them the team will help.
"""


async def fetch_notion_brain():
    """Pull the latest Satoshi brain from the Notion page."""
    if not NOTION_TOKEN or not NOTION_PAGE_ID:
        return FALLBACK_BRAIN

    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            # Notion paginates blocks — we collect them all
            all_blocks = []
            cursor = None
            while True:
                url = f"https://api.notion.com/v1/blocks/{NOTION_PAGE_ID}/children?page_size=100"
                if cursor:
                    url += f"&start_cursor={cursor}"
                resp = await http.get(url, headers=headers)
                if resp.status_code != 200:
                    print(f"Notion fetch failed: {resp.status_code} {resp.text}")
                    return FALLBACK_BRAIN
                data = resp.json()
                all_blocks.extend(data.get("results", []))
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")

        return blocks_to_text(all_blocks)
    except Exception as e:
        print(f"Notion error: {e}")
        return FALLBACK_BRAIN


def blocks_to_text(blocks):
    """Convert Notion blocks into plain text the LLM can read."""
    lines = []
    for block in blocks:
        btype = block.get("type")
        content = block.get(btype, {})
        rich = content.get("rich_text", [])
        text = "".join(r.get("plain_text", "") for r in rich)

        if btype == "heading_1":
            lines.append(f"\n# {text}\n")
        elif btype == "heading_2":
            lines.append(f"\n## {text}\n")
        elif btype == "heading_3":
            lines.append(f"\n### {text}\n")
        elif btype == "bulleted_list_item":
            lines.append(f"- {text}")
        elif btype == "numbered_list_item":
            lines.append(f"- {text}")
        elif btype == "to_do":
            checked = "[x]" if content.get("checked") else "[ ]"
            lines.append(f"{checked} {text}")
        elif btype == "quote":
            lines.append(f"> {text}")
        elif btype == "code":
            lines.append(f"\n{text}\n")
        elif btype == "divider":
            lines.append("\n---\n")
        elif btype == "paragraph":
            lines.append(text)
        elif text:
            lines.append(text)

    return "\n".join(lines).strip()


async def get_brain():
    """Get the current Satoshi brain, using a 60-second cache."""
    now = time.time()
    if (
        _brain_cache["text"]
        and now - _brain_cache["fetched_at"] < BRAIN_CACHE_SECONDS
    ):
        return _brain_cache["text"]

    brain = await fetch_notion_brain()
    _brain_cache["text"] = brain
    _brain_cache["fetched_at"] = now
    return brain


# In-memory session store
sessions = {}
# Maps session_id -> Slack thread timestamp + visitor short_id + name
slack_threads = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str


def detect_name(text: str):
    """Extract a likely first name from a message. Case-insensitive."""
    blocklist = {
        "perfect", "great", "awesome", "thanks", "thank", "okay", "ok", "cool",
        "yes", "yeah", "yep", "no", "nope", "sure", "alright", "got", "nice",
        "hi", "hey", "hello", "satoshi", "lucas", "the", "a", "an", "and",
        "but", "well", "so", "actually", "really", "totally", "honestly",
        "understood", "appreciate", "love", "good", "right", "exactly",
    }

    patterns = [
        r"(?:my name is|i'?m|im|this is|it'?s|name'?s|call me)\s+([A-Z][a-zA-Z]{1,20})",
        r"(?:nice to meet you|hey|hi|hello|got it|understood|appreciate that|so|okay|alright|cool|thanks|perfect|great)[,\s]+([A-Z][a-zA-Z]{1,20})[,\.\!\?]",
        r"^([A-Z][a-zA-Z]{1,20})$",
    ]

    for pat in patterns:
        match = re.search(pat, text, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip().title()
            if candidate.lower() not in blocklist and len(candidate) >= 2:
                return candidate
    return None


def detect_email(text: str):
    match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return match.group(0) if match else None


async def generate_summary(history):
    """One-line summary of the conversation for the Slack thread header."""
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=(
                "Summarise this sales chat in one line for a Slack thread header.\n"
                "Format: [Name if known] · [what they want] · [email if shared]\n"
                "Example: Lucas · Interested in Fast Track · Has capital · lucas@email.com\n"
                "Keep under 15 words. Use 'Visitor' if no name given."
            ),
            messages=[{"role": "user", "content": f"Summarise: {str(history[-8:])}"}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"Summary error: {e}")
        return None


async def post_to_slack_thread(http, text: str, thread_ts: str = None):
    """Post a message to Slack, optionally inside a thread."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return None
    payload = {"channel": SLACK_CHANNEL_ID, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    resp = await http.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json=payload,
    )
    return resp.json()


async def update_thread_header(http, thread_ts: str, header_text: str):
    """Update the opening message of a Slack thread."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return
    await http.post(
        "https://slack.com/api/chat.update",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={"channel": SLACK_CHANNEL_ID, "ts": thread_ts, "text": header_text},
    )


async def send_to_slack(session_id: str, user_msg: str, bot_reply: str, history: list):
    """Post each exchange to Slack as a clean threaded conversation."""
    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        return

    short_id = session_id[-6:] if len(session_id) >= 6 else session_id
    thread_info = slack_threads.get(session_id)

    async with httpx.AsyncClient(timeout=10.0) as http:
        # First message from this visitor — open a new thread
        if not thread_info:
            header = f"⚡ *New visitor* · `visitor-{short_id}`"
            resp = await post_to_slack_thread(http, header)
            if resp and resp.get("ok"):
                thread_ts = resp["ts"]
                slack_threads[session_id] = {
                    "ts": thread_ts,
                    "short_id": short_id,
                    "name": None,
                    "email": None,
                    "msg_count": 0,
                }
                thread_info = slack_threads[session_id]

        if not thread_info:
            return

        # Post the exchange into the thread
        body = f"👤 {user_msg}\n🤖 {bot_reply}\n─────────────────"
        await post_to_slack_thread(http, body, thread_ts=thread_info["ts"])

        thread_info["msg_count"] += 1

        # Detect name from either side of the conversation
        name_from_user = detect_name(user_msg)
        name_from_bot = detect_name(bot_reply)
        if not thread_info["name"]:
            if name_from_user:
                thread_info["name"] = name_from_user
            elif name_from_bot:
                thread_info["name"] = name_from_bot

        # Detect email
        email_found = detect_email(user_msg)
        if email_found and not thread_info["email"]:
            thread_info["email"] = email_found

        # Update thread header every 2 exchanges with an AI-generated summary
        if thread_info["msg_count"] % 2 == 0:
            summary = await generate_summary(history)
            if summary:
                header_text = f"⚡ *{summary}*\n`visitor-{thread_info['short_id']}`"
                await update_thread_header(http, thread_info["ts"], header_text)
        elif thread_info["name"]:
            # Quick header update with just the name as soon as we have it
            header_text = (
                f"⚡ *{thread_info['name']}* · `visitor-{thread_info['short_id']}`"
            )
            await update_thread_header(http, thread_info["ts"], header_text)


@app.post("/chat")
async def chat(body: ChatRequest):
    history = sessions.get(body.session_id, [])
    history.append({"role": "user", "content": body.message})

    # Pull the latest Satoshi brain from Notion (cached for 60s)
    system_prompt = await get_brain()

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=system_prompt,
        messages=history,
    )
    reply = response.content[0].text

    history.append({"role": "assistant", "content": reply})
    sessions[body.session_id] = history[-20:]

    # Mirror the conversation to Slack
    try:
        await send_to_slack(body.session_id, body.message, reply, history)
    except Exception as e:
        print(f"Slack error: {e}")

    return {"reply": reply, "session_id": body.session_id}


@app.get("/health")
async def health():
    return {"status": "Satoshi is online"}


@app.get("/brain/refresh")
async def refresh_brain():
    """Manually clear the brain cache so Notion edits show up immediately."""
    _brain_cache["text"] = None
    _brain_cache["fetched_at"] = 0
    brain = await get_brain()
    return {"status": "refreshed", "length": len(brain) if brain else 0}


@app.get("/brain/preview")
async def preview_brain():
    """See exactly what Satoshi is reading from Notion right now."""
    brain = await get_brain()
    return {"brain": brain}
