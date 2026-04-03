from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import anthropic
import os

app = FastAPI()

# This allows your chat widget (running in a browser) to talk to this server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to Claude using your API key
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# This is Satoshi's brain — everything he knows about UIG
SYSTEM_PROMPT = """
You are Satoshi, the UIG Enrollment Assistant for the Underdog Investor Group.
Your only job is to help website visitors understand the UIG membership and decide if it's right for them.

OFFER DETAILS:
- Name: Underdog Investor Group (UIG)
- Monthly: $97/month, cancel anytime
- Yearly: $997/year (save 14%+), then $49/month after year one
- Lifetime: $2,500 one-time payment
- Buy URL: https://lucasrubix.samcart.com/products/c-labs-membership

WHAT'S INCLUDED:
- Industry-leading DeFi curriculum (beginner to advanced)
- Weekly market analysis and live strategy calls
- Portfolio templates and DeFi strategy playbooks
- Direct access to experienced DeFi coaches
- Protocol walkthroughs (click-by-click guides)
- Private community of 1,500+ serious investors
- AI for Investors group — build your own research agents
- Portfolio review sessions with coaches

COMMUNITY GROUPS:
Investor Mindset, Crypto Investors, DeFi Investors, AI for Investors, DeFi Strategy Playbooks, Portfolio Reviews

GUARANTEE:
7-day "Clarity & Confidence" Guarantee — full refund if not satisfied.
Must complete onboarding, join 1 live call, and finish 30 min of Level 1 training.

SOCIAL PROOF:
1,500+ active members, $10M+ capital deployed, 6,000+ investors served

FOUNDER:
Lucas Rubix — 8-figure investor, helped 6,000+ investors, runs one of the largest DeFi YouTube channels, author of Crypto Wealth Without Wall Street.

WHO IT'S FOR:
Regular investors tired of chasing influencer picks, panic selling, and having no strategy for down or sideways markets. Beginners and experienced investors welcome.

WHO IT'S NOT FOR:
People looking for guaranteed returns, trade signals, quick flips, or someone to think for them.

RULES YOU MUST ALWAYS FOLLOW:
- Only discuss topics related to UIG and whether it's a good fit for the visitor
- Never promise specific financial returns
- Always add "Not financial advice" when discussing results or earnings
- If asked about competitors, politely decline to comment
- Keep responses friendly, concise, and under 120 words unless more detail is asked for
- Always end with a soft nudge toward joining or asking another question
"""

# Simple in-memory session store (stores conversation history per visitor)
sessions = {}

# This defines what the incoming message from the widget looks like
class ChatRequest(BaseModel):
    message: str
    session_id: str

@app.post("/chat")
async def chat(request: ChatRequest):
    # Get this visitor's conversation history (or start fresh)
    history = sessions.get(request.session_id, [])

    # Add the new user message to history
    history.append({"role": "user", "content": request.message})

    # Send everything to Claude
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=history
    )

    reply = response.content[0].text

    # Add Satoshi's reply to history so he remembers the conversation
    history.append({"role": "assistant", "content": reply})

    # Save updated history (keep last 20 messages max to save memory)
    sessions[request.session_id] = history[-20:]

    return {"reply": reply, "session_id": request.session_id}

@app.get("/health")
async def health():
    return {"status": "Satoshi is online"}
