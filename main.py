import logging
import random
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    CallbackQueryHandler, ContextTypes
)
from pymongo import MongoClient
from telegram.error import BadRequest
from bson.objectid import ObjectId
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
reports_collection = db["reports"]  # new collection to persist reports
like_notifications_collection = db["like_notifications"]  # new collection to queue and manage like notifications

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Minimum gap between "someone liked you" notifications if user didn't respond
NOTIFICATION_MIN_GAP = timedelta(minutes=30)

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
        "is_verified": False,
        "email": None,
        "verification_otp": None,
        "icebreaker": "",
    }
    if doc is None:
        return defaults.copy()
    for k, v in defaults.items():
        if k not in doc:
            doc[k] = v
    return doc

# Helper to keep Telegram username in DB up-to-date.
def upsert_tg_username(user_id, username):
    if username:
        try:
            users_collection.update_one({"user_id": user_id}, {"$set": {"tg_username": username}})
        except Exception:
            logger.exception("Failed to upsert tg_username for user %s", user_id)


# ------------------- LIKE NOTIFICATION QUEUE HELPERS -------------------
def _get_current_utc():
    return datetime.utcnow()

def _has_recent_unresponded_sent(recipient_id):
    """
    Return True if there exists a 'sent' notification for recipient that was sent within NOTIFICATION_MIN_GAP and is still awaiting response.
    """
    recent = like_notifications_collection.find_one({
        "recipient_id": recipient_id,
        "status": "sent",
    }, sort=[("sent_at", -1)])
    if not recent:
        return False
    sent_at = recent.get("sent_at")
    if not sent_at:
        return False
    return (_get_current_utc() - sent_at) < NOTIFICATION_MIN_GAP


def queue_like_notification(liker_id, recipient_id):
    """
    Persist a like notification in the queue. Return the inserted doc id (string).
    """
    doc = {
        "recipient_id": recipient_id,
        "liker_id": liker_id,
        "created_at": _get_current_utc(),
        "sent_at": None,
        "status": "queued",  # queued | sent | responded | cancelled
        "response": None,  # store action like 'viewed','ignored','liked_back'
    }
    try:
        res = like_notifications_collection.insert_one(doc)
        return str(res.inserted_id)
    except Exception:
        logger.exception("Failed to insert like notification for recipient %s from liker %s", recipient_id, liker_id)
        return None


async def try_deliver_next_notification(recipient_id, context: ContextTypes.DEFAULT_TYPE):
    """
    If recipient has no current 'sent' (awaiting-response) notification and there are queued notifications,
    deliver the latest queued one immediately. Returns True if a notification was sent.
    """
    # If there is currently an unresponded sent notification within the gap, do not deliver more
    if _has_recent_unresponded_sent(recipient_id):
        logger.debug("Recipient %s has recent unresponded sent notification; skipping delivery", recipient_id)
        return False

    # Ensure no 'sent' notifications exist (even older ones) that are awaiting response. We'll treat only status=='sent' as awaiting.
    existing_sent = like_notifications_collection.find_one({"recipient_id": recipient_id, "status": "sent"})
    if existing_sent:
        # if it's old (older than gap), allow new delivery; otherwise skip (but _has_recent_unresponded_sent already checked)
        sent_at = existing_sent.get("sent_at")
        if sent_at and (_get_current_utc() - sent_at) < NOTIFICATION_MIN_GAP:
            logger.debug("Existing sent notification is still in gap window for recipient %s", recipient_id)
            return False
        # else we allow sending another; mark the old as cancelled to avoid duplicates
        try:
            like_notifications_collection.update_one({"_id": existing_sent["_id"]}, {"$set": {"status": "cancelled"}})
        except Exception:
            logger.exception("Failed to cancel old sent notification for recipient %s", recipient_id)

    # pick the latest queued notification (user asked to show latest immediately)
    queued = like_notifications_collection.find_one({"recipient_id": recipient_id, "status": "queued"}, sort=[("created_at", -1)])
    if not queued:
        logger.debug("No queued notifications for recipient %s", recipient_id)
        return False

    # attempt to send
    try:
        liker_id = queued["liker_id"]
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👀 Show Profile", callback_data=f"show_liker_{liker_id}"),
                InlineKeyboardButton("❌ Skip", callback_data="ignore_like")
            ]
        ])
        notif_text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "   💌 *Secret Admirer*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Someone likes you! 👀\n"
            "Want to see who it is?"
        )
        await context.bot.send_message(
            chat_id=recipient_id,
            text=notif_text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        # mark as sent
        like_notifications_collection.update_one(
            {"_id": queued["_id"]},
            {"$set": {"status": "sent", "sent_at": _get_current_utc()}}
        )
        logger.info("Delivered like notification %s to recipient %s", str(queued.get("_id")), recipient_id)
        return True
    except Exception:
        logger.exception("Failed to deliver queued like notification %s to recipient %s", str(queued.get("_id")), recipient_id)
        # leave it queued to retry later
        return False


async def handle_new_like_notification(liker_id, recipient_id, context: ContextTypes.DEFAULT_TYPE):
    """
    High-level handler called when A likes B (non-mutual). It queues the notification and tries to deliver immediately
    unless a recent unresponded notification exists for the recipient.
    """
    # persist notification
    nid = queue_like_notification(liker_id, recipient_id)
    if not nid:
        return

    # If recipient has a currently sent notification within gap and not responded, do not send now.
    if _has_recent_unresponded_sent(recipient_id):
        logger.debug("Queued notification %s for recipient %s due to recent sent", nid, recipient_id)
        return

    # Otherwise, try to deliver immediately
    await try_deliver_next_notification(recipient_id, context)


async def mark_notifications_responded(recipient_id, liker_id=None, response_type="viewed"):
    """
    Mark the most recent 'sent' notification(s) for recipient as responded.
    If liker_id is provided, only mark the matching notification for that liker_id.
    """
    query = {"recipient_id": recipient_id, "status": "sent"}
    if liker_id is not None:
        query["liker_id"] = liker_id

    try:
        docs = list(like_notifications_collection.find(query))
        for d in docs:
            like_notifications_collection.update_one({"_id": d["_id"]}, {
                "$set": {"status": "responded", "response": response_type, "responded_at": _get_current_utc()}
            })
    except Exception:
        logger.exception("Failed to mark notifications responded for recipient %s liker %s", recipient_id, liker_id)


# ------------------- START -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    tg_username = update.effective_user.username
    # ensure we persist username on /start
    upsert_tg_username(user_id, tg_username)

    user = users_collection.find_one({"user_id": user_id})
    if user:
        users_collection.update_one({"user_id": user_id}, {"$set": {"tg_username": tg_username}})
        keyboard = [[InlineKeyboardButton("✨ Open Menu", callback_data="main_menu")]]
        welcome_back = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "    🔥 *Welcome Back!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Good to see you again! 💫\n"
            "Tap below to jump right in."
        )
        if update.message:
            await update.message.reply_text(welcome_back, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        else:
            await safe_edit_or_send_message(update, welcome_back, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
    welcome_new = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "  💎 *AAU UniMatch* 💎\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Hey there! 👋\n\n"
        "Find your match among AAU students.\n"
        "It only takes a minute to set up!\n\n"
        "_Tap the button to begin_ 👇"
    )
    await update.message.reply_text(
        welcome_new,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🚀 Let's Go!", callback_data="start_onboarding")]]),
        parse_mode="Markdown"
    )

# ------------------- ONBOARDING -------------------
async def start_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tg_username = query.from_user.username
    # persist username on callbacks too
    upsert_tg_username(user_id, tg_username)

    users_collection.update_one({"user_id": user_id}, {"$set": {"step": "awaiting_name", "tg_username": tg_username}})
    await safe_edit_or_send_callback(query, "📋 *Step 1 of 8* · Your Name\n○○○○○○○○○○ 10%\n\n✏️ What's your *full name*?", parse_mode="Markdown")

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
        await message.reply_text("📋 *Step 2 of 8* · Department\n●○○○○○○○○○ 20%\n\n🎓 Enter your *department* (e.g., Computer Science):", parse_mode="Markdown")
        return

    if step == "awaiting_department":
        if not text:
            await message.reply_text("Please enter a valid department.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"department": text, "step": "awaiting_year"}})
        await message.reply_text("📋 *Step 3 of 8* · Year\n●●○○○○○○○○ 30%\n\n📅 What year are you in? _(1st, 2nd, 3rd, 4th, or Alumni)_", parse_mode="Markdown")
        return

    if step == "awaiting_year":
        if not text:
            await message.reply_text("Please enter a valid year.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"year": text, "step": "awaiting_gender"}})
        keyboard = [
            [InlineKeyboardButton("🙋‍♂️ Male", callback_data="gender_male"),
             InlineKeyboardButton("🙋‍♀️ Female", callback_data="gender_female")]
        ]
        await message.reply_text("📋 *Step 4 of 8* · Gender\n●●●○○○○○○○ 40%\n\n👤 Select your gender:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        return

    if step == "awaiting_age":
        if not text.isdigit() or not (16 <= int(text) <= 100):
            await message.reply_text("Please enter a valid age (16–100).")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"age": int(text), "step": "awaiting_photo"}})
        await update.message.reply_text("📋 *Step 6 of 8* · Photo\n●●●●●○○○○○ 60%\n\n📸 Upload a profile photo to continue:", parse_mode="Markdown")
        return

    if step == "awaiting_bio":
        if not text:
            await message.reply_text("Please write a short bio about yourself.")
            return
        users_collection.update_one({"user_id": user_id}, {"$set": {"bio": text, "step": "done"}})
        done_msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "   🎉 *All Set!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Your profile is live! 💫\n"
            "Start discovering other AAU students now."
        )
        await update.message.reply_text(done_msg, parse_mode="Markdown")
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
            await message.reply_text("Please send a valid year.")
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

    if step == "awaiting_email_input":
        if not text.endswith("@aau.edu.et"):
            await update.message.reply_text("❌ Please enter a valid AAU email (e.g., firstname.lastname-ug@aau.edu.et).")
            return
        
        # Automatic verification — valid AAU email format = instantly verified
        users_collection.update_one({"user_id": user_id}, {"$set": {"email": text, "is_verified": True, "step": "done"}})
        verified_msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "   🛡 *Verified!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "You now have the *Verified* badge! 🎉\n"
            "Your profile will stand out in discovery."
        )
        await update.message.reply_text(verified_msg, parse_mode="Markdown")
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
            [InlineKeyboardButton("🙋‍♂️ Male", callback_data="interest_male"),
             InlineKeyboardButton("🙋‍♀️ Female", callback_data="interest_female"),
             InlineKeyboardButton("👥 Both", callback_data="interest_both")]
        ]
        await update.message.reply_text("📋 *Step 7 of 8* · Preferences\n●●●●●●○○○○ 70%\n\n✅ Photo saved!\n\n🔍 Who do you want to discover?", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
    # persist username on any callback
    upsert_tg_username(user_id, query.from_user.username)

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
            [InlineKeyboardButton("👤 Name", callback_data="edit_name"), InlineKeyboardButton("🎂 Age", callback_data="edit_age")],
            [InlineKeyboardButton("⚧ Gender", callback_data="edit_gender"), InlineKeyboardButton("🎓 Department", callback_data="edit_department")],
            [InlineKeyboardButton("📅 Year", callback_data="edit_year"), InlineKeyboardButton("📝 Bio", callback_data="edit_bio")],
            [InlineKeyboardButton("📸 Photo", callback_data="edit_photo")],
            [InlineKeyboardButton("🔙 Back", callback_data="main_menu")]
        ]
        edit_text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "    ✏️ *Edit Profile*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Tap a field to update it:"
        )
        await safe_edit_or_send_callback(query, edit_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
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
            await safe_edit_or_send_callback(query, "📋 *Step 5 of 8* · Age\n●●●●○○○○○○ 50%\n\n🎂 How old are you? _(16–100)_", parse_mode="Markdown")
        return

    if data.startswith("interest_"):
        interest = data.split("_", 1)[1]
        users_collection.update_one({"user_id": user_id}, {"$set": {"interested_in": interest, "step": "awaiting_bio"}})
        await safe_edit_or_send_callback(query, "📋 *Step 8 of 8* · Bio\n●●●●●●●○○○ 80%\n\n📝 Almost done! Write a short *bio* — vibes, goals, or hobbies:", parse_mode="Markdown")
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
        # Enhanced report handling:
        try:
            target_id = int(data.split("_", 1)[1])
        except Exception:
            await safe_edit_or_send_callback(query, "Invalid report target.")
            return

        reporter_id = query.from_user.id

        # Prevent reporter from reporting themselves
        if reporter_id == target_id:
            await safe_edit_or_send_callback(query, "You cannot report yourself.")
            return

        # Prevent duplicate reports by same reporter for the same target (only if open)
        existing = reports_collection.find_one({
            "target_id": target_id,
            "reporter_id": reporter_id,
            "status": {"$in": ["open", "pending"]}
        })
        if existing:
            await safe_edit_or_send_callback(query, "You've already reported this user. Our admins will review it.")
            return

        # Persist the report
        report_doc = {
            "target_id": target_id,
            "reporter_id": reporter_id,
            "created_at": datetime.utcnow(),
            "status": "open"
        }
        try:
            res = reports_collection.insert_one(report_doc)
            report_id = str(res.inserted_id)
        except Exception:
            logger.exception("Failed to save report to DB for target=%s by reporter=%s", target_id, reporter_id)
            await safe_edit_or_send_callback(query, "❌ Failed to file the report. Please try again later.")
            return

        # Acknowledge the reporter
        await safe_edit_or_send_callback(query, "🚫 Thank you — we've recorded your report. Our admins will review it shortly.")

        # Notify admin channel (or each admin privately if no channel configured)
        try:
            target_user = users_collection.find_one({"user_id": target_id}) or {}
            reporter_user = users_collection.find_one({"user_id": reporter_id}) or {}

            admin_text = (
                f"⚠️ New report (id: {report_id})\n\n"
                f"Target: {target_user.get('name','Unknown')} (id: {target_id})\n"
                f"Reported by: {reporter_user.get('name','Unknown')} (id: {reporter_id})\n"
                f"Time: {datetime.utcnow().isoformat()} UTC\n\n"
                f"Use the buttons to view profile / ban or ignore the report."
            )

            admin_keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("View Profile", callback_data=f"admin_view_{target_id}"),
                    InlineKeyboardButton("Ban User", callback_data=f"admin_ban_{target_id}")
                ],
                [
                    InlineKeyboardButton("Ignore Report", callback_data=f"admin_ignore_{report_id}")
                ]
            ])

            if ADMIN_CHANNEL_ID:
                await context.bot.send_message(chat_id=ADMIN_CHANNEL_ID, text=admin_text, reply_markup=admin_keyboard)
            else:
                # fallback: DM each admin
                for aid in ADMIN_IDS:
                    try:
                        await context.bot.send_message(chat_id=aid, text=admin_text, reply_markup=admin_keyboard)
                    except Exception:
                        logger.exception("Failed to DM admin %s about report %s", aid, report_id)
        except Exception:
            logger.exception("Failed to notify admins about report %s", report_id)

        return

    if data.startswith("admin_view_"):
        # Only admins may use admin actions
        if query.from_user.id not in ADMIN_IDS:
            await safe_edit_or_send_callback(query, "⛔ Only admins can use this.")
            return
        try:
            target_id = int(data.split("_", 2)[2])
        except Exception:
            await safe_edit_or_send_callback(query, "Invalid target.")
            return

        target = users_collection.find_one({"user_id": target_id})
        if not target:
            await safe_edit_or_send_callback(query, "User not found.")
            return

        photos = target.get("photos", [])
        caption = (
            f"{target.get('name','Unknown')}, {target.get('age','N/A')}\n"
            f"Dept: {target.get('department','N/A')} | Year: {target.get('year','N/A')}\n"
            f"{target.get('bio','No bio available')}\n"
            f"ID: {target_id}\n"
            f"Reported by: see reports collection"
        )

        admin_actions = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Ban User", callback_data=f"admin_ban_{target_id}"),
                InlineKeyboardButton("Back to Admin Panel", callback_data="admin_panel")
            ]
        ])
        try:
            if photos:
                await query.message.reply_photo(photo=photos[-1], caption=caption, reply_markup=admin_actions)
            else:
                await query.message.reply_text(caption, reply_markup=admin_actions)
        except Exception:
            logger.exception("Failed to send admin view profile for %s", target_id)
        return

    if data.startswith("admin_ban_"):
        if query.from_user.id not in ADMIN_IDS:
            await safe_edit_or_send_callback(query, "⛔ Only admins can perform this action.")
            return
        try:
            target_id = int(data.split("_", 2)[2])
        except Exception:
            await safe_edit_or_send_callback(query, "Invalid target.")
            return
        users_collection.update_one({"user_id": target_id}, {"$set": {"banned": True}})
        await safe_edit_or_send_callback(query, f"User {target_id} has been banned.")
        try:
            await context.bot.send_message(target_id, "You have been banned from AAU-LinkUp by the admins.")
        except Exception:
            logger.debug("Couldn't DM user about ban (they may not have started the bot).")
        return

    if data.startswith("admin_ignore_"):
        if query.from_user.id not in ADMIN_IDS:
            await safe_edit_or_send_callback(query, "⛔ Only admins can perform this action.")
            return
        try:
            report_id = data.split("_", 2)[2]
            oid = ObjectId(report_id)
        except Exception:
            await safe_edit_or_send_callback(query, "Invalid report id.")
            return
        try:
            reports_collection.update_one({"_id": oid}, {"$set": {"status": "ignored", "reviewed_by": query.from_user.id, "reviewed_at": datetime.utcnow()}})
            await safe_edit_or_send_callback(query, f"Report {report_id} marked as ignored.")
        except Exception:
            logger.exception("Failed to mark report %s as ignored", report_id)
            await safe_edit_or_send_callback(query, "Failed to mark report as ignored.")
        return

    if data == "choice_verify_email":
        users_collection.update_one({"user_id": user_id}, {"$set": {"step": "awaiting_email_input"}})
        await safe_edit_or_send_callback(query, "Please enter your university email (e.g., firstname.lastname-ug@aau.edu.et):")
        return

    if data == "choice_skip_email":
        users_collection.update_one({"user_id": user_id}, {"$set": {"step": "done"}})
        done_text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "   🎉 *All Set!*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Your profile is live! 💫\n"
            "Start discovering other AAU students now."
        )
        await safe_edit_or_send_callback(query, done_text, parse_mode="Markdown")
        await show_main_menu(update, context)
        return

    await safe_edit_or_send_callback(query, "Unknown action. Use the menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]]))

# ------------------- PROFILE DISPLAY -------------------
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function can be called by callback_query or by a normal message
    user_id = update.callback_query.from_user.id if update.callback_query else update.effective_user.id
    # persist username
    if update.callback_query:
        upsert_tg_username(user_id, update.callback_query.from_user.username)
    else:
        upsert_tg_username(user_id, update.effective_user.username)

    user = users_collection.find_one({"user_id": user_id})
    if not user:
        await safe_edit_or_send_message(update, "No profile found.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Main Menu", callback_data="main_menu")]]))
        return

    user = ensure_user_doc(user)
    verified_badge = " 🛡" if user.get("is_verified") else ""
    gender_icon = "🙋‍♂️" if user.get('gender') == 'male' else "🙋‍♀️" if user.get('gender') == 'female' else "👤"
    text = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {gender_icon} *{user.get('name')}*{verified_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎓  {user.get('department')}  ·  📅 {user.get('year')}\n"
        f"🎂  {user.get('age')} years old\n\n"
        f"📝 _{user.get('bio', 'No bio yet')}_\n\n"
        f"❤️ *{len(user.get('liked_by', []))}* likes received"
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Profile", callback_data="edit_profile"), InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
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
        [InlineKeyboardButton("💖 Find Match", callback_data="find_match"), InlineKeyboardButton("👤 Profile", callback_data="view_profile")],
        [InlineKeyboardButton("✏️ Edit", callback_data="edit_profile"), InlineKeyboardButton("🏆 Ranks", callback_data="leaderboard")],
        [InlineKeyboardButton("❓ Help", callback_data="help_command")],
    ]
    if update.effective_user.id in ADMIN_IDS:
        keyboard.append([InlineKeyboardButton("🛠 Admin", callback_data="admin_panel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    menu_text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "    💎 *UniMatch Menu*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "What would you like to do?"
    )
    await safe_edit_or_send_message(update, menu_text, reply_markup=reply_markup, parse_mode="Markdown")

# ------------------- MATCH SYSTEM -------------------
# ------------------- FIND MATCH -------------------
async def find_match(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    # persist username on match actions
    upsert_tg_username(user_id, query.from_user.username)

    user = ensure_user_doc(users_collection.find_one({"user_id": user_id}))

    search_query = {"user_id": {"$ne": user_id}, "step": "done", "banned": {"$ne": True}}
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
        # Also skip banned users (defense-in-depth)
        if c.get("banned"):
            return False
        return True

    filtered = [c for c in candidates if eligible(c)]

    back_keyboard = [[InlineKeyboardButton("🔙 Back to Menu", callback_data="main_menu")]]
    if not filtered:
        empty_text = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "   😢 *No New Profiles*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "You've seen everyone for now!\n"
            "Check back soon for new faces. 💫"
        )
        await safe_edit_or_send_callback(query, empty_text, reply_markup=InlineKeyboardMarkup(back_keyboard), parse_mode="Markdown")
        return

    candidate = random.choice(filtered)
    v_badge = " 🛡" if candidate.get("is_verified") else ""
    gender_icon = "🙋‍♂️" if candidate.get('gender') == 'male' else "🙋‍♀️" if candidate.get('gender') == 'female' else "👤"
    caption = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {gender_icon} *{candidate.get('name')}*{v_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎓  {candidate.get('department')}  ·  📅 {candidate.get('year')}\n"
        f"🎂  {candidate.get('age')} years old\n\n"
        f"📝 _{candidate.get('bio', 'No bio')}_"
    )

    photos = candidate.get("photos", [])
    match_keyboard = [
        [InlineKeyboardButton("❤️ Like", callback_data=f"like_{candidate.get('user_id')}"),
         InlineKeyboardButton("👎 Pass", callback_data=f"skip_{candidate.get('user_id')}")],
        [InlineKeyboardButton("🚫 Report", callback_data=f"report_{candidate.get('user_id')}"),
         InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
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
# ------------------- LIKE HANDLER -------------------
# ------------------- LIKE HANDLER -------------------
async def handle_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    else:
        return

    user_id = query.from_user.id
    # persist username of the user pressing like
    upsert_tg_username(user_id, query.from_user.username)

    if not query.data or "_" not in query.data:
        await query.answer("Invalid action")
        return

    try:
        liked_id = int(query.data.split("_", 1)[1])
    except Exception:
        await query.answer("Invalid target.")
        return

    if liked_id == user_id:
        await query.answer("You can't like yourself.")
        return

    # Load both users
    liker = ensure_user_doc(users_collection.find_one({"user_id": user_id}))
    liked = ensure_user_doc(users_collection.find_one({"user_id": liked_id}))
    if not liked.get("user_id"):
        await query.answer("User not found.")
        return

    # Prevent duplicate likes
    if liked_id in (liker.get("likes") or []):
        await query.answer("You've already connected with this user.")
        await find_match(update, context)
        return

    # Update likes and liked_by
    users_collection.update_one({"user_id": user_id}, {"$addToSet": {"likes": liked_id}})
    users_collection.update_one({"user_id": liked_id}, {"$addToSet": {"liked_by": user_id}})

    liked_doc = users_collection.find_one({"user_id": liked_id})
    liked_name = liked_doc.get("name", "Someone")
    liked_likes = liked_doc.get("likes", [])
    mutual = user_id in liked_likes

    await query.answer(f"You liked {liked_name} ❤️")

    if user_id in liked_doc.get("likes", []):
        liker_doc = users_collection.find_one({"user_id": user_id})
        liker_name = liker_doc.get("name", "Someone")
        liked_tg = liked_doc.get("tg_username")
        liker_tg = liker_doc.get("tg_username")
        try:
            mention_for_liker = f"@{liked_tg}" if liked_tg else liked_name
            mention_for_liked = f"@{liker_tg}" if liker_tg else liker_name
            match_msg_liker = (
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "   💞 *It's a Match!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"You and *{liked_name}* liked each other! 🎉\n\n"
                f"📩 Say hi → {mention_for_liker}"
            )
            await context.bot.send_message(user_id, match_msg_liker, parse_mode="Markdown")
        except Exception:
            pass
        try:
            match_msg_liked = (
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "   💞 *It's a Match!*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"You and *{liker_name}* liked each other! 🎉\n\n"
                f"📩 Say hi → {mention_for_liked}"
            )
            await context.bot.send_message(liked_id, match_msg_liked, parse_mode="Markdown")
        except Exception:
            logger.exception("Failed to deliver next notification after mutual like for %s", liked_id)
        return

    # If not mutual, queue and maybe notify (rate-limited)
    try:
        await handle_new_like_notification(user_id, liked_id, context)
    except Exception:
        logger.exception("Failed to handle new like notification for %s -> %s", user_id, liked_id)

    # Move to next match
    await find_match(update, context)




async def show_liker_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # persist username on this callback too
    upsert_tg_username(query.from_user.id, query.from_user.username)

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
    v_badge = " 🛡" if liker.get("is_verified") else ""
    gender_icon = "🙋‍♂️" if liker.get('gender') == 'male' else "🙋‍♀️" if liker.get('gender') == 'female' else "👤"
    caption = (
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {gender_icon} *{liker.get('name', 'Unknown')}*{v_badge}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🎓  {liker.get('department', 'N/A')}  ·  📅 {liker.get('year', 'N/A')}\n"
        f"🎂  {liker.get('age', 'N/A')} years old\n\n"
        f"📝 _{liker.get('bio', 'No bio available')}_"
    )

    match_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("❤️ Like Back", callback_data=f"like_{liker_id}"),
            InlineKeyboardButton("👎 Pass", callback_data=f"skip_{liker_id}")
        ],
        [
            InlineKeyboardButton("🚫 Report", callback_data=f"report_{liker_id}"),
            InlineKeyboardButton("🔙 Menu", callback_data="main_menu")
        ]
    ])

    # Mark the corresponding sent notification as responded (they viewed)
    try:
        await mark_notifications_responded(query.from_user.id, liker_id=liker_id, response_type="viewed")
        # After responding, if there are queued notifications, try to deliver the latest immediately
        await try_deliver_next_notification(query.from_user.id, context)
    except Exception:
        logger.exception("Error marking/delivering notifications after show_liker_profile for %s", query.from_user.id)

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

    medals = ["🥇", "🥈", "🥉"] + ["  "] * 7
    msg = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "   🏆 *Leaderboard*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )
    msg += "*🙋‍♂️ Top Males*\n"
    if top_males:
        for i, u in enumerate(top_males, 0):
            medal = medals[i] if i < len(medals) else "  "
            msg += f"{medal} {i+1}. *{u.get('name','Unknown')}* — ❤️ {len(u.get('liked_by', []))}\n"
    else:
        msg += "_No profiles yet_\n"

    msg += "\n*🙋‍♀️ Top Females*\n"
    if top_females:
        for i, u in enumerate(top_females, 0):
            medal = medals[i] if i < len(medals) else "  "
            msg += f"{medal} {i+1}. *{u.get('name','Unknown')}* — ❤️ {len(u.get('liked_by', []))}\n"
    else:
        msg += "_No profiles yet_\n"

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
        [InlineKeyboardButton("📊 Leaderboard", callback_data="leaderboard"), InlineKeyboardButton("📢 Broadcast", callback_data="broadcast")],
        [InlineKeyboardButton("❗ Reports", callback_data="admin_list_reports")],
        [InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]
    ]
    admin_text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "   🛠 *Admin Panel*\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await safe_edit_or_send_message(update, admin_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

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
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "   ❓ *Help & Info*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "💖 *Find Match* — discover other AAU students\n"
        "👤 *Profile* — view your profile card\n"
        "✏️ *Edit* — update your info or photos\n"
        "🏆 *Ranks* — see the most popular profiles\n\n"
        "Use /start to begin your journey.\n"
        "Need help? DM → @Urcoder21"
    )
    keyboard = [[InlineKeyboardButton("🔙 Menu", callback_data="main_menu")]]
    await safe_edit_or_send_message(update, help_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


# ------------------- IGNORE LIKE HANDLER -------------------
async def ignore_like(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simple acknowledgement for the 'skip' button on like notification
    if update.callback_query:
        await update.callback_query.answer("Skipped ❤️")
        # mark sent notifications as responded/ignored
        try:
            await mark_notifications_responded(update.callback_query.from_user.id, response_type="ignored")
            # After ignoring, attempt to deliver next queued notification immediately
            await try_deliver_next_notification(update.callback_query.from_user.id, context)
        except Exception:
            logger.exception("Failed to mark/deliver notifications after ignore_like for %s", update.callback_query.from_user.id)

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
    # It's important that specific callback patterns are added before the generic handler below.
    app.add_handler(CallbackQueryHandler(show_liker_profile, pattern=r"^show_liker_"))
    app.add_handler(CallbackQueryHandler(handle_like, pattern=r"^like_"))
    app.add_handler(CallbackQueryHandler(ignore_like, pattern="ignore_like"))

    # Admin action handlers
    app.add_handler(CallbackQueryHandler(lambda u, c: None, pattern=r"^admin_list_reports$"))  # placeholder if you want to implement listing
    app.add_handler(CallbackQueryHandler(handle_buttons, pattern=r"^admin_view_"))  # route to handle_buttons for admin_view_
    app.add_handler(CallbackQueryHandler(handle_buttons, pattern=r"^admin_ban_"))  # route to handle_buttons for admin_ban_
    app.add_handler(CallbackQueryHandler(handle_buttons, pattern=r"^admin_ignore_"))  # route to handle_buttons for admin_ignore_

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
