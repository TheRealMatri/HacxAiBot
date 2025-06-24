import os
import requests
import json
import time
import re
from datetime import datetime
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, MessageHandler,
                          ContextTypes, filters, CallbackQueryHandler)
import threading
import psutil
from ratelimit import limits, sleep_and_retry

# Configuration
TELEGRAM_TOKEN = "7796376668:AAG1WP53OpMkp0luDC4IxdaJVDg5tXXV6ao"
TOGETHER_API_KEY = "tgp_v1_jpxmeYXld5n1xRlct8QSQQdwp6Z1fTx05e7qdxkZO0Q"
MODEL_NAME = "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"

# Load custom prompt
try:
    with open("prompt.txt", "r") as f:
        SYSTEM_PROMPT = f.read().strip()
except FileNotFoundError:
    SYSTEM_PROMPT = ("You are a helpful AI assistant. Be concise - limit responses to 5-7 sentences. "
                     "Always format code in markdown code blocks. "
                     "When providing links, ensure they are real and clickable using markdown format. "
                     "You can fetch content from webpages when provided with URLs. "
                     "Maintain context from the conversation history to provide coherent responses. "
                     "IMPORTANT: When web search is enabled, use real-time internet data to answer questions. "
                     "Always verify URLs are functional and include them in responses when possible. "
                     "When citing sources, provide the actual URL using markdown formatting: [Title](URL). "
                     "Pay special attention to finding download links and forum content when requested.")

# Per-user state management
user_states = {}

# Global variables for status tracking
start_time = datetime.now()
memory_usage = 0
request_count = 0
last_search_time = None
last_fetch_time = None
api_call_count = 0
last_api_reset = datetime.now()

# --- Rate Limiting Decorator ---
@sleep_and_retry
@limits(calls=60, period=60)
def rate_limited_api_call():
    """Ensure we don't exceed 60 API calls per minute"""
    pass

# --- Enhanced Search Functions ---
def duckduckgo_search(query: str):
    """Search with DuckDuckGo"""
    try:
        # API request
        api_response = requests.get(
            "https://api.duckduckgo.com/",
            params={
                "q": query,
                "format": "json",
                "no_redirect": 1,
                "no_html": 1,
                "skip_disambig": 1
            },
            timeout=15
        )
        api_data = api_response.json()

        results = []
        if api_data.get("AbstractText"):
            results.append({
                "title": api_data.get("Heading", "Main Result"),
                "url": api_data.get("AbstractURL", ""),
                "description": api_data.get("AbstractText", "")
            })

        # HTML scraping
        html_response = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'},
            timeout=15
        )
        soup = BeautifulSoup(html_response.text, 'html.parser')

        # Extract organic results
        for result in soup.select('.result__body'):
            title_elem = result.select_one('.result__title a')
            snippet_elem = result.select_one('.result__snippet')

            if title_elem and snippet_elem:
                title = title_elem.text.strip()
                url = title_elem['href']
                clean_url = re.sub(r'&uddg=.*', '', url.split('=')[-1])
                clean_url = requests.utils.unquote(clean_url)

                if clean_url.startswith('http'):
                    results.append({
                        "title": title,
                        "url": clean_url,
                        "description": snippet_elem.text.strip()
                    })

            if len(results) >= 5:
                break

        return results[:5]
    except Exception as e:
        print(f"DuckDuckGo search error: {str(e)}")
        return []

def searx_search(query: str):
    """Search using SearXNG meta search engine"""
    try:
        response = requests.get(
            "https://searx.be/search",
            params={
                "q": query,
                "format": "json",
                "language": "en-US",
                "safesearch": 0
            },
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'},
            timeout=15
        )
        data = response.json()

        results = []
        for result in data.get('results', [])[:5]:
            results.append({
                "title": result.get('title', 'No Title'),
                "url": result.get('url', ''),
                "description": result.get('content', 'No description available')
            })

        return results
    except Exception as e:
        print(f"SearX search error: {str(e)}")
        return []

def dual_search(query: str):
    """Perform search using both engines and combine results"""
    ddg_results = duckduckgo_search(query)
    searx_results = searx_search(query)

    # Combine and deduplicate
    combined = ddg_results + searx_results
    unique_results = []
    seen_urls = set()

    for result in combined:
        if result['url'] not in seen_urls:
            unique_results.append(result)
            seen_urls.add(result['url'])

    return unique_results[:7]

def find_download_links(content: str, base_url: str):
    """Find potential download links in HTML content"""
    soup = BeautifulSoup(content, 'lxml')
    download_links = []

    # Look for file extensions
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        if any(ext in href for ext in ['.exe', '.zip', '.rar', '.tar', '.gz', '.pdf', '.dmg', '.deb', '.rpm']):
            # Make absolute URL
            if href.startswith('/'):
                full_url = requests.compat.urljoin(base_url, href)
            elif href.startswith('http'):
                full_url = href
            else:
                continue

            download_links.append({
                "text": a.get_text().strip() or "Download",
                "url": full_url
            })

    return download_links[:5]

def fetch_webpage_content(url: str):
    """Fetch and extract main content from a webpage"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()

        soup = BeautifulSoup(response.content, 'lxml')
        title = soup.title.string if soup.title else "No Title"

        # Remove unnecessary elements
        for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
            element.decompose()

        # Extract main content
        content_selectors = [
            'article', 
            'main', 
            'div[itemprop="articleBody"]', 
            'div.content', 
            'div.post-content',
            'div.entry-content',
            'body'
        ]

        main_content = ""
        for selector in content_selectors:
            element = soup.select_one(selector)
            if element:
                main_content = element.get_text(separator='\n', strip=True)
                if len(main_content) > 100:
                    break

        # Fallback to body
        if not main_content and soup.body:
            main_content = soup.body.get_text(separator='\n', strip=True)

        # Find download links
        download_links = find_download_links(response.text, url)

        # Format download links
        dl_info = ""
        if download_links:
            dl_info = "## 🔽 Download Links:\n"
            for dl in download_links:
                dl_info += f"- [{dl['text']}]({dl['url']})\n"
            dl_info += "\n"

        # Clean and truncate content
        if main_content:
            main_content = re.sub(r'\s+', ' ', main_content)
            if len(main_content) > 3000:
                main_content = main_content[:3000] + "... [truncated]"

        return {
            "title": title,
            "url": url,
            "content": main_content or "No content extracted",
            "downloads": dl_info
        }
    except Exception as e:
        print(f"Error fetching {url}: {str(e)}")
        return {
            "title": "Error",
            "url": url,
            "content": f"⚠️ Failed to fetch content: {str(e)}",
            "downloads": ""
        }

# --- AI Inference with Enhanced Web Context ---
async def generate_ai_response(prompt: str, user_id: int):
    """Generate response with Together.ai API"""
    global api_call_count, last_api_reset

    # Apply rate limiting
    rate_limited_api_call()

    state = user_states.get(user_id, {"net": False, "history": []})
    web_context = ""
    history = state.get("history", [])
    use_web = state["net"]

    # Extract URLs from user message
    url_pattern = r'(?:https?://|www\.)[^\s<>"]+'
    urls = re.findall(url_pattern, prompt)

    # Fetch content from URLs if provided
    if urls and use_web:
        try:
            for url in urls[:2]:
                if not url.startswith('http'):
                    url = 'https://' + url
                content = fetch_webpage_content(url)
                web_context += (f"## Webpage Content: [{content['title']}]({content['url']})\n"
                                f"{content['content']}\n\n"
                                f"{content['downloads']}\n")
        except Exception as e:
            web_context += f"⚠️ URL fetch error: {str(e)}\n\n"

    # Perform search if enabled and no URLs found
    if use_web and not urls:
        try:
            results = dual_search(prompt)
            if results:
                web_context += "## 🔍 Web Search Results\n"
                for i, res in enumerate(results, 1):
                    web_context += (f"{i}. [{res['title']}]({res['url']})\n"
                                  f"   {res.get('description', 'No description available')}\n\n")
            else:
                web_context += "⚠️ No search results found\n\n"
        except Exception as e:
            web_context += f"⚠️ Search error: {str(e)}\n\n"

    # Prepare messages
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add web context if available
    if web_context:
        messages.append({"role": "system", "content": web_context})
        messages.append({
            "role": "system", 
            "content": "IMPORTANT: Use the provided web context to answer the user's question. "
                       "Be concise - limit your response to 5-7 sentences. "
                       "Include relevant links from the search results or webpage content."
        })

    # Add history context
    if history:
        messages.extend(history[-20:])

    messages.append({"role": "user", "content": prompt})

    # API call
    headers = {"Authorization": f"Bearer {TOGETHER_API_KEY}"}
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": 1024,
        "temperature": 0.7
    }

    try:
        response = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )
        resp_json = response.json()

        # Update API call count
        api_call_count += 1
        if (datetime.now() - last_api_reset).seconds >= 60:
            api_call_count = 1
            last_api_reset = datetime.now()

        if 'choices' in resp_json and resp_json['choices']:
            ai_response = resp_json['choices'][0]['message']['content']

            # Update message history
            new_history = history + [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": ai_response}
            ]
            user_states[user_id]["history"] = new_history[-20:]

            return ai_response
        else:
            return f"⚠️ Unexpected API response: {json.dumps(resp_json)[:300]}"
    except Exception as e:
        return f"⚠️ API Error: {str(e)}"

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message"""
    user_id = update.effective_user.id
    user_states[user_id] = {"net": False, "history": []}

    keyboard = [
        [InlineKeyboardButton("🌐 Web Search ON", callback_data="net_on")],
        [InlineKeyboardButton("🚫 Web Search OFF", callback_data="net_off")]
    ]

    await update.message.reply_text(
        "🤖 Welcome to Advanced AI Assistant with Web Access\n\n"
        "🔍 *Key Features:*\n"
        "- Real-time webpage content extraction\n"
        "- Dual search engine (DuckDuckGo + SearXNG)\n"
        "- Download link detection\n"
        "- Conversation history\n\n"
        "💡 *How to use:*\n"
        "1. Enable web search with /neton\n"
        "2. Ask questions requiring real-time info\n"
        "3. Include URLs for content analysis\n\n"
        "📋 *Commands:*\n"
        "/neton - Enable web search\n"
        "/netoff - Disable web search\n"
        "/clear - Reset conversation history\n"
        "/status - Show bot status\n\n"
        "⚙️ *Current status:*\n"
        f"Web Search: {'ON 🌐' if user_states[user_id]['net'] else 'OFF 🚫'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_states:
        user_states[user_id] = {"net": False, "history": []}

    state = user_states[user_id]
    response_text = ""

    if query.data == "net_on":
        state["net"] = True
        response_text = "🌐 Web search ACTIVATED"
    elif query.data == "net_off":
        state["net"] = False
        response_text = "🚫 Web search DEACTIVATED"

    # Update keyboard
    keyboard = [
        [InlineKeyboardButton("🌐 Web Search ON", callback_data="net_on")],
        [InlineKeyboardButton("🚫 Web Search OFF", callback_data="net_off")]
    ]

    await query.edit_message_text(
        f"{response_text}\n\n⚙️ Current status:\n"
        f"Web Search: {'ON 🌐' if state['net'] else 'OFF 🚫'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset conversation history"""
    user_id = update.effective_user.id
    if user_id in user_states:
        user_states[user_id]["history"] = []
        await update.message.reply_text("🗑️ Conversation history cleared!")
    else:
        await update.message.reply_text("⚠️ No active session found. Use /start first.")

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status"""
    global memory_usage
    user_id = update.effective_user.id
    state = user_states.get(user_id, {"net": False, "history": []})

    # Update memory usage
    process = psutil.Process()
    memory_usage = process.memory_info().rss / (1024 * 1024)  # in MB

    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{uptime.days}d {hours}h {minutes}m {seconds}s"

    history_count = len(state.get("history", []))

    status_message = (
        "🤖 *Bot Status*\n"
        f"• Uptime: `{uptime_str}`\n"
        f"• Memory: `{memory_usage:.2f} MB`\n"
        f"• Requests: `{request_count}`\n"
        f"• API Calls (last min): `{api_call_count}/60`\n"
        f"• Web Search: {'`ON 🌐`' if state.get('net', False) else '`OFF 🚫`'}\n"
        f"• History: `{history_count}` messages\n\n"
        "🌐 *Web Access Status:*\n"
        f"• Last search: `{last_search_time if last_search_time else 'Never'}`\n"
        f"• Last fetch: `{last_fetch_time if last_fetch_time else 'Never'}`"
    )

    await update.message.reply_text(status_message, parse_mode="Markdown")

# Command handlers
command_handlers = {
    "neton": lambda update, context: update_state(update, True, "Web search ACTIVATED"),
    "netoff": lambda update, context: update_state(update, False, "Web search DEACTIVATED"),
}

async def update_state(update: Update, value: bool, message: str):
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"net": False, "history": []}

    user_states[user_id]["net"] = value
    await update.message.reply_text(f"✅ {message}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process user messages"""
    global request_count, last_search_time, last_fetch_time
    request_count += 1

    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"net": False, "history": []}

    # Show typing indicator
    await update.message.reply_chat_action("typing")

    # Track web access timestamps
    if user_states[user_id]["net"]:
        if re.search(r'https?://', update.message.text):
            last_fetch_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            last_search_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Generate AI response
    try:
        response = await generate_ai_response(update.message.text, user_id)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")
        return

    # Split long messages
    max_length = 4096
    for i in range(0, len(response), max_length):
        part = response[i:i + max_length]
        await update.message.reply_text(part, disable_web_page_preview=True)

# --- Main Application ---
def main():
    global start_time
    start_time = datetime.now()

    print("🤖 Starting AI Telegram Bot...")
    print(f"System prompt: {SYSTEM_PROMPT[:200]}...")

    # Create Telegram application
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("status", show_status))
    for command, handler in command_handlers.items():
        app.add_handler(CommandHandler(command, handler))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("✅ Bot is running")
    app.run_polling()

if __name__ == "__main__":
    main()
