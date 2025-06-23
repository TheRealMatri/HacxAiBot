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
from flask import Flask
from ratelimit import limits, sleep_and_retry

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "7796376668:AAG1WP53OpMkp0luDC4IxdaJVDg5tXXV6ao")
TOGETHER_API_KEY = os.getenv("TOGETHER_API_KEY", "tgp_v1_jpxmeYXld5n1xRlct8QSQQdwp6Z1fTx05e7qdxkZO0Q")
MODEL_NAME = "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free"
TOR_PROXY = "socks5h://localhost:9050"

# Validate environment variables
if not TELEGRAM_TOKEN or not TOGETHER_API_KEY:
    raise ValueError("Missing required environment variables: TELEGRAM_TOKEN and TOGETHER_API_KEY")

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
def duckduckgo_search(query: str, use_tor: bool = False):
    """Search with DuckDuckGo including Tor support with enhanced results"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}

    try:
        # First try the API for instant answers
        api_response = session.get(
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

        # Extract API results
        if api_data.get("AbstractText"):
            results.append({
                "title": api_data.get("Heading", "Main Result"),
                "url": api_data.get("AbstractURL", ""),
                "description": api_data.get("AbstractText", "")
            })

        # Fallback to HTML scraping if API doesn't provide enough results
        if len(results) < 3:
            html_response = session.get(
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
                    # Clean tracking parameters from URL
                    clean_url = re.sub(r'&uddg=.*', '', url.split('=')[-1])
                    clean_url = requests.utils.unquote(clean_url)

                    # Skip ads and non-http links
                    if clean_url.startswith('http'):
                        results.append({
                            "title": title,
                            "url": clean_url,
                            "description": snippet_elem.text.strip()
                        })

                if len(results) >= 5:  # Limit to 5 results
                    break

        return results[:5]  # Return top 5 results
    except Exception as e:
        print(f"DuckDuckGo search error: {str(e)}")
        return []

def searx_search(query: str, use_tor: bool = False):
    """Search using SearXNG meta search engine"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}

    try:
        # Using a public SearXNG instance
        response = session.get(
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
        for result in data.get('results', [])[:5]:  # Get top 5 results
            results.append({
                "title": result.get('title', 'No Title'),
                "url": result.get('url', ''),
                "description": result.get('content', 'No description available')
            })

        return results
    except Exception as e:
        print(f"SearX search error: {str(e)}")
        return []

def dual_search(query: str, use_tor: bool = False):
    """Perform search using both engines and combine results"""
    ddg_results = duckduckgo_search(query, use_tor)
    searx_results = searx_search(query, use_tor)

    # Combine and deduplicate results
    combined = ddg_results + searx_results
    unique_results = []
    seen_urls = set()

    for result in combined:
        if result['url'] not in seen_urls:
            unique_results.append(result)
            seen_urls.add(result['url'])

    return unique_results[:7]  # Return top 7 unique results

def find_download_links(content: str, base_url: str):
    """Find potential download links in HTML content"""
    soup = BeautifulSoup(content, 'lxml')
    download_links = []

    # Look for common download indicators
    for a in soup.find_all('a', href=True):
        href = a['href'].lower()
        text = a.get_text().lower()

        # Check for file extensions
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

    # Look for download buttons
    for button in soup.find_all(class_=re.compile(r'download|btn-download|download-button')):
        link = button.find('a', href=True)
        if link:
            href = link['href'].lower()
            if any(ext in href for ext in ['.exe', '.zip', '.rar', '.tar', '.gz', '.pdf', '.dmg', '.deb', '.rpm']):
                # Make absolute URL
                if href.startswith('/'):
                    full_url = requests.compat.urljoin(base_url, href)
                elif href.startswith('http'):
                    full_url = href
                else:
                    continue

                download_links.append({
                    "text": link.get_text().strip() or "Download",
                    "url": full_url
                })

    return download_links[:5]  # Return top 5 download links

def fetch_webpage_content(url: str, use_tor: bool = False):
    """Fetch and extract main content from a webpage with enhanced parsing"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = session.get(url, headers=headers, timeout=20)
        response.raise_for_status()

        # Parse HTML content
        soup = BeautifulSoup(response.content, 'lxml')

        # Extract title
        title = soup.title.string if soup.title else "No Title"

        # Remove unnecessary elements
        for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
            element.decompose()

        # Extract main content - try multiple strategies
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
                if len(main_content) > 100:  # Valid content found
                    break

        # Fallback to body if no content found
        if not main_content:
            main_content = soup.body.get_text(separator='\n', strip=True) if soup.body else ""

        # Find download links
        download_links = find_download_links(response.text, url)

        # Find forum content
        forum_content = ""
        if any(word in url for word in ['forum', 'community', 'board', 'discussion']):
            # Look for common forum structures
            for thread in soup.select('.thread, .topic, .post, .comment'):
                forum_content += thread.get_text(separator='\n', strip=True) + "\n\n"
            if not forum_content:
                forum_content = "No forum threads detected"

        # Clean and truncate content
        if main_content:
            # Remove excessive whitespace
            main_content = re.sub(r'\s+', ' ', main_content)
            # Truncate to preserve tokens
            if len(main_content) > 3000:
                main_content = main_content[:3000] + "... [truncated]"

        # Format download links
        dl_info = ""
        if download_links:
            dl_info = "## 🔽 Download Links:\n"
            for dl in download_links:
                dl_info += f"- [{dl['text']}]({dl['url']})\n"
            dl_info += "\n"

        # Format forum content
        forum_info = ""
        if forum_content:
            forum_info = "## 💬 Forum Content:\n"
            if len(forum_content) > 2000:
                forum_info += forum_content[:2000] + "... [truncated]"
            else:
                forum_info += forum_content
            forum_info += "\n\n"

        return {
            "title": title,
            "url": url,
            "content": main_content or "No content extracted",
            "downloads": dl_info,
            "forum": forum_info
        }
    except Exception as e:
        print(f"Error fetching {url}: {str(e)}")
        return {
            "title": "Error",
            "url": url,
            "content": f"⚠️ Failed to fetch content: {str(e)}",
            "downloads": "",
            "forum": ""
        }

def fetch_telegram_channel(channel_name: str, use_tor: bool = False):
    """Fetch Telegram channel content using Telegram Web"""
    session = requests.Session()
    if use_tor:
        session.proxies = {"http": TOR_PROXY, "https": TOR_PROXY}

    try:
        # Format URL
        if not channel_name.startswith('@'):
            channel_name = '@' + channel_name
        url = f"https://t.me/s/{channel_name[1:]}"

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = session.get(url, headers=headers, timeout=20)
        response.raise_for_status()

        # Parse HTML content
        soup = BeautifulSoup(response.content, 'lxml')

        # Extract channel title
        title = soup.select_one('.tgme_channel_info_header_title span')
        title = title.text.strip() if title else "No Title"

        # Extract messages
        messages = []
        for message in soup.select('.tgme_widget_message_wrap'):
            # Extract message text
            text_elem = message.select_one('.tgme_widget_message_text')
            text = text_elem.text.strip() if text_elem else ""

            # Extract message date
            date_elem = message.select_one('.tgme_widget_message_date time')
            date = date_elem['datetime'] if date_elem else ""

            # Extract media
            media = ""
            if message.select_one('.tgme_widget_message_photo_wrap'):
                media = " [Photo]"
            elif message.select_one('.tgme_widget_message_video_wrap'):
                media = " [Video]"
            elif message.select_one('.tgme_widget_message_document_wrap'):
                media = " [Document]"

            messages.append(f"{date}: {text}{media}")

        content = "\n\n".join(messages[:15])  # Get last 15 messages
        if not content:
            content = "No messages found"

        return {
            "title": title,
            "url": url,
            "content": content
        }
    except Exception as e:
        print(f"Error fetching Telegram channel: {str(e)}")
        return {
            "title": "Error",
            "url": url,
            "content": f"⚠️ Failed to fetch Telegram channel: {str(e)}"
        }

# --- AI Inference with Enhanced Web Context ---
async def generate_ai_response(prompt: str, user_id: int):
    """Generate response with Together.ai API using message history and web context"""
    global api_call_count, last_api_reset

    # Apply rate limiting (60 calls per minute)
    rate_limited_api_call()

    state = user_states.get(user_id, {"tor": False, "net": False, "history": []})
    web_context = ""
    history = state.get("history", [])
    use_web = state["tor"] or state["net"]

    # Check for Telegram channel links
    telegram_match = re.search(r'(?:https?://)?t(?:elegram)?\.me/(?:s/)?([a-zA-Z0-9_]+)', prompt)
    if telegram_match and use_web:
        channel_name = telegram_match.group(1)
        try:
            content = fetch_telegram_channel(channel_name, use_tor=state["tor"])
            web_context += (f"## Telegram Channel: [{content['title']}]({content['url']})\n"
                            f"{content['content']}\n\n")
        except Exception as e:
            web_context += f"⚠️ Telegram channel fetch error: {str(e)}\n\n"

    # Extract regular URLs from user message
    url_pattern = r'(?<!telegram\.me/)(?:https?://|www\.)[^\s<>"]+'
    urls = re.findall(url_pattern, prompt)

    # Fetch content from URLs if provided
    if urls and use_web and not telegram_match:
        try:
            for url in urls[:2]:  # Limit to 2 URLs
                if not url.startswith('http'):
                    url = 'https://' + url
                content = fetch_webpage_content(url, use_tor=state["tor"])
                web_context += (f"## Webpage Content: [{content['title']}]({content['url']})\n"
                                f"{content['content']}\n\n"
                                f"{content['downloads']}"
                                f"{content['forum']}\n")
        except Exception as e:
            web_context += f"⚠️ URL fetch error: {str(e)}\n\n"

    # Perform search if enabled and no URLs/channels found
    if use_web and not urls and not telegram_match:
        try:
            results = dual_search(prompt, use_tor=state["tor"])
            if results:
                web_context += "## 🔍 Web Search Results\n"
                for i, res in enumerate(results, 1):
                    web_context += (f"{i}. [{res['title']}]({res['url']})\n"
                                  f"   {res.get('description', 'No description available')}\n\n")
            else:
                web_context += "⚠️ No search results found\n\n"
        except Exception as e:
            web_context += f"⚠️ Search error: {str(e)}\n\n"

    # Prepare messages with history context
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add web context if available
    if web_context:
        messages.append({"role": "system", "content": web_context})

        # Add explicit instruction to use web data
        messages.append({
            "role": "system", 
            "content": "IMPORTANT: Use the provided web context to answer the user's question. "
                       "Be concise - limit your response to 5-7 sentences. "
                       "Include relevant links from the search results or webpage content in your response using markdown format. "
                       "Pay special attention to download links and forum content when requested."
        })

    # Add history context with clear separation
    if history:
        messages.append({"role": "system", "content": "## Conversation History"})
        messages.extend(history[-20:])  # Last 20 messages as context

    messages.append({"role": "user", "content": prompt})

    # API call
    headers = {"Authorization": f"Bearer {TOGETHER_API_KEY}"}
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": 1024,  # Limit tokens for concise responses
        "temperature": 0.7
    }

    try:
        response = requests.post(
            "https://api.together.xyz/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60  # Increased timeout for complex responses
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
            # Keep last 20 messages (10 interactions)
            user_states[user_id]["history"] = new_history[-20:]

            return ai_response
        else:
            return f"⚠️ Unexpected API response: {json.dumps(resp_json)[:300]}"
    except Exception as e:
        return f"⚠️ API Error: {str(e)}"

# --- Telegram Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message with enhanced instructions"""
    user_id = update.effective_user.id
    user_states[user_id] = {"tor": False, "net": False, "history": []}

    keyboard = [
        [
            InlineKeyboardButton("🔓 Tor ON", callback_data="tor_on"),
            InlineKeyboardButton("🌐 Clearnet ON", callback_data="net_on")
        ],
        [
            InlineKeyboardButton("🔒 Tor OFF", callback_data="tor_off"),
            InlineKeyboardButton("🚫 Clearnet OFF", callback_data="net_off")
        ]
    ]

    await update.message.reply_text(
        "🤖 Welcome to Advanced AI Assistant with Real Web Access\n\n"
        "🔍 *Key Features:*\n"
        "- Real-time webpage content extraction from URLs\n"
        "- Dual search engine (DuckDuckGo + SearXNG)\n"
        "- Telegram channel content access\n"
        "- Download link detection\n"
        "- Forum content parsing\n"
        "- Tor support for anonymous browsing\n\n"
        "💡 *How to use:*\n"
        "1. Enable web search with /neton or /toron\n"
        "2. Ask questions requiring real-time info\n"
        "3. Include URLs or Telegram channel links\n"
        "4. The AI will automatically search web when needed\n\n"
        "📋 *Commands:*\n"
        "/toron - Enable Tor search\n"
        "/toroff - Disable Tor\n"
        "/neton - Enable Clearnet search\n"
        "/netoff - Disable Clearnet\n"
        "/clear - Reset conversation history\n"
        "/status - Show bot status\n\n"
        "⚙️ *Current status:*\n"
        f"Tor: {'ON 🔓' if user_states[user_id]['tor'] else 'OFF 🔒'}\n"
        f"Clearnet: {'ON 🌐' if user_states[user_id]['net'] else 'OFF 🚫'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses with enhanced feedback"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False, "history": []}

    state = user_states[user_id]
    response_text = ""

    if query.data == "tor_on":
        state["tor"] = True
        state["net"] = False
        response_text = "✅ Tor search ACTIVATED\nClearnet search disabled"
    elif query.data == "tor_off":
        state["tor"] = False
        response_text = "🔒 Tor search DEACTIVATED"
    elif query.data == "net_on":
        state["net"] = True
        state["tor"] = False
        response_text = "🌐 Clearnet search ACTIVATED\nTor search disabled"
    elif query.data == "net_off":
        state["net"] = False
        response_text = "🚫 Clearnet search DEACTIVATED"

    # Update keyboard state
    keyboard = [
        [
            InlineKeyboardButton("🔓 Tor ON", callback_data="tor_on"),
            InlineKeyboardButton("🌐 Clearnet ON", callback_data="net_on")
        ],
        [
            InlineKeyboardButton("🔒 Tor OFF", callback_data="tor_off"),
            InlineKeyboardButton("🚫 Clearnet OFF", callback_data="net_off")
        ]
    ]

    await query.edit_message_text(
        f"{response_text}\n\n⚙️ Current status:\n"
        f"Tor: {'ON 🔓' if state['tor'] else 'OFF 🔒'}\n"
        f"Clearnet: {'ON 🌐' if state['net'] else 'OFF 🚫'}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset conversation history with confirmation"""
    user_id = update.effective_user.id
    if user_id in user_states:
        user_states[user_id]["history"] = []
        await update.message.reply_text("🗑️ Conversation history completely cleared!")
    else:
        await update.message.reply_text("⚠️ No active session found. Use /start first.")

async def show_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show enhanced bot status with more details"""
    global memory_usage
    user_id = update.effective_user.id
    state = user_states.get(user_id, {"tor": False, "net": False, "history": []})

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
        f"• Tor: {'`ON 🔓`' if state.get('tor', False) else '`OFF 🔒`'}\n"
        f"• Clearnet: {'`ON 🌐`' if state.get('net', False) else '`OFF 🚫`'}\n"
        f"• History: `{history_count}` messages (`{history_count//2}` exchanges)\n\n"
        "🌐 *Web Access Status:*\n"
        f"• Last search: `{last_search_time if last_search_time else 'Never'}`\n"
        f"• Last fetch: `{last_fetch_time if last_fetch_time else 'Never'}`"
    )

    await update.message.reply_text(status_message, parse_mode="Markdown")

# Command handlers
command_handlers = {
    "toron": lambda update, context: update_state(update, "tor", True, "Tor search ACTIVATED\nClearnet search disabled"),
    "toroff": lambda update, context: update_state(update, "tor", False, "Tor search DEACTIVATED"),
    "neton": lambda update, context: update_state(update, "net", True, "Clearnet search ACTIVATED\nTor search disabled"),
    "netoff": lambda update, context: update_state(update, "net", False, "Clearnet search DEACTIVATED"),
}

async def update_state(update: Update, key: str, value: bool, message: str):
    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False, "history": []}

    state = user_states[user_id]
    state[key] = value

    # Ensure Tor and Clearnet are mutually exclusive
    if key == "tor" and value:
        state["net"] = False
    elif key == "net" and value:
        state["tor"] = False

    await update.message.reply_text(f"✅ {message}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process user messages with enhanced web handling"""
    global request_count, last_search_time, last_fetch_time
    request_count += 1

    user_id = update.effective_user.id
    if user_id not in user_states:
        user_states[user_id] = {"tor": False, "net": False, "history": []}

    # Show typing indicator
    await update.message.reply_chat_action("typing")

    # Track web access timestamps
    if user_states[user_id]["tor"] or user_states[user_id]["net"]:
        if re.search(r'https?://', update.message.text) or re.search(r't\.me/', update.message.text):
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

# --- Enhanced Replit Keep-Alive Mechanism ---
def ping_server():
    """Ping our own server to keep Replit alive with enhanced reliability"""
    while True:
        try:
            # Ping our own Flask server
            response = requests.get("http://localhost:8080/ping", timeout=10)
            if response.status_code == 200:
                print(f"✅ Keep-alive ping successful at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            else:
                print(f"⚠️ Keep-alive ping failed with status {response.status_code}")
        except Exception as e:
            print(f"🚨 Keep-alive ping error: {str(e)}")
            # Attempt to restart Flask server if ping fails
            try:
                print("Attempting to restart Flask server...")
                flask_thread = threading.Thread(target=run_flask)
                flask_thread.daemon = True
                flask_thread.start()
                print("Flask server restarted")
            except Exception as restart_error:
                print(f"Failed to restart Flask: {str(restart_error)}")

        # Ping every 4 minutes (240 seconds) to prevent Replit shutdown
        time.sleep(240)

# --- Flask Web Server ---
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    global memory_usage
    # Update memory usage
    process = psutil.Process()
    memory_usage = process.memory_info().rss / (1024 * 1024)  # in MB

    uptime = datetime.now() - start_time
    hours, remainder = divmod(uptime.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{uptime.days}d {hours}h {minutes}m {seconds}s"

    return (f"🤖 Advanced AI Telegram Bot is running!\n"
            f"• Uptime: {uptime_str}\n"
            f"• Memory: {memory_usage:.2f} MB\n"
            f"• Requests: {request_count}\n"
            f"• API Calls (last min): {api_call_count}/60\n"
            f"• Last Search: {last_search_time or 'Never'}\n"
            f"• Last Fetch: {last_fetch_time or 'Never'}\n"
            f"• Features: Dual search, Telegram channel access, Download detection, Forum parsing")

@flask_app.route('/ping')
def ping():
    return "OK", 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=8080)

# --- Main Application ---
def main():
    global start_time
    start_time = datetime.now()

    print("🤖 Starting Enhanced AI Telegram Bot with Web Access...")
    print(f"System prompt: {SYSTEM_PROMPT[:200]}...")

    # Start Flask server in a separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    print("🌐 Flask server started on port 8080")

    # Start keep-alive pinger in a separate thread
    pinger_thread = threading.Thread(target=ping_server)
    pinger_thread.daemon = True
    pinger_thread.start()
    print("🔌 Keep-alive pinger started")

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

    print("✅ Bot is running with real web access capabilities")
    app.run_polling()

if __name__ == "__main__":
    main()
