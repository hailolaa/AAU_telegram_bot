import logging
import random
import os
import sys
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)
from pymongo import MongoClient
from telegram.error import BadRequest
from aiohttp import web


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
# Support multiple admin IDs separated by commas
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip()]
ADMIN_CHANNEL_ID = int(os.getenv("ADMIN_CHANNEL_ID")) if os.getenv("ADMIN_CHANNEL_ID") else None
WEBHOOK_PATH = "/webhook"
PORT = int(os.getenv("PORT", 10000))
BASE_URL = os.getenv("BASE_URL")  # e.g. https://your-app-name.onrender.com

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is not set in environment.")
    sys.exit(1)

client = MongoClient(MONGO_URI) if MONGO_URI else MongoClient()
db = client["unimatch_bot2"]
users_collection = db["users"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------- UTILITIES -------------------
async def safe_edit_or_send_callback(query, text, reply_markup=None, parse_mode=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest:
        # Fallback to sending a new message in the same chat
        await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


async def safe_edit_or_send_message(update, text, reply_markup=None, parse_mode=None):
    # Handles both callback_query and normal messages
    if update and getattr(update, "callback_query", None):
        await safe_edit_or_send_callback(update.callback_query, text, reply_markup=reply_markup, parse_mode=parse_mode)
    elif update and getattr(update, "message", None):
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        # As a last resort, log it
        logger.warning("No update.message or update.callback_query available for sending message: %s", text)


def ensure_user_doc(doc):
    defaults = {
        "user_id": None,
        "tg_username": None,
        "name": "",
        "gender": None,
        "age": None,
        "department": "",
        "year": "",
        "interested_in": None,
        "bio": None,
        "photos": [],
        "likes": [],
        "liked_by": [],
        "passed": [],
        "step": "awaiting_name",
    }
    if doc is None:
        return defaults.copy()
    for k, v in defaults.items():
        if k not in doc:
            doc[k] = v
    return doc

# ------------------- START -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tg_username = update.effective_user.username
    user = users_collection.find_one({"user_id": user_id})
    if user:
        users_collection.update_one({"user_id": user_id}, {"$set": {"tg_username": tg_username}})
        keyboard = [[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]]
        if update.message:
            await update.message.reply_text("Welcome back! Use the menu below.", reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await safe_edit_or_send_message(update, "Welcome back! Use the menu below.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    users_collection.insert_one({
        "user_id": user_id,
        "tg_username": tg_username,
        "step": "awaiting_name",
        "likes": [],
        "liked_by": [],
        "passed": [],
        "photos": [],
        "department": "",
        "year": ""
    })
    await safe_edit_or_send_message(
        update,
        "Hey 👋 Welcome to AAU-LinkUp\nPress the button to start onboarding:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Start", callback_data="start_onboarding")]])
    )

# ------------------- ONBOARDING -------------------
async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tg_username = query.from_user.username
    users_collection.update_one({"user_id": user_id}, {"$set": {"step": "awaiting_name", "tg_username": tg_username}})
    await safe_edit_or_send_callback(query, "First, your name?:", parse_mode="Markdown")

# ------------------- MESSAGE HANDLER -------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message if update.message else update.channel_post
    if not message:
        return

    chat_id = message.chat_id
    text = message.text.strip() if message.text else ""

    # Debugging line to check chat ID
    logger.debug("Chat ID: %s", chat_id)

    # Broadcast flow support:
    # - Admins can trigger broadcast from their private chat (using user_data)
    # - Or from the configured admin control channel (using chat_data)
    # Check admin user-data broadcast flag first (private admin)
    user_id = message.chat_id
    if user_id in ADMIN_IDS and context.user_data.get("awaiting_broadcast"):
        all_users = list(users_collection.find({}, {"user_id": 1}))
        sent = 0
        for u in all_users:
            try:
                await context.bot.send_message(u["user_id"], f"📢 Broadcast from admin:\n\n{text}")
                sent += 1
            except Exception:
                pass
        context.user_data["awaiting_broadcast"] = False
        await message.reply_text(f"Broadcast sent to {sent} users.")
        return

    # Channel-driven broadcast (if admin hits broadcast from the control channel)
    if chat_id == ADMIN_CHANNEL_ID and context.chat_data.get("awaiting_broadcast"):
        all_users = list(users_collection.find({}, {"user_id": 1}))
        sent = 0
        for u in all_users:
            try:
                await context.bot.send_message(u["user_id"], f"📢 Broadcast from admin channel:\n\n{text}")
                sent += 1
            except Exception:
                pass
        context.chat_data["awaiting_broadcast"] = False
        await message.reply_text(f"Broadcast sent to {sent} users.")
        return

    # Only handle onboarding/user logic for private chats (not channels)
    if message.chat.type != "private":
        return

    # proceed with user onboarding/profile edits
    user = ensure_user_doc(users_collection.find_one({"user_id": user_id}))
    step = user.get("step")

    if step == "awaiting_name":
        if not text:
            await message.reply_text("Please send a valid name.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"name": text, "step": "awaiting_department"}})
        await message.reply_text("Great! Now enter your department (e.g., Computer Science):")
        return

    if step == "awaiting_department":
        if not text:
            await message.reply_text("Please enter a valid department.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"department": text, "step": "awaiting_year"}})
        await message.reply_text("Awesome! Now enter your year (e.g., 1st, 2nd, 3rd, 4th, Alumni):")
        return

    if step == "awaiting_year":
        if not text:
            await message.reply_text("Please enter a valid year.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"year": text, "step": "awaiting_gender"}})
        keyboard = [
            [InlineKeyboardButton("Male", callback_data="gender_male"),
             InlineKeyboardButton("Female", callback_data="gender_female")]
        ]
        await message.reply_text("Nice! Now select your gender:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if step == "awaiting_age":
        if not text.isdigit() or not (16 <= int(text) <= 100):
            await message.reply_text("Please enter a valid age (16–100).")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"age": int(text), "step": "awaiting_photo"}})
        await message.reply_text("Cool 😎 Now upload a profile photo.")
        return

    if step == "awaiting_bio":
        if not text:
            await message.reply_text("Please write a short bio about yourself.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"bio": text, "step": "done"}})
        await message.reply_text("Profile complete! 🎉")
        await show_main_menu(update, context)
        return

    if step == "edit_name":
        if not text:
            await message.reply_text("Please send a valid name.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"name": text, "step": "done"}})
        await message.reply_text("✅ Name updated.")
        await show_main_menu(update, context)
        return

    if step == "edit_department":
        if not text:
            await message.reply_text("Please enter a valid department.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"department": text, "step": "done"}})
        await message.reply_text("✅ Department updated.")
        await show_main_menu(update, context)
        return

    if step == "edit_year":
        if not text:
            await message.reply_text("Please enter a valid year.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"year": text, "step": "done"}})
        await message.reply_text("✅ Year updated.")
        await show_main_menu(update, context)
        return

    if step == "edit_age":
        if not text.isdigit() or not (16 <= int(text) <= 100):
            await message.reply_text("Please enter a valid age (16-100).")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"age": int(text), "step": "done"}})
        await message.reply_text("✅ Age updated.")
        await show_main_menu(update, context)
        return

    if step == "edit_bio":
        if not text:
            await message.reply_text("Please send a bio text.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"bio": text, "step": "done"}})
        await message.reply_text("✅ Bio updated.")
        await show_main_menu(update, context)
        return

    await update.message.reply_text(
        "I didn't understand that. Use the menu.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]])
    )

# ------------------- PHOTO HANDLER -------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.chat_id
    if not update.message.photo:
        await update.message.reply_text("Please send a photo.")
        return
    photo = update.message.photo[-1].file_id
    user = ensure_user_doc(users_collection.find_one({"user_id": user_id}))
    step = user.get("step")

    if step == "awaiting_photo":
        users_collection.update_one(
            {"user_id": user_id},
            {"$push": {"photos": photo}, "$set": {"step": "awaiting_interest"}}
        )
        keyboard = [
            [InlineKeyboardButton("Male", callback_data="interest_male"),
             InlineKeyboardButton("Female", callback_data="interest_female"),
             InlineKeyboardButton("Both", callback_data="interest_both")]
        ]
        await update.message.reply_text("📸 Photo saved! Great! Who are you interested in?", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if step == "edit_photo":
        users_collection.update_one({"user_id": user_id}, {"$set": {"photos": [photo], "step": "done"}})
        await update.message.reply_text("✅ Photo updated.")
        await show_main_menu(update, context)
        return

    if step == "awaiting_broadcast" and user_id in ADMIN_IDS:
        await update.message.reply_text("Broadcast requires text only.")
        return

    users_collection.update_one({"user_id": user_id}, {"$addToSet": {"photos": photo}})
    await update.message.reply_text("Photo uploaded to your profile.")

# ------------------- CALLBACK HANDLER -------------------
async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    user = ensure_user_doc(users_collection.find_one({"user_id": user_id}))
    chat_id = query.message.chat_id
    data = query.data
    await query.answer()

    if data.startswith("like_"):
        await handle_like(update, context)
        return

    if data.startswith("skip_"):
        try:
            target_id = int(data.split("_", 1)[1])
            users_collection.update_one({"user_id": user_id}, {"$addToSet": {"passed": target_id}})
        except Exception:
            pass
        await find_match(update, context)
        return

    if data == "main_menu":
        await show_main_menu(update, context)
        return

    if data == "start_onboarding":
        await start_onboarding(update, context)
        return

    if data == "edit_profile":
        keyboard = [
            [InlineKeyboardButton("✏️ Edit Name", callback_data="edit_name")],
            [InlineKeyboardButton("✏️ Edit Age", callback_data="edit_age")],
            [InlineKeyboardButton("✏️ Edit Gender", callback_data="edit_gender")],
            [InlineKeyboardButton("✏️ Edit Department", callback_data="edit_department")],
            [InlineKeyboardButton("✏️ Edit Year", callback_data="edit_year")],
            [InlineKeyboardButton("✏️ Edit Bio", callback_data="edit_bio")],
            [InlineKeyboardButton("🖼 Edit Photo", callback_data="edit_photo")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        await safe_edit_or_send_callback(query, "Choose what to edit:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("edit_"):
        users_collection.update_one({"user_id": user_id}, {"$set": {"step": data}})
        await safe_edit_or_send_callback(query, f"✏️ Send your new {data.split('_', 1)[1]}:")
        return

    if data.startswith("gender_"):
        gender = data.split("_", 1)[1]
        cur_step = user.get("step", "")
        if cur_step.startswith("edit_"):
            users_collection.update_one({"user_id": user_id}, {"$set": {"gender": gender, "step": "done"}})
            await safe_edit_or_send_callback(query, f"✅ Gender updated to {gender}.")
            await show_main_menu(update, context)
        else:
            users_collection.update_one({"user_id": user_id}, {"$set": {"gender": gender, "step": "awaiting_age"}})
            await safe_edit_or_send_callback(query, "Enter your age (16–100):")
        return

    if data.startswith("interest_"):
        interest = data.split("_", 1)[1]
        users_collection.update_one({"user_id": user_id}, {"$set": {"interested_in": interest, "step": "awaiting_bio"}})
        await safe_edit_or_send_callback(query, "Great! Write a short bio about yourself:")
        return

    if data == "view_profile":
        await show_profile(update, context)
        return

    if data == "find_match":
        await find_match(update, context)
        return

    if data == "leaderboard":
        await show_leaderboard(update, context)
        return

    if data == "admin_panel":
        await show_admin_panel(update, context)
        return

    if data == "broadcast":
        # Allow broadcast both from the configured admin channel or private admin
        if chat_id == ADMIN_CHANNEL_ID:
            context.chat_data["awaiting_broadcast"] = True
            await safe_edit_or_send_callback(query, "Send the message to broadcast (text only) in this channel.")
        elif user_id in ADMIN_IDS:
            context.user_data["awaiting_broadcast"] = True
            await safe_edit_or_send_callback(query, "Send the message to broadcast (text only) in your private chat. It will be forwarded to all users.")
        else:
            await safe_edit_or_send_callback(query, "⛔ Only the control channel or admins can broadcast.")
        return

    if data.startswith("report_"):
        target_id = int(data.split("_", 1)[1])
        # You can log this or notify admin
        await safe_edit_or_send_callback(query, "🚫 User reported. Thank you for keeping AAU-LinkUp safe!")
        return

    if data == "help_command":
        await help_command(update, context)
        return

    await safe_edit_or_send_callback(query, "Unknown action. Use the menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]]))

# ------------------- PROFILE DISPLAY -------------------
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function can be called by callback_query or by a normal message
    user_id = update.callback_query.from_user.id if update.callback_query else update.effective_user.id
    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await safe_edit_or_send_message(update, "No profile found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]]))
        return

    user = ensure_user_doc(user)
    text = (
        f"👤 *{user.get('name')}*\n"
        f"Gender: {user.get('gender')}\n"
        f"Age: {user.get('age')}\n"
        f"Department: {user.get('department')}\n"
        f"Year: {user.get('year')}\n"
        f"Bio: {user.get('bio')}\n"
        f"❤️ Likes received: {len(user.get('liked_by', []))}\n"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    photos = user.get("photos", [])

    if update.callback_query:
        try:
            if photos:
                await update.callback_query.message.reply_photo(photos[-1], caption=text, parse_mode="Markdown", reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=reply_markup)
        except BadRequest:
            if photos:
                await update.callback_query.message.reply_photo(photos[-1], caption=text, parse_mode="Markdown", reply_markup=reply_markup)
            else:
                await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        if photos:
            await update.message.reply_photo(photos[-1], caption=text, parse_mode="Markdown", reply_markup=reply_markup)
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

# ------------------- MAIN MENU -------------------
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("View Profiles", callback_data="find_match")],
        [InlineKeyboardButton("👤 My Profile", callback_data="view_profile")],
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile")],
        [InlineKeyboardButton("❓ Help", callback_data="help_command")],
    ]
    if update.effective_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await safe_edit_or_send_message(update, "Choose an option:", reply_markup=reply_markup)

# ------------------- MATCH SYSTEM -------------------
# ------------------- FIND MATCH -------------------
async def find_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = ensure_user_doc(users_collection.find_one({"user_id": user_id}))

    search_query = {"user_id": {"$ne": user_id}, "step": "done"}
    interested_in = user.get("interested_in")
    if interested_in and interested_in != "both":
        search_query["gender"] = interested_in
    else:
        search_query["gender"] = {"$in": ["male", "female"]}

    candidates = list(users_collection.find(search_query))

    def eligible(c):
        uid = c.get("user_id")
        if uid == user_id or uid in (user.get("likes") or []) or uid in (user.get("passed") or []):
            return False
        return True

    filtered = [c for c in candidates if eligible(c)]

    # If no profiles left to show, reset passed and likes
    if not filtered:
        users_collection.update_one(
            {"user_id": user_id},
            {"$set": {"passed": [], "likes": []}}
        )
        candidates = list(users_collection.find(search_query))
        filtered = [c for c in candidates if c.get("user_id") != user_id]

        if not filtered:
            keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
            await safe_edit_or_send_callback(
                query,
                "No matches available at the moment 😢 Try again later.",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        else:
            await query.message.reply_text("✨ You've seen everyone! Starting fresh...")

    candidate = random.choice(filtered)
    caption = (
        f"{candidate.get('name')}, {candidate.get('age')}\n"
        f"Department: {candidate.get('department')}\n"
        f"Year: {candidate.get('year')}\n"
        f"{candidate.get('bio')}"
    )

    photos = candidate.get("photos", [])
    match_keyboard = [
        [
            InlineKeyboardButton("👍 Connect", callback_data=f"like_{candidate.get('user_id')}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{candidate.get('user_id')}")
        ],
        [InlineKeyboardButton("🚫 Report", callback_data=f"report_{candidate.get('user_id')}")],
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]

    if photos:
        await query.message.reply_photo(
            photos[-1],
            caption=caption,
            reply_markup=InlineKeyboardMarkup(match_keyboard)
        )
    else:
        await safe_edit_or_send_callback(
            query,
            caption,
            reply_markup=InlineKeyboardMarkup(match_keyboard)
        )

# ------------------- LIKE HANDLER -------------------
async def handle_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    user_id = query.from_user.id
    if not query.data or "_" not in query.data:
        await query.answer("Invalid action")
        return

    try:
        liked_id = int(query.data.split("_", 1)[1])
    except Exception:
        await query.answer("Invalid target.")
        return

    liker = ensure_user_doc(users_collection.find_one({"user_id": user_id}))
    liked = ensure_user_doc(users_collection.find_one({"user_id": liked_id}))

    if not liked.get("user_id"):
        await query.answer("User not found.")
        return

    # Update like lists
    users_collection.update_one({"user_id": user_id}, {"$addToSet": {"likes": liked_id}})
    users_collection.update_one({"user_id": liked_id}, {"$addToSet": {"liked_by": user_id}})

    liked_doc = users_collection.find_one({"user_id": liked_id})
    liked_name = liked_doc.get("name", "Someone")
    await query.answer(f"You liked {liked_name} ❤️")

    # Notify the liked user that someone is interested
    try:
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👀 Show Profile", callback_data=f"show_liker_{user_id}"),
                InlineKeyboardButton("❌ Skip", callback_data="ignore_like")
            ]
        ])
        await context.bot.send_message(
            chat_id=liked_id,
            text="💌 Someone on AAU-LinkUp is interested in connecting with you. Would you like to see their profile?",
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        logger.warning("Failed to send like notification to %s: %s", liked_id, e)

    # Check for mutual like — notify both
    liked_doc = users_collection.find_one({"user_id": liked_id})
    if user_id in (liked_doc.get("likes", []) or []):
        liker_doc = users_collection.find_one({"user_id": user_id})
        liker_name = liker_doc.get("name", "Someone")

        liked_tg = liked_doc.get("tg_username")
        liker_tg = liker_doc.get("tg_username")

        mention_for_liker = f"@{liked_tg}" if liked_tg else liked_doc.get("name", "Someone")
        mention_for_liked = f"@{liker_tg}" if liker_tg else liker_name

        # Notify the liker (user who just tapped "Connect")
        try:
            await context.bot.send_message(user_id, f"💞 It's a mutual connection! You and {mention_for_liker} expressed interest. Feel free to chat and exchange details.")
        except Exception:
            logger.debug("Couldn't notify liker about mutual match.")

        # Notify the liked (the one who previously liked)
        try:
            await context.bot.send_message(liked_id, f"💞 It's a mutual connection! You and {mention_for_liked} expressed interest. Feel free to chat and exchange details.")
        except Exception:
            logger.debug("Couldn't notify liked about mutual match.")

    await find_match(update, context)


async def show_liker_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        liker_id = int(data.split("_", 2)[2])
    except Exception:
        await query.answer("Invalid user.")
        return

    liker = users_collection.find_one({"user_id": liker_id})
    if not liker:
        await query.answer("User not found.")
        return

    photos = liker.get("photos", [])
    caption = (
        f"{liker.get('name', 'Unknown')}, {liker.get('age', 'N/A')}\n"
        f"Department: {liker.get('department', 'N/A')}\n"
        f"Year: {liker.get('year', 'N/A')}\n"
        f"{liker.get('bio', 'No bio available')}"
    )

    match_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Connect", callback_data=f"like_{liker_id}"),
            InlineKeyboardButton("⏭ Skip", callback_data=f"skip_{liker_id}")
        ],
        [
            InlineKeyboardButton("🚫 Report", callback_data=f"report_{liker_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")
        ]
    ])

    if photos:
        await query.message.reply_photo(
            photo=photos[-1],
            caption=caption,
            parse_mode="Markdown",
            reply_markup=match_keyboard
        )
    else:
        await query.message.reply_text(
            caption,
            parse_mode="Markdown",
            reply_markup=match_keyboard
        )

# ------------------- LEADERBOARD -------------------
async def show_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = list(users_collection.find({"step": "done"}))
    males = [u for u in users if u.get("gender") == "male"]
    females = [u for u in users if u.get("gender") == "female"]

    top_males = sorted(males, key=lambda u: len(u.get("liked_by", [])), reverse=True)[:10]
    top_females = sorted(females, key=lambda u: len(u.get("liked_by", [])), reverse=True)[:10]

    msg = "🏆 *Top 10 Most Liked Profiles*\n\n"
    msg += "*Male:*\n"
    if top_males:
        for i, u in enumerate(top_males, 1):
            msg += f"{i}. {u.get('name','Unknown')} - ❤️ {len(u.get('liked_by', []))} | Dept: {u.get('department','')} | Year: {u.get('year','')}\n"
    else:
        msg += "No male profiles yet.\n"

    msg += "\n*Female:*\n"
    if top_females:
        for i, u in enumerate(top_females, 1):
            msg += f"{i}. {u.get('name','Unknown')} - ❤️ {len(u.get('liked_by', []))} | Dept: {u.get('department','')} | Year: {u.get('year','')}\n"
    else:
        msg += "No female profiles yet.\n"

    keyboard = [
        [InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await safe_edit_or_send_message(update, msg, parse_mode="Markdown", reply_markup=reply_markup)

# ------------------- ADMIN PANEL -------------------
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await safe_edit_or_send_message(update, "⛔ Admin panel only available to bot admins.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 View Leaderboard", callback_data="leaderboard")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="broadcast")],
    ]
    await safe_edit_or_send_message(update, "🛠 Admin Panel:", reply_markup=InlineKeyboardMarkup(keyboard))

# ------------------- ADMIN COMMAND -------------------
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin panel only available to bot admins.")
        return
    await show_admin_panel(update, context)

# ------------------- HELP COMMAND -------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "👋 *Welcome to AAU-LinkUp!*\n\n"
        "Find friends, study buddies, or networks at your university.\n"
        "Use /start to begin, or the menu to explore features.\n"
        "If you need help, contact @Urcoder21."
    )
    await safe_edit_or_send_message(update, help_text, parse_mode="Markdown")


# ------------------- IGNORE LIKE HANDLER -------------------
async def ignore_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simple acknowledgement for the 'skip' button on like notification
    if update.callback_query:
        await update.callback_query.answer("Skipped ❤️")

# ------------------- APP SETUP -------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # --- Command Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_command))

    # --- Message Handlers ---
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    # --- Callback Query Handlers (specific ones first) ---
    app.add_handler(CallbackQueryHandler(handle_like, pattern=r"^like_"))
    app.add_handler(CallbackQueryHandler(show_liker_profile, pattern=r"^show_liker_"))
    app.add_handler(CallbackQueryHandler(ignore_like, pattern="ignore_like"))

    # --- Keep this last! (generic handler) ---
    app.add_handler(CallbackQueryHandler(handle_buttons))

    # Use webhook if BASE_URL is provided, otherwise fallback to polling (convenient for local dev)
    if BASE_URL:
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,  # Use token as the URL path
            webhook_url=f"{BASE_URL}/{BOT_TOKEN}",
        )
    else:
        logger.info("BASE_URL not set; starting polling mode.")
        app.run_polling()

if __name__ == "__main__":
    main()
