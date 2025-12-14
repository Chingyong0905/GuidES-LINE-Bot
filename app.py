# -*- coding: utf-8 -*-
import os
import re
from datetime import datetime
from urllib.parse import parse_qs

from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    TemplateSendMessage, ButtonsTemplate,
    PostbackAction, PostbackEvent
)

from groq import Groq

# === RAG ===
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

# === Firebase (RTDB) ===
import firebase_admin
from firebase_admin import credentials, db




# =============================================================================
# App
# =============================================================================
app = Flask(__name__)


# =============================================================================
# Configuration (use environment variables)
# =============================================================================
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET")

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")


firebase_enabled = True

# Debug: 是否每次回答後都附上選單（開發方便）
DEBUG_SHOW_MENU_AFTER_REPLY = os.environ.get("DEBUG_SHOW_MENU_AFTER_REPLY", "1") == "0"

missing = [k for k, v in {
    "LINE_CHANNEL_ACCESS_TOKEN": LINE_CHANNEL_ACCESS_TOKEN,
    "LINE_CHANNEL_SECRET": LINE_CHANNEL_SECRET,
    "GROQ_API_KEY": GROQ_API_KEY,
}.items() if not v]
if missing:
    raise RuntimeError(
        "Missing config in environment variables: " + ", ".join(missing) +
        "\nPlease set them before running."
    )

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
groq_client = Groq(api_key=GROQ_API_KEY)


# =============================================================================
# Mode settings
# =============================================================================
MODE_LABELS = {
    "department_announcement": "系所公告",
    "scholarship": "獎助學金資訊",
    "faculty_lab": "實驗室與師資介紹",
    "course_requirement": "修課規定",
}

# 本地狀態（輔助用）：重啟會清空；真正「暫存記憶」放 Firebase
user_state = {}  # { sender_id: {"mode": "..."} }

# 你的 faiss 路徑：與 app.py 同層 /faiss_db/faiss_db_xxx
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_FAISS_DIR = os.path.join(BASE_DIR, "faiss_db")
FAISS_DIR_BY_MODE = {
    "scholarship": os.path.join(BASE_FAISS_DIR, "faiss_db_scholarship"),
    "faculty_lab": os.path.join(BASE_FAISS_DIR, "faiss_db_faculty_lab"),
    "department_announcement": os.path.join(BASE_FAISS_DIR, "faiss_db_department_announcement"),
    "course_requirement": os.path.join(BASE_FAISS_DIR, "faiss_db_course_requirement"),
}


def get_sender_id(event) -> str:
    """
    用來識別「同一個聊天對象」的 key。
    - 私聊：通常有 user_id
    - 群聊/房間：有時 user_id 不一定取到；此時用 group/room 做降級 key
    """
    src = event.source
    uid = getattr(src, "user_id", None)
    if uid:
        return uid
    gid = getattr(src, "group_id", None)
    if gid:
        return f"group:{gid}"
    rid = getattr(src, "room_id", None)
    if rid:
        return f"room:{rid}"
    return "unknown_sender"


def build_mode_menu() -> TemplateSendMessage:
    return TemplateSendMessage(
        alt_text="GuidES 功能選單",
        template=ButtonsTemplate(
            title="GuidES",
            text="請選擇你要查詢的類別：",
            actions=[
                PostbackAction(label="系所公告", data="mode=department_announcement"),
                PostbackAction(label="獎助學金資訊", data="mode=scholarship"),
                PostbackAction(label="實驗室與師資介紹", data="mode=faculty_lab"),
                PostbackAction(label="修課規定", data="mode=course_requirement"),
            ],
        ),
    )


# =============================================================================
# Prompts
# =============================================================================
GENERAL_SYSTEM_PROMPT = (
    "你現在扮演的是個說繁體中文的台灣大學生，目的在於與同儕討論與回答同儕的問題。"
    "回答請適當分段與分行，避免整段黏在一起。"
)

RAG_SYSTEM_PROMPT = """
你是一個專業的「GuidES 系所資訊助理」。
請依據下方【參考資料】回答學生問題，並遵守下列規則：

【內容規則】
1) 只能根據參考資料回答；若資料不足或無關，請明確說：「系上資料庫目前沒有相關資訊」。
2) 不要猜測、不補充未出現在資料中的細節。

【格式規則（很重要）】
- 回答請適當分行，不要整段黏在一起。
- 優先用條列（• 或 1./2./3.）整理重點。
- 建議結構：
  第一行是查詢結論
  然後列 2–5 點相關内容的條列
  有URL的話就放上來
  注意事項：如有日期/資格/截止，獨立一行或條列
- 使用繁體中文，語氣親切且專業。
""".strip()


# =============================================================================
# Reply post-processing
# =============================================================================
def prettify_reply(text: str) -> str:
    if not text:
        return text
    t = text.replace("\r\n", "\n").strip()

    # 若完全沒有換行且很長：用標點切行當保險
    if "\n" not in t and len(t) >= 80:
        t = re.sub(r"([。！？；])\s*", r"\1\n", t)

    # 壓縮過多空行
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


# =============================================================================
# Firebase (Temp Memory) - enabled or not
# =============================================================================
firebase_enabled = False
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    FIREBASE_CRED_PATH = os.path.join(
        BASE_DIR,
        "guides-linebot-firebase-adminsdk-fbsvc-31f1c82802.json"
    )
    FIREBASE_DB_URL = "https://guides-linebot-default-rtdb.firebaseio.com"

    cred = credentials.Certificate(FIREBASE_CRED_PATH)
    firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})

    firebase_enabled = True
    print("✅ Firebase initialized.")
except Exception as e:
    print("❌ Firebase init failed:", repr(e))
    firebase_enabled = False



def _mem_base(sender_id: str) -> str:
    return f"TempMemory/{sender_id}"


def fb_set_mode(sender_id: str, mode: str):
    if not firebase_enabled:
        return
    try:
        db.reference(_mem_base(sender_id)).update({"mode": mode})
    except Exception as e:
        print("fb_set_mode error:", e)


def fb_get_mode(sender_id: str):
    if not firebase_enabled:
        return None
    try:
        return db.reference(_mem_base(sender_id) + "/mode").get()
    except Exception as e:
        print("fb_get_mode error:", e)
        return None


def fb_clear_history(sender_id: str):
    """
    A) 切換 mode 時清空舊記憶：刪掉 history 整個節點
    """
    if not firebase_enabled:
        return
    try:
        db.reference(_mem_base(sender_id) + "/history").delete()
    except Exception as e:
        print("fb_clear_history error:", e)


def fb_append_history(sender_id: str, role: str, content: str):
    if not firebase_enabled:
        return
    if role not in ("user", "assistant") or not content:
        return

    # 用毫秒時間戳作 key，天然可排序
    key = str(int(datetime.utcnow().timestamp() * 1000))
    try:
        db.reference(_mem_base(sender_id) + f"/history/{key}").set({
            "role": role,
            "content": content
        })
    except Exception as e:
        print("fb_append_history error:", e)


def fb_load_recent_history(sender_id: str, limit: int = 8):
    """
    讀取最近 N 則 history（同一個 sender_id，且只有「目前 mode」那份記憶）
    回傳 [{"role":"user","content":"..."}, ...] 按時間排序
    """
    if not firebase_enabled:
        return []
    try:
        data = db.reference(_mem_base(sender_id) + "/history").get() or {}
        items = sorted(data.items(), key=lambda x: x[0])
        last_items = items[-limit:] if len(items) > limit else items

        out = []
        for _, v in last_items:
            role = v.get("role")
            content = v.get("content")
            if role in ("user", "assistant") and content:
                out.append({"role": role, "content": content})
        return out
    except Exception as e:
        print("fb_load_recent_history error:", e)
        return []


def fb_trim_history(sender_id: str, keep: int = 8):
    """
    超過 keep 就刪掉最舊的，避免無限長
    """
    if not firebase_enabled:
        return
    try:
        ref = db.reference(_mem_base(sender_id) + "/history")
        data = ref.get() or {}
        keys = sorted(list(data.keys()))
        if len(keys) <= keep:
            return
        to_delete = keys[:len(keys) - keep]
        for k in to_delete:
            db.reference(_mem_base(sender_id) + f"/history/{k}").delete()
    except Exception as e:
        print("fb_trim_history error:", e)


# =============================================================================
# RAG: load multiple FAISS DBs
# =============================================================================
print("正在載入 Embedding 模型與 FAISS 資料庫...")
embedding_model = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")

vectorstores = {}
retrievers = {}

for mode, path in FAISS_DIR_BY_MODE.items():
    try:
        vs = FAISS.load_local(path, embeddings=embedding_model, allow_dangerous_deserialization=True)
        vectorstores[mode] = vs
        retrievers[mode] = vs.as_retriever(search_kwargs={"k": 3})
        print(f"✅ [{mode}] loaded: {path}")
    except Exception as e:
        vectorstores[mode] = None
        retrievers[mode] = None
        print(f"❌ [{mode}] load failed: {path} | {e}")


def generate_rag_response(sender_id: str, user_question: str, mode: str) -> str:
    retriever = retrievers.get(mode)
    if not retriever:
        label = MODE_LABELS.get(mode, mode)
        return f"⚠️ 系統維護中：[{label}] 資料庫尚未載入，請稍後再試或切換其他類別。"

    # 取同一個 sender_id 的暫存記憶（最多 8 則）
    history = fb_load_recent_history(sender_id, limit=8)

    # Retrieval
    try:
        docs = retriever.invoke(user_question)
        context_text = "\n\n".join([f"[資料片段]: {doc.page_content}" for doc in docs])
    except Exception as e:
        print(f"Retrieval Error ({mode}): {e}")
        context_text = "（檢索發生錯誤）"

    mode_label = MODE_LABELS.get(mode, mode)
    user_prompt = f"""
【查詢類別】：
{mode_label}

【參考資料】：
{context_text}

【學生問題】：
{user_question}
""".strip()

    messages = [{"role": "system", "content": RAG_SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    try:
        r = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.3,
        )
        ans = prettify_reply((r.choices[0].message.content or "").strip())

        # 寫入 Firebase 暫存記憶
        fb_append_history(sender_id, "user", user_question)
        fb_append_history(sender_id, "assistant", ans)
        fb_trim_history(sender_id, keep=8)

        return ans
    except Exception as e:
        print(f"Groq Error: {e}")
        return "抱歉，AI 思考時發生錯誤。"


def generate_general_response(user_text: str) -> str:
    try:
        r = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": GENERAL_SYSTEM_PROMPT},
                {"role": "user", "content": user_text}
            ],
            temperature=0.7,
        )
        return prettify_reply((r.choices[0].message.content or "").strip())
    except Exception as e:
        return f"Error: {e}"


# =============================================================================
# Health check
# =============================================================================
@app.route("/", methods=["GET"])
def health():
    loaded = [m for m, r in retrievers.items() if r is not None]
    missing_db = [m for m, r in retrievers.items() if r is None]
    fb = "enabled" if firebase_enabled else "disabled"
    return f"OK | faiss_loaded={loaded} faiss_missing={missing_db} | firebase={fb}"


# =============================================================================
# LINE webhook
# =============================================================================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check LINE_CHANNEL_SECRET.")
        abort(400)
    except Exception as e:
        print("Handler error:", repr(e))
        abort(500)

    return "OK"


# =============================================================================
# Postback handler (select mode)
# =============================================================================
@handler.add(PostbackEvent)
def handle_postback(event):
    sender_id = get_sender_id(event)
    data = event.postback.data or ""
    qs = parse_qs(data)
    mode = qs.get("mode", [None])[0]

    if mode not in MODE_LABELS:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="我沒有辨識到你的選擇，請再點一次。"))
        return

    # 取得目前 mode（本地優先；沒有就用 Firebase）
    prev_mode = user_state.get(sender_id, {}).get("mode")
    if not prev_mode:
        prev_mode = fb_get_mode(sender_id)

    # A) 只有「切換到新 mode」才清空舊記憶
    if prev_mode != mode:
        fb_clear_history(sender_id)

    # 記住新 mode（本地 + Firebase）
    user_state[sender_id] = {"mode": mode}
    fb_set_mode(sender_id, mode)

    label = MODE_LABELS[mode]
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(
            text=f"你選了【{label}】。\n"
                 f"請直接輸入你要查的問題。\n"
                 f"要切換類別請輸入ES。"
        )
    )


# =============================================================================
# Text handler (no @ needed for RAG)
# =============================================================================
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    sender_id = get_sender_id(event)
    text = (event.message.text or "").strip()
    print("Sender:", sender_id, "Text:", text)

    try:
        # 呼叫選單（任何時候）
        if text in ["@機器人", "選單", "menu", "功能", "開始", "start", "切換", "OK", "ES", "我沒了"]:
            line_bot_api.reply_message(event.reply_token, build_mode_menu())
            return

        # （可選）保留原本的翻譯/摘要指令
        if text.startswith("@翻譯 "):
            content = text[len("@翻譯 "):].strip()
            prompt = "將以下內容翻譯成繁體中文：\n" + content
            reply = generate_general_response(prompt)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        if text.startswith("@摘要 "):
            content = text[len("@摘要 "):].strip()
            prompt = "請用繁體中文總結以下內容（適當分行、條列重點）：\n" + content
            reply = generate_general_response(prompt)
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            return

        # 取得 mode（本地優先；沒有就去 Firebase 恢復）
        mode = user_state.get(sender_id, {}).get("mode")
        if not mode:
            mode = fb_get_mode(sender_id)
            if mode:
                user_state[sender_id] = {"mode": mode}

        # 如果仍沒有 mode → 引導選單
        if not mode or mode not in MODE_LABELS:
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text="請先選擇你要查詢的類別："),
                build_mode_menu()
            ])
            return

        # RAG
        reply = generate_rag_response(sender_id, text, mode=mode)

        if DEBUG_SHOW_MENU_AFTER_REPLY:
            # 可選1：回答後再附上選單，方便切換（開發/Debug）
            line_bot_api.reply_message(event.reply_token, [
                TextSendMessage(text=reply),
                build_mode_menu()
            ])
        else:
            # 可選2：回答後要輸入「選單」才可切換類別（正式使用體驗較乾淨）
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=reply + "\n\n（輸入ES可回到選單）")
            )


    except Exception as e:
        print("Unexpected error:", repr(e))
        # 開發期可開啟回傳錯誤
        # line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"系統錯誤: {e}"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
