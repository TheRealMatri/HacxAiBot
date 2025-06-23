import os
import requests
import textwrap
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7796376668:AAG1WP53OpMkp0luDC4IxdaJVDg5tXXV6ao")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "tgp_v1_jpxmeYXld5n1xRlct8QSQQdwp6Z1fTx05e7qdxkZO0Q")
MODEL_NAME = "meta-llama/Llama-3-70b-chat-hf"
TOR_PROXY = "socks5h://localhost:9050"
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")  # Optional for better search results

# Load custom prompt
try:
    with open("prompt.txt", "r") as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    SYSTEM_PROMPT = "You are a helpful AI assistant. Always format code in markdown code blocks."

# Per-user state management
user_states = {}

# --- Search Functions ---
def brave_search(query: str, use_tor: bool = False):
    """Search using Brave API with Tor option"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}
    
    headers = {"Accept": "application/json"}
    if BRAVE_API_KEY:
        headers["X-Subscription-Token"] = BRAVE_API_KEY

    response = session.get(
        f"https://api.search.brave.com/res/v1/web/search?q={query}",
        headers=headers,
        timeout=15
    )
    return response.json().get("web", {}).get("results", [])[:3]

def duckduckgo_search(query: str, use_tor: bool = False):
    """Fallback search with DuckDuckGo"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}
    
    response = session.get(
        f"https://api.duckduckgo.com/?q={query}&format=json",
        timeout=10
    )
    return [{
        "title": result["Text"],
        "url": result["FirstURL"],
        "description": result.get("Text", "")
    } for result in response.json().get("Results", [])[:3]]

# --- AI Inference ---
async def generate_ai_response(prompt: str, user_id: int):
    """Generate response with Together.ai API"""
    # Get user state
    state = user_states.get(user_id, {"tor": False, "net": False})
    
    # Get web context if enabled
    web_context = ""
    if state["tor"] or state["net"]:
        try:
            results = brave_search(prompt, use_tor=state["tor"]) or duckduckgo_search(prompt, use_tor=state["tor"])
            web_context = "\n".join(
                f"🔍 [{res['title']}]({res['url']})\n{res.get('description', '')}"
                for res in results
            )
        except Exception as e:
            web_context = f"⚠️ Search failed: {str(e)}"

    # Prepare prompt
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    if web_context:
        messages.append({
            "role": "system",
            "content": f"## Web Context\n{web_context}"
        })
    
    messages.append({"role": "user", "content": prompt})

    # API call
    headers = {"Authorization": f"Bearer {TOGETHER_API_KEY}"}
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7
    }
    
    response = requests.post(
        "https://api.together.xyz/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=30
    )
    return response.json()["choices"][0]["message"]["content"]

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    user_id = update.effective_user.id
    user_states[user_id] = {"tor": False, "net": False}
    
    keyboard = [
        [InlineKeyboardButton("🔓 Tor ON", callback_data="tor_on"),
         InlineKeyboardButton("🌐 Clearnet ON", callback_data="net_on")],
        [InlineKeyboardButton("🔒 Tor OFF", callback_data="tor_off"),
         InlineKeyboardButton("🚫 Clearnet OFF", callback_data="net_off")]
    ]
    
    await update.message.reply_text(
        "🤖 Welcome to Anonymous AI Assistant\n\n"
        "Commands:\n"
        "/toron - Enable Tor search\n"
        "/toroff - Disable Tor\n"
        "/neton - Enable Clearnet search\n"
        "/netoff - Disable Clearnet\n\n"
        "Current status:\n"
        f"Tor: {'ON 🔓' if user_states[user_id]['tor'] else 'OFF 🔒'}\n"
        f"Clearnet: {'ON 🌐' if user_states[user_id]['net'] else 'OFF 🚫'}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def tor_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable Tor mode"""
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False}
    
    user_states[user_id]["tor"] = True
    user_states[user_id]["net"] = False  # Disable clearnet when Tor is enabled
    await update.message.reply_text("✅ Tor search ACTIVATED\nClearnet search disabled")

async def tor_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable Tor mode"""
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False}
    
    user_states[user_id]["tor"] = False
    await update.message.reply_text("🔒 Tor search DEACTIVATED")

async def net_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable Clearnet mode"""
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False}
    
    user_states[user_id]["net"] = True
    user_states[user_id]["tor"] = False  # Disable Tor when clearnet is enabled
    await update.message.reply_text("🌐 Clearnet search ACTIVATED\nTor search disabled")

async def net_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Disable Clearnet mode"""
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False}
    
    user_states[user_id]["net"] = False
    await update.message.reply_text("🚫 Clearnet search DEACTIVATED")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process user messages"""
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False}
    
    user_msg = update.message.text
    
    # Show typing indicator
    await update.message.reply_chat_action("typing")
    
    # Generate AI response
    try:
        response = await generate_ai_response(user_msg, user_id)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")
        return
    
    # Format for Telegram with code blocks
    formatted_response = ""
    in_code_block = False
    code_lang = ""
    
    for line in response.split("\n"):
        if line.startswith("```"):
            if in_code_block:
                formatted_response += f"```{code_lang}\n"
                in_code_block = False
            else:
                code_lang = line[3:].strip() or "python"
                formatted_response += f"```{code_lang}\n"
                in_code_block = True
        else:
            if in_code_block:
                formatted_response += line + "\n"
            else:
                wrapped = textwrap.fill(line, width=65)
                formatted_response += wrapped + "\n\n"
    
    # Send in chunks (Telegram has 4096 char limit)
    for i in range(0, len(formatted_response), 4090):
        await update.message.reply_text(
            formatted_response[i:i+4090],
            disable_web_page_preview=True
        )

# --- Main Application ---
def main():
    print("🤖 Starting AI Telegram Bot...")
    print(f"System prompt: {SYSTEM_PROMPT[:100]}...")
    
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("toron", tor_on))
    app.add_handler(CommandHandler("toroff", tor_off))
    app.add_handler(CommandHandler("neton", net_on))
    app.add_handler(CommandHandler("netoff", net_off))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    print("✅ Bot is running")
    app.run_polling()

if __name__ == "__main__":
    main()
