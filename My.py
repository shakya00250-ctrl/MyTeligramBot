#!/usr/bin/env python3
"""
StudyBot — Telegram bot for Classes 9–12 (single file, async, PTB v20+)
---------------------------------------------------------------------
NOW WITH:
- Class → Subject → Category navigation (9/10/11/12; PCM/PCB/Commerce covered)
- Hindi/English UI toggle
- Search & Smart Search filters (class, subject, category, lang, keyword)
- Views + Download counter
- Bookmarks (/bookmark, /mybookmarks, remove buttons)
- Daily Suggestion push (per-user subscribe / unsubscribe)
- Quiz/MCQ (per subject), scoring & per-user **Leaderboard**
- Admin: /addjson, /remove, /broadcast, /backup
- Multimedia-friendly items (type: link/pdf/image/video)
- Optional **Voice Notes** (TTS via gTTS if available)

How to run (Termux/PC)
1) pip install python-telegram-bot==20.7 apscheduler==3.10.4 gTTS==2.5.1
2) export BOT_TOKEN=123:ABC  (या TOKEN में पेस्ट करें)
3) वैकल्पिक: export ADMIN_ID=YOUR_TELEGRAM_USER_ID
4) python bot.py

Data files (auto-created):
- materials.json — study materials
- users.json — per-user bookmarks, points, daily subscription, quiz state
"""
from __future__ import annotations

import os
import re
import io
import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputFile,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ----------------------- CONFIG -----------------------
TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")
DATA_FILE = Path("materials.json")
USERS_FILE = Path("users.json")
ADMINS = {int(os.getenv("ADMIN_ID", "0"))}  # put your Telegram user id, optional

SUPPORTED_CLASSES = ["9", "10", "11", "12"]
CATEGORIES = [
    "Notes",
    "PYQs",
    "Sample Papers",
    "Syllabus",
    "Formulas",
    "NCERT Solutions",
    "Important Questions",
]

CLASS_SUBJECTS = {
    "9": ["Maths", "Science", "English", "Hindi", "Social Science"],
    "10": ["Maths", "Science", "English", "Hindi", "Social Science"],
    "11": ["Physics", "Chemistry", "Maths", "Biology", "English", "Hindi", "Accounts", "Business Studies", "Economics"],
    "12": ["Physics", "Chemistry", "Maths", "Biology", "English", "Hindi", "Accounts", "Business Studies", "Economics"],
}

LANGS = ["English", "Hindi"]

# ----------------------- MODELS -----------------------
@dataclass
class Item:
    id: str
    class_: str  # "9", "10", "11", "12"
    subject: str
    category: str
    title: str
    lang: str  # "English" or "Hindi"
    url: str
    added_at: str  # ISO timestamp
    views: int = 0
    downloads: int = 0
    media_type: str = "link"  # link|pdf|image|video

    @staticmethod
    def from_dict(d: dict) -> "Item":
        return Item(
            id=d["id"],
            class_=d["class_"],
            subject=d["subject"],
            category=d["category"],
            title=d["title"],
            lang=d.get("lang", "English"),
            url=d["url"],
            added_at=d.get("added_at", datetime.utcnow().isoformat()),
            views=int(d.get("views", 0)),
            downloads=int(d.get("downloads", 0)),
            media_type=d.get("media_type", "link"),
        )

# ----------------------- STORAGE -----------------------
class Store:
    def __init__(self, file: Path):
        self.file = file
        self.items: Dict[str, Item] = {}
        self._load()
        if not self.items:
            self._seed_sample_data()
            self._save()

    def _load(self):
        if self.file.exists():
            try:
                raw = json.loads(self.file.read_text())
                self.items = {i["id"]: Item.from_dict(i) for i in raw.get("items", [])}
            except Exception as e:
                logging.exception("Failed to load data: %s", e)
                self.items = {}
        else:
            self.items = {}

    def _save(self):
        data = {"items": [asdict(it) for it in self.items.values()]}
        self.file.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _seed_sample_data(self):
        now = datetime.utcnow().isoformat()
        seed: List[Item] = []
        for cls, subjects in CLASS_SUBJECTS.items():
            for subj in subjects:
                for cat in CATEGORIES:
                    for lang in LANGS:
                        it = Item(
                            id=f"{cls}_{subj}_{cat}_{lang}",
                            class_=cls,
                            subject=subj,
                            category=cat,
                            title=f"Class {cls} {subj} {cat} ({lang})",
                            lang=lang,
                            url=f"https://example.com/{cls}/{subj}/{cat}/{lang}",
                            added_at=now,
                            media_type="link",
                        )
                        seed.append(it)
        for it in seed:
            self.items[it.id] = it

    # Query helpers
    def list_classes(self) -> List[str]:
        return SUPPORTED_CLASSES

    def list_subjects(self, class_: str) -> List[str]:
        subs = sorted({it.subject for it in self.items.values() if it.class_ == class_})
        return subs or CLASS_SUBJECTS.get(class_, [])

    def list_categories(self, class_: str, subject: str) -> List[str]:
        cats = sorted({it.category for it in self.items.values() if it.class_ == class_ and it.subject == subject})
        return cats or CATEGORIES

    def list_items(self, class_: str, subject: str, category: str, lang: Optional[str]) -> List[Item]:
        out = [
            it for it in self.items.values()
            if it.class_ == class_ and it.subject == subject and it.category == category and (lang is None or it.lang == lang)
        ]
        return sorted(out, key=lambda x: (x.added_at, x.views, x.downloads), reverse=True)

    def top_latest(self, limit: int = 10) -> List[Item]:
        return sorted(self.items.values(), key=lambda x: x.added_at, reverse=True)[:limit]

    def inc_view(self, item_id: str):
        if item_id in self.items:
            self.items[item_id].views += 1
            self._save()

    def inc_download(self, item_id: str):
        if item_id in self.items:
            self.items[item_id].downloads += 1
            self._save()

    def search(self, query: str, lang: Optional[str]) -> List[Item]:
        q = query.lower().strip()
        res = [
            it for it in self.items.values()
            if (q in it.title.lower() or q in it.subject.lower() or q in it.category.lower())
            and (lang is None or it.lang == lang)
        ]
        return sorted(res, key=lambda x: (x.class_, x.subject, x.category, -x.views, -x.downloads))

    def smart_search(self, params: Dict[str, str]) -> List[Item]:
        def ok(it: Item) -> bool:
            if "class" in params and it.class_ != params["class"]:
                return False
            if "subject" in params and it.subject.lower() != params["subject"].lower():
                return False
            if "category" in params and it.category.lower() != params["category"].lower():
                return False
            if "lang" in params and it.lang.lower() != params["lang"].lower():
                return False
            if "keyword" in params:
                k = params["keyword"].lower()
                if k not in it.title.lower():
                    return False
            return True
        res = [it for it in self.items.values() if ok(it)]
        return sorted(res, key=lambda x: (x.class_, x.subject, x.category, -x.views, -x.downloads))

    def add_from_json(self, items: List[dict]) -> int:
        count = 0
        for d in items:
            it = Item.from_dict(d)
            self.items[it.id] = it
            count += 1
        self._save()
        return count

store = Store(DATA_FILE)

# ----------------------- USERS DB -----------------------
class Users:
    def __init__(self, file: Path):
        self.file = file
        self.data: Dict[str, Any] = {}
        self._load()

    def _load(self):
        if self.file.exists():
            try:
                self.data = json.loads(self.file.read_text())
            except Exception:
                logging.exception("Failed to load users.json, starting fresh")
                self.data = {}
        else:
            self.data = {}

    def _save(self):
        self.file.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))

    def ensure_user(self, uid: int):
        s = str(uid)
        if s not in self.data:
            self.data[s] = {
                "lang": "hi",
                "bookmarks": [],
                "points": 0,
                "daily": False,
                "quiz": {},
            }
            self._save()

    def set_lang(self, uid: int, lang: str):
        self.ensure_user(uid)
        self.data[str(uid)]["lang"] = lang
        self._save()

    def get_lang(self, uid: int) -> str:
        self.ensure_user(uid)
        return self.data[str(uid)].get("lang", "hi")

    def add_points(self, uid: int, pts: int):
        self.ensure_user(uid)
        self.data[str(uid)]["points"] += pts
        self._save()

    def points(self, uid: int) -> int:
        self.ensure_user(uid)
        return int(self.data[str(uid)].get("points", 0))

    def subscribe_daily(self, uid: int, flag: bool):
        self.ensure_user(uid)
        self.data[str(uid)]["daily"] = flag
        self._save()

    def daily_users(self) -> List[int]:
        return [int(uid) for uid, d in self.data.items() if d.get("daily")] 

    # Bookmarks
    def bookmark(self, uid: int, item_id: str):
        self.ensure_user(uid)
        b = self.data[str(uid)]["bookmarks"]
        if item_id not in b:
            b.append(item_id)
            self._save()

    def unbookmark(self, uid: int, item_id: str):
        self.ensure_user(uid)
        b = self.data[str(uid)]["bookmarks"]
        if item_id in b:
            b.remove(item_id)
            self._save()

    def list_bookmarks(self, uid: int) -> List[str]:
        self.ensure_user(uid)
        return list(self.data[str(uid)]["bookmarks"])

    # Quiz state per user
    def get_quiz(self, uid: int) -> dict:
        self.ensure_user(uid)
        return self.data[str(uid)].setdefault("quiz", {})

    def set_quiz(self, uid: int, q: dict):
        self.ensure_user(uid)
        self.data[str(uid)]["quiz"] = q
        self._save()

users = Users(USERS_FILE)

# ----------------------- I18N -----------------------
TEXT = {
    "hi": {
        "start": "👋 नमस्ते! StudyBot में आपका स्वागत है। अपनी भाषा चुनें:",
        "home": "📚 कक्षा चुनें या नीचे दिए गए विकल्पों का उपयोग करें:",
        "choose_class": "कक्षा चुनें",
        "choose_subject": "विषय चुनें",
        "choose_category": "श्रेणी चुनें",
        "no_items": "क्षमा करें, इस सेक्शन में सामग्री नहीं मिली।",
        "latest": "🆕 हाल ही में जोड़ी गई सामग्री:",
        "search_hint": "🔎 /search <keywords> या /s class=12 subject=physics keyword=electrostatics",
        "item": "*{title}*\nकक्षा {class_} · {subject} · {category} · {lang}\n👁️ {views} views · ⬇️ {downloads} downloads",
        "open": "खोलें",
        "mark_dl": "⬇️ डाउनलोड मार्क करें",
        "back": "🔙 पीछे",
        "stats": "📈 सबसे अधिक देखी गई सामग्री:",
        "added": "✅ {n} सामग्री जोड़ी गई।",
        "not_admin": "यह कमांड केवल एडमिन के लिए है।",
        "bm_added": "🔖 बुकमार्क किया गया!",
        "bm_removed": "❌ बुकमार्क हटाया गया।",
        "daily_on": "🗓️ Daily suggestion चालू कर दी गई है।",
        "daily_off": "🛑 Daily suggestion बंद कर दी गई है।",
        "quiz_start": "🧠 {subj} Quiz शुरू! {n} प्रश्न। विकल्प चुनें:",
        "quiz_end": "✅ Quiz समाप्त! Score: {score}/{n}",
        "no_bm": "आपके पास कोई बुकमार्क नहीं है।",
        "leader": "🏆 लीडरबोर्ड:",
    },
    "en": {
        "start": "👋 Welcome to StudyBot! Choose your language:",
        "home": "📚 Pick a class or use options below:",
        "choose_class": "Choose Class",
        "choose_subject": "Choose Subject",
        "choose_category": "Choose Category",
        "no_items": "Sorry, no material found here.",
        "latest": "🆕 Recently added materials:",
        "search_hint": "🔎 /search <keywords> or /s class=12 subject=physics keyword=electrostatics",
        "item": "*{title}*\nClass {class_} · {subject} · {category} · {lang}\n👁️ {views} views · ⬇️ {downloads} downloads",
        "open": "Open",
        "mark_dl": "⬇️ Mark Downloaded",
        "back": "🔙 Back",
        "stats": "📈 Most viewed materials:",
        "added": "✅ {n} items added.",
        "not_admin": "This command is admin-only.",
        "bm_added": "🔖 Bookmarked!",
        "bm_removed": "❌ Bookmark removed.",
        "daily_on": "🗓️ Daily suggestion enabled.",
        "daily_off": "🛑 Daily suggestion disabled.",
        "quiz_start": "🧠 {subj} quiz started! {n} questions.",
        "quiz_end": "✅ Quiz finished! Score: {score}/{n}",
        "no_bm": "You have no bookmarks yet.",
        "leader": "🏆 Leaderboard:",
    },
}

# In-memory language preference facade delegates to users.json
USER_LANG: Dict[int, str] = {}

def L(user_id: int) -> str:
    return users.get_lang(user_id)

# ----------------------- QUIZ BANK (tiny demo) -----------------------
QUIZ_BANK = {
    "Maths": [
        ("What is the value of (a+b)^2?", ["a^2 + b^2", "a^2 + 2ab + b^2", "2ab", "a^2 - 2ab + b^2"], 1),
    ],
    "Physics": [
        ("SI unit of force?", ["Newton", "Joule", "Pascal", "Watt"], 0),
    ],
    "Chemistry": [
        ("Atomic number represents?", ["Neutrons", "Protons", "Electrons in last shell", "Mass number"], 1),
    ],
    "Biology": [
        ("Powerhouse of the cell?", ["Nucleus", "Mitochondria", "Ribosome", "Chloroplast"], 1),
    ],
    "English": [
        ("Choose the correct tense: 'She ____ to school.'", ["go", "goes", "gone", "going"], 1),
    ],
    "Social Science": [
        ("India became Republic in?", ["1947", "1950", "1952", "1962"], 1),
    ],
}

# ----------------------- UI BUILDERS -----------------------

def lang_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 हिंदी", callback_data="LANG|hi"), InlineKeyboardButton("🇬🇧 English", callback_data="LANG|en")],
        [InlineKeyboardButton("📚 Browse", callback_data="HOME")],
        [InlineKeyboardButton("🆕 Latest", callback_data="LATEST"), InlineKeyboardButton("🔎 Search", callback_data="SEARCH_HELP")],
    ])


def home_keyboard(lang: str) -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(f"Class {c}", callback_data=f"CLS|{c}") for c in SUPPORTED_CLASSES]
    return InlineKeyboardMarkup([
        row1,
        [InlineKeyboardButton("🆕 Latest", callback_data="LATEST"), InlineKeyboardButton("🔖 Bookmarks", callback_data="BM_LIST")],
        [InlineKeyboardButton("🧠 Quiz", callback_data="QUIZ_MENU"), InlineKeyboardButton("🏆 Leaderboard", callback_data="LEADER")],
        [InlineKeyboardButton("🌐 Language / भाषा", callback_data="LANGSEL")],
    ])


def subjects_keyboard(class_: str, lang: str) -> InlineKeyboardMarkup:
    subs = store.list_subjects(class_)
    buttons = [[InlineKeyboardButton(s, callback_data=f"SUB|{class_}|{s}")] for s in subs]
    buttons.append([InlineKeyboardButton(TEXT[lang]["back"], callback_data="HOME")])
    return InlineKeyboardMarkup(buttons)


def categories_keyboard(class_: str, subject: str, lang: str) -> InlineKeyboardMarkup:
    cats = store.list_categories(class_, subject)
    buttons = [[InlineKeyboardButton(c, callback_data=f"CAT|{class_}|{subject}|{c}")] for c in cats]
    buttons.append([InlineKeyboardButton(TEXT[lang]["back"], callback_data=f"CLS|{class_}")])
    return InlineKeyboardMarkup(buttons)


def items_keyboard(items: List[Item], lang: str, back_data: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for it in items[:10]:
        rows.append([InlineKeyboardButton(f"🔗 {it.title[:48]} (⬇️{it.downloads})", callback_data=f"ITM|{it.id}")])
    rows.append([InlineKeyboardButton(TEXT[lang]["back"], callback_data=back_data)])
    return InlineKeyboardMarkup(rows)


def item_open_keyboard(it: Item, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔗 {TEXT[lang]['open']}", url=it.url)],
        [InlineKeyboardButton(TEXT[lang]['mark_dl'], callback_data=f"DL|{it.id}"), InlineKeyboardButton("🔖", callback_data=f"BM|{it.id}")],
        [InlineKeyboardButton(TEXT[lang]["back"], callback_data="HOME")],
    ])

# ----------------------- HELPERS -----------------------
SMART_RE = re.compile(r"(\w+)=([^\s]+)")

def parse_smart(s: str) -> Dict[str, str]:
    params = {k.lower(): v for k, v in SMART_RE.findall(s)}
    if "keyword" not in params:
        # remaining words not in key=value -> keyword
        leftover = SMART_RE.sub("", s).strip()
        if leftover:
            params["keyword"] = leftover
    return params

async def send_item_view(query_msg, it: Item, lang: str):
    caption = TEXT[lang]["item"].format(
        title=it.title,
        class_=it.class_,
        subject=it.subject,
        category=it.category,
        lang=it.lang,
        views=it.views,
        downloads=it.downloads,
    )
    try:
        await query_msg.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=item_open_keyboard(it, lang), disable_web_page_preview=False)
    except Exception:
        await query_msg.edit_message_text(caption, reply_markup=item_open_keyboard(it, lang))

# Optional TTS via gTTS
try:
    from gtts import gTTS
    async def tts_bytes(text: str) -> bytes:
        t = gTTS(text=text, lang='en')
        buf = io.BytesIO()
        t.write_to_fp(buf)
        buf.seek(0)
        return buf.read()
except Exception:
    async def tts_bytes(text: str) -> bytes:
        return b""

# ----------------------- HANDLERS -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.ensure_user(update.effective_user.id)
    lang = L(update.effective_user.id)
    t = TEXT[lang]
    await update.message.reply_text(t["start"], reply_markup=lang_keyboard())

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    t = TEXT[lang]
    msg = (
        f"{t['home']}\n\n"
        "• /search <keywords>\n"
        "• /s class=12 subject=physics keyword=electrostatics\n"
        "• /latest, /stats, /leader\n"
        "• /bookmark <item_id>, /mybookmarks\n"
        "• /daily_on, /daily_off\n"
        "• /quiz <subject>\n"
        "• /language\n"
        f"\n{t['search_hint']}"
    )
    await update.message.reply_text(msg, reply_markup=home_keyboard(lang))

async def language_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Choose / भाषा चुनें:", reply_markup=lang_keyboard())

async def latest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = L(user_id)
    t = TEXT[lang]
    items = store.top_latest(10)
    if not items:
        await update.message.reply_text(t["no_items"]) 
        return
    await update.message.reply_text(t["latest"], reply_markup=items_keyboard(items, lang, back_data="HOME"))

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = L(user_id)
    t = TEXT[lang]
    top = sorted(store.items.values(), key=lambda x: (x.views, x.downloads), reverse=True)[:10]
    if not top:
        await update.message.reply_text(t["no_items"]) 
        return
    lines = [t["stats"]]
    for i, it in enumerate(top, 1):
        lines.append(f"{i}. {it.title} (Class {it.class_}, {it.subject}, {it.category}) – {it.views}👁️ / {it.downloads}⬇️")
    await update.message.reply_text("\n".join(lines))

async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    lang = L(user_id)
    t = TEXT[lang]
    if not context.args:
        await update.message.reply_text(t["search_hint"]) 
        return
    q = " ".join(context.args)
    results = store.search(q, lang=None)
    users.add_points(user_id, 1)
    if not results:
        await update.message.reply_text(t["no_items"]) 
        return
    await update.message.reply_text(f"🔎 Results for: *{q}*", parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("Select a material:", reply_markup=items_keyboard(results[:25], lang, back_data="HOME"))

async def smart_search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    t = TEXT[lang]
    if not context.args:
        await update.message.reply_text(t["search_hint"]) 
        return
    params = parse_smart(" ".join(context.args))
    results = store.smart_search(params)
    users.add_points(update.effective_user.id, 1)
    if not results:
        await update.message.reply_text(t["no_items"]) 
        return
    await update.message.reply_text(f"🎯 Smart search: `{json.dumps(params)}`", parse_mode=ParseMode.MARKDOWN)
    await update.message.reply_text("Select a material:", reply_markup=items_keyboard(results[:25], lang, back_data="HOME"))

async def addjson_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = L(uid)
    t = TEXT[lang]
    if uid not in ADMINS or uid == 0:
        await update.message.reply_text(t["not_admin"]) 
        return
    if not context.args:
        await update.message.reply_text("Send JSON array of items as a reply to this message or use:\n/addjson <json>")
        return
    try:
        payload = json.loads(" ".join(context.args))
        if isinstance(payload, dict):
            payload = [payload]
        count = store.add_from_json(payload)
        await update.message.reply_text(t["added"].format(n=count))
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to parse JSON: {e}")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = L(uid)
    t = TEXT[lang]
    if uid not in ADMINS or uid == 0:
        await update.message.reply_text(t["not_admin"]) 
        return
    if not context.args:
        await update.message.reply_text("Usage: /remove <item_id>")
        return
    item_id = context.args[0]
    if item_id in store.items:
        del store.items[item_id]
        store._save()
        await update.message.reply_text(f"Removed {item_id}")
    else:
        await update.message.reply_text("Item not found")

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMINS or uid == 0:
        await update.message.reply_document(InputFile(DATA_FILE.open('rb'), filename='materials.json'))
        await update.message.reply_document(InputFile(USERS_FILE.open('rb') if USERS_FILE.exists() else io.BytesIO(b'{}'), filename='users.json'))
        return
    # Admins get both too
    await update.message.reply_document(InputFile(DATA_FILE.open('rb'), filename='materials.json'))
    await update.message.reply_document(InputFile(USERS_FILE.open('rb') if USERS_FILE.exists() else io.BytesIO(b'{}'), filename='users.json'))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = L(uid)
    if uid not in ADMINS or uid == 0:
        await update.message.reply_text(TEXT[lang]["not_admin"]) 
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    # naive broadcast to daily subscribers
    count = 0
    for u in users.daily_users():
        try:
            await context.bot.send_message(chat_id=u, text=f"📢 {msg}")
            count += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {count} users")

# Bookmarks
async def bookmark_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: /bookmark <item_id>")
        return
    iid = context.args[0]
    if iid in store.items:
        users.bookmark(update.effective_user.id, iid)
        users.add_points(update.effective_user.id, 1)
        await update.message.reply_text(TEXT[lang]["bm_added"]) 
    else:
        await update.message.reply_text("Item not found")

async def mybookmarks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lang = L(uid)
    ids = users.list_bookmarks(uid)
    if not ids:
        await update.message.reply_text(TEXT[lang]["no_bm"]) 
        return
    items = [store.items[i] for i in ids if i in store.items]
    await update.message.reply_text("🔖 Your bookmarks:", reply_markup=items_keyboard(items, lang, back_data="HOME"))

# Daily suggestions
async def daily_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.subscribe_daily(update.effective_user.id, True)
    await update.message.reply_text(TEXT[L(update.effective_user.id)]["daily_on"]) 

async def daily_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users.subscribe_daily(update.effective_user.id, False)
    await update.message.reply_text(TEXT[L(update.effective_user.id)]["daily_off"]) 

# Leaderboard
async def leader_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    # top 10 by points
    ranked = sorted(((int(uid), d.get("points", 0)) for uid, d in users.data.items()), key=lambda x: x[1], reverse=True)[:10]
    lines = [TEXT[lang]["leader"]]
    for i, (uid, pts) in enumerate(ranked, 1):
        lines.append(f"{i}. ID {uid} — {pts} pts")
    await update.message.reply_text("\n".join(lines))

# Quiz
async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang = L(update.effective_user.id)
    if not context.args:
        await update.message.reply_text("Usage: /quiz <subject>")
        return
    subj = " ".join(context.args).strip().title()
    bank = QUIZ_BANK.get(subj)
    if not bank:
        await update.message.reply_text("No quiz available for this subject.")
        return
    # build quiz state
    qs = [{"q": q, "opts": opts, "ans": ans} for (q, opts, ans) in bank]
    users.set_quiz(update.effective_user.id, {"subject": subj, "i": 0, "score": 0, "qs": qs})
    await update.message.reply_text(TEXT[lang]["quiz_start"].format(subj=subj, n=len(qs)))
    await send_next_quiz(update, context)

async def send_next_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    qstate = users.get_quiz(uid)
    i = qstate.get("i", 0)
    qs = qstate.get("qs", [])
    lang = L(uid)
    if i >= len(qs):
        score = qstate.get("score", 0)
        users.add_points(uid, score * 2)
        await context.bot.send_message(chat_id=uid, text=TEXT[lang]["quiz_end"].format(score=score, n=len(qs)))
        return
    cur = qs[i]
    buttons = [[InlineKeyboardButton(cur["opts"][k], callback_data=f"QZ|{i}|{k}")] for k in range(len(cur["opts"]))]
    buttons.append([InlineKeyboardButton(TEXT[lang]["back"], callback_data="HOME")])
    await context.bot.send_message(chat_id=uid, text=f"Q{i+1}. {cur['q']}", reply_markup=InlineKeyboardMarkup(buttons))
    
        # ----------------------- CALLBACKS -----------------------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = update.effective_user.id
    lang = L(uid)
    t = TEXT[lang]

    # navigation
    if data == "HOME":
        await query.edit_message_text(t["home"], reply_markup=home_keyboard(lang))
        return

    if data == "LANGSEL":
        await query.edit_message_text(TEXT[lang]["start"], reply_markup=lang_keyboard())
        return

    if data.startswith("LANG|"):
        _, new_lang = data.split("|", 1)
        users.set_lang(uid, new_lang)
        await query.edit_message_text(TEXT[new_lang]["home"], reply_markup=home_keyboard(new_lang))
        return

    if data == "LATEST":
        items = store.top_latest(10)
        if not items:
            await query.edit_message_text(t["no_items"]) 
            return
        await query.edit_message_text(t["latest"], reply_markup=items_keyboard(items, lang, back_data="HOME"))
        return

    if data == "SEARCH_HELP":
        await query.edit_message_text(TEXT[lang]["search_hint"], reply_markup=home_keyboard(lang))
        return

    if data == "BM_LIST":
        ids = users.list_bookmarks(uid)
        if not ids:
            await query.edit_message_text(TEXT[lang]["no_bm"], reply_markup=home_keyboard(lang))
            return
        items = [store.items[i] for i in ids if i in store.items]
        await query.edit_message_text("🔖 Your bookmarks:", reply_markup=items_keyboard(items, lang, back_data="HOME"))
        return

    if data == "QUIZ_MENU":
        subs = sorted(set(s for v in CLASS_SUBJECTS.values() for s in v))
        kb = [[InlineKeyboardButton(s, callback_data=f"QZSUB|{s}")] for s in subs if s in QUIZ_BANK]
        kb.append([InlineKeyboardButton(TEXT[lang]["back"], callback_data="HOME")])
        await query.edit_message_text("Choose subject for quiz:", reply_markup=InlineKeyboardMarkup(kb))
        return

    if data.startswith("QZSUB|"):
        _, subj = data.split("|", 1)
        bank = QUIZ_BANK.get(subj)
        if not bank:
            await query.edit_message_text("No quiz available.")
            return
        qs = [{"q": q, "opts": opts, "ans": ans} for (q, opts, ans) in bank]
        users.set_quiz(uid, {"subject": subj, "i": 0, "score": 0, "qs": qs})
        await query.edit_message_text(TEXT[lang]["quiz_start"].format(subj=subj, n=len(qs)))
        await send_next_quiz(update, context)
        return

    if data.startswith("QZ|"):
        _, i_str, opt_str = data.split("|", 2)
        i = int(i_str); opt = int(opt_str)
        qstate = users.get_quiz(uid)
        qs = qstate.get("qs", [])
        if 0 <= i < len(qs):
            ans = qs[i]["ans"]
            if opt == ans:
                qstate["score"] = qstate.get("score", 0) + 1
                users.add_points(uid, 1)
            qstate["i"] = i + 1
            users.set_quiz(uid, qstate)
            await send_next_quiz(update, context)
        return

    if data.startswith("CLS|"):
        _, cls = data.split("|", 1)
        await query.edit_message_text(TEXT[lang]["choose_subject"], reply_markup=subjects_keyboard(cls, lang))
        return

    if data.startswith("SUB|"):
        _, cls, subj = data.split("|", 2)
        await query.edit_message_text(TEXT[lang]["choose_category"], reply_markup=categories_keyboard(cls, subj, lang))
        return

    if data.startswith("CAT|"):
        _, cls, subj, cat = data.split("|", 3)
        items = store.list_items(cls, subj, cat, lang=None)
        if not items:
            await query.edit_message_text(TEXT[lang]["no_items"], reply_markup=categories_keyboard(cls, subj, lang))
            return
        await query.edit_message_text(f"{subj} · {cat}", reply_markup=items_keyboard(items, lang, back_data=f"SUB|{cls}|{subj}"))
        return

    if data.startswith("ITM|"):
        _, item_id = data.split("|", 1)
        it = store.items.get(item_id)
        if not it:
            await query.edit_message_text(TEXT[lang]["no_items"], reply_markup=home_keyboard(lang))
            return
        store.inc_view(item_id)
        users.add_points(uid, 1)
        await send_item_view(query, it, lang)
        return

    if data.startswith("DL|"):
        _, item_id = data.split("|", 1)
        store.inc_download(item_id)
        users.add_points(uid, 2)
        it = store.items.get(item_id)
        if it:
            await send_item_view(query, it, lang)
        return

    if data.startswith("BM|"):
        _, item_id = data.split("|", 1)
        if item_id in users.list_bookmarks(uid):
            users.unbookmark(uid, item_id)
            await query.answer(TEXT[lang]["bm_removed"], show_alert=False)
        else:
            users.bookmark(uid, item_id)
            await query.answer(TEXT[lang]["bm_added"], show_alert=False)
        return
      
      # ----------------------- DAILY SCHEDULER -----------------------
async def send_daily(context=None):
    for uid in users.daily_users():
        try:
            # Latest item pick karo
            items = store.top_latest(1) or list(store.items.values())
            if not items:
                continue
            it = items[0]

            caption = f"🌅 Daily pick:\n{it.title}"
            # Daily message bhejo
            await app.bot.send_message(chat_id=uid, text=caption)

        except Exception as e:
            print(f"Error sending daily to {uid}: {e}")

# ----------------------- APP -----------------------
async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Sorry, I didn’t understand that command.")


def build_app() -> Application:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("language", language_cmd))
    app.add_handler(CommandHandler("latest", latest_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("s", smart_search_cmd))
    app.add_handler(CommandHandler("addjson", addjson_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    app.add_handler(CommandHandler("bookmark", bookmark_cmd))
    app.add_handler(CommandHandler("mybookmarks", mybookmarks_cmd))

    app.add_handler(CommandHandler("daily_on", daily_on_cmd))
    app.add_handler(CommandHandler("daily_off", daily_off_cmd))

    app.add_handler(CommandHandler("leader", leader_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))

    app.add_handler(CallbackQueryHandler(on_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message))

    return app


import asyncio
from datetime import datetime, timezone
datetime.now(timezone.utc).isoformat()

def utcnow():
    return datetime.now(timezone.utc)

async def main():
    global scheduler
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("Please set BOT_TOKEN environment variable or paste it into TOKEN.")

    app = build_app()

    # Scheduler for daily suggestions at 08:00 (server time)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(send_daily, CronTrigger(hour=8, minute=0), args=[app.bot])
    scheduler.start()

    # PTB lifecycle
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # idle loop
    try:
        await asyncio.Event().wait()
    finally:
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
