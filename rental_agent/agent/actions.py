"""Finish explicit payment requests and detect promises with no queued work."""
import re

PAYMENT = re.compile(r"\b(?:payment|payement)\s+link\b|\blink to pay\b|\b(?:want|ready|like)\s+to\s+pay\b|\bcan i pay\b|رابط\s*الدفع|(?:عايز|أريد|اريد)\s*(?:أدفع|ادفع)", re.I)
REQUEST = re.compile(r"\b(?:send|generate|create|give|get|want|ready|like|can i pay)\b|ارسل|أرسل|ابعت|عايز|أريد|اريد", re.I)
EXCLUDE = re.compile(r"\b(?:not|don't|do not|never|cancel|refund|deposit|holding|why|what is|how does)\b|لا\s|استرداد|تأمين|وديعة", re.I)
PROMISE = re.compile(
    r"(?:i['’]ll|i will|let me|please allow me).{0,100}(?:send|generate|create|check|look up|fetch).{0,100}(?:link|quote|photo|picture|availability)"
    r"|(?:i['’]ll|i will).{0,50}(?:send|come back|get back).{0,60}(?:moment|shortly|soon)"
    r"|(?:سأرسل|هأبعت|هبعت).{0,60}(?:الرابط|الصور|قليلا|قليل)", re.I | re.S)


def wants_payment(message):
    # Only short, unambiguous requests bypass the model. A request to change a
    # car, dates, price or payment purpose must go through the normal tools.
    words = set("i me my the a an this that it for of to can could would you please now again new fresh send generate create give get want ready like pay payment payement link rental rent total current secure using stripe hello hi thanks thank أر‌سل ارسل أرسل ابعت لي لى رابط الدفع للإيجار للايجار عايز أريد اريد أدفع ادفع لو سمحت جديد دلوقتي".replace("أر‌سل ", "").split())
    tokens = re.findall(r"[^\W\d_]+", message.lower())
    return bool(PAYMENT.search(message) and REQUEST.search(message)
                and not EXCLUDE.search(message) and not re.search(r"\d", message)
                and set(tokens) <= words)


def unfinished(reply):
    # A conditional explanation is not a claim that background work is running.
    if re.search(r"\b(?:once|until|after|when|if)\b|بعد|عندما", reply, re.I):
        return False
    if not PROMISE.search(reply):
        return False
    return True


CORRECTION = (
    "Do not end this turn by promising to send a link, quote, photos, or an availability check later. "
    "No background follow-up job is scheduled. Complete the requested tool action now and deliver its result. "
    "If a required detail is missing, ask for that detail now; if the action failed, explain that failure."
)
SAFE_REPLY = "I could not complete that request in this reply. Please tell me which car or booking it concerns so I can check the next step."
