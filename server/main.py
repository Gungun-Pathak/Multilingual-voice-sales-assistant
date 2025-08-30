import requests
import re
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from langchain_core.messages import HumanMessage, AIMessage
from schemas import ChatRequest, ChatResponse, TTSRequest, SpecialDeal
from agent import chatbot_graph
from vector_store import populate_pinecone_data
from config import MURF_API_KEY, MURF_VOICE_ID

# --- FastAPI Application ---
app = FastAPI(title="Mobile Salesperson Chatbot API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Complete language to voice mapping
LANGUAGE_VOICE_MAP = {
    'en-US': 'en-US-natalie',      # English US
    'en-UK': 'en-UK-theo',         # English UK
    'es-ES': 'es-ES-elvira',       # Spanish
    'de-DE': 'de-DE-matthias',     # German
    'pt-BR': 'pt-BR-heitor',       # Portuguese
    'ja-JP': 'ja-JP-kenji',        # Japanese
    'ko-KR': 'ko-KR-gyeong',       # Korean
    'zh-CN': 'zh-CN-tao',          # Chinese
    'hi-IN': 'hi-IN-kabir',        # Hindi
    'ta-IN': 'ta-IN-iniya',        # Tamil
    'bn-IN': 'bn-IN-anwesha',      # Bengali
    'pl-PL': 'pl-PL-jacek',        # Polish
    # Add fallbacks for languages you might not have voices for yet
    'fr-FR': 'en-US-natalie',      # French fallback to English
    'it-IT': 'en-US-natalie',      # Italian fallback to English
    'ar-SA': 'en-US-natalie',      # Arabic fallback to English
}

def detect_language_from_text(text: str) -> str:
    """Detect language from text using character patterns."""
    language_patterns = {
        'hi-IN': r'[\u0900-\u097F]',  # Hindi
        'ko-KR': r'[\uAC00-\uD7AF]',  # Korean
        'ja-JP': r'[\u3040-\u309F\u30A0-\u30FF]',  # Japanese
        'zh-CN': r'[\u4E00-\u9FFF]',  # Chinese
        'es-ES': r'[áéíóúñ¿¡]',  # Spanish
        'fr-FR': r'[àâçéèêëîïôûùüÿœæ]',  # French
        'de-DE': r'[äöüß]',  # German
        'it-IT': r'[àèéìíîòóùú]',  # Italian
        'pt-BR': r'[ãõâêîôûáéíóúç]',  # Portuguese
        'ar-SA': r'[\u0600-\u06FF]',  # Arabic
        'ta-IN': r'[\u0B80-\u0BFF]',  # Tamil
        'bn-IN': r'[\u0980-\u09FF]',  # Bengali
        'pl-PL': r'[ąćęłńóśźż]',  # Polish
    }
    
    for lang_code, pattern in language_patterns.items():
        if re.search(pattern, text, re.IGNORECASE):
            return lang_code
    
    return 'en-US'  # Default to English

def generate_speech_sync(text: str, language: str = 'en-US', voice_id_override: str = None):
    """Generate speech with language-specific voice, allowing voice ID override."""
    if not MURF_API_KEY or not text.strip():
        return None

    # Priority: 1. Explicit voice_id_override, 2. Language mapping, 3. Default
    voice_id = voice_id_override or LANGUAGE_VOICE_MAP.get(language, MURF_VOICE_ID or 'en-US-natalie')
    
    print(f"Generating speech for language: {language} with voice: {voice_id}")
    
    url = "https://api.murf.ai/v1/speech/generate"
    headers = {
        "api-key": MURF_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "voiceId": voice_id,
        "format": "MP3",
        "sampleRate": 44100,
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        data = response.json()
        return data.get("audioFile")
    except requests.RequestException as e:
        print(f"Error calling Murf AI: {e}")
        return None

@app.on_event("startup")
def on_startup():
    """Populates Pinecone data on startup."""
    populate_pinecone_data()

@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    """Handles chat requests and generates speech in one go."""
    # Detect language from user message (override the request language if non-English detected)
    detected_language = detect_language_from_text(request.user_message)
    
    # Use detected language if it's not English, otherwise use the requested language
    final_language = detected_language if detected_language != 'en-US' else request.language
    
    print(f"Detected language: {detected_language}, Using language: {final_language}")
    
    # Add language context to the system prompt
    language_context = f"User is speaking in {final_language}. Respond in the same language."
    
    history_messages = [
        HumanMessage(content=msg['content']) if msg['role'] == 'user'
        else AIMessage(content=msg['content'])
        for msg in request.history
    ]

    initial_state = {
        "messages": [("system", language_context)] + history_messages + [HumanMessage(content=request.user_message)],
        "retrieved_products": {},
        "product_context_ids": [],
        "detected_language": final_language  # Pass the final language to state
    }
    
    final_state = await chatbot_graph.ainvoke(initial_state)
    final_answer_args = final_state["messages"][-1].tool_calls[0]['args']
    
    agent_text_response = final_answer_args['text']

    # Generate Audio with language-specific voice
    audio_url = generate_speech_sync(
        agent_text_response, 
        final_language,  # Use the final determined language
        request.voice_id
    )

    all_retrieved_products = {}
    for products_dict in final_state.get('__intermediate_steps__', []):
        if isinstance(products_dict, dict) and 'retrieved_products' in products_dict:
            all_retrieved_products.update(products_dict['retrieved_products'])
    all_retrieved_products.update(final_state.get('retrieved_products', {}))

    response_products = [
        all_retrieved_products[pid]
        for pid in final_answer_args.get('product_ids', [])
        if pid in all_retrieved_products
    ]

    response_deal = None
    if final_answer_args.get('deal_heading') and final_answer_args.get('deal_product_ids'):
        deal_products = [
            all_retrieved_products[pid]
            for pid in final_answer_args['deal_product_ids']
            if pid in all_retrieved_products
        ]
        if deal_products:
            response_deal = SpecialDeal(
                heading=final_answer_args['deal_heading'],
                deal_price=final_answer_args['deal_price'],
                products_involved=deal_products
            )

    return ChatResponse(
        text=agent_text_response,
        audio_url=audio_url,
        products=response_products,
        special_deal=response_deal
    )

@app.post("/generate-speech")
def generate_speech(req: TTSRequest):
    if not MURF_API_KEY:
        raise HTTPException(500, "Missing MURF_API_KEY env variable")
    if not req.text or not req.text.strip():
        raise HTTPException(400, "Text is required")
    
    # Use provided voice_id or determine from language
    voice_id = req.voice_id or LANGUAGE_VOICE_MAP.get(req.language, MURF_VOICE_ID)
    if not voice_id:
        raise HTTPException(400, "voice_id is required (or set MURF_VOICE_ID env var)")
    
    print("text:", req.text)
    print("voice_id:", voice_id)
    print("language:", req.language)

    url = "https://api.murf.ai/v1/speech/generate"
    headers = {
        "api-key": MURF_API_KEY,
        "Content-Type": "application/json",
    }
    
    payload = {
        "text": req.text,
        "voiceId": voice_id,
        "format": (req.format or "MP3").upper(),
        "sampleRate": req.sample_rate,
        "style": req.style,
        "encodeAsBase64": bool(req.encode_as_base64),
    }
    
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        audio_url = data.get("audioFile")
        audio_b64 = data.get("encodedAudio")

        print(audio_url)

        if not audio_url and not audio_b64:
            raise HTTPException(502, "Murf did not return audio")

        return {"audio_url": audio_url, "audio_base64": audio_b64, "meta": {
            "length_seconds": data.get("audioLengthInSeconds"),
            "consumed_chars": data.get("consumedCharacterCount"),
            "remaining_chars": data.get("remainingCharacterCount"),
            "warning": data.get("warning"),
        }}
    except requests.HTTPError as e:
        detail = e.response.text if e.response is not None else str(e)
        raise HTTPException(status_code=e.response.status_code if e.response else 502,
                            detail=f"Murf API error: {detail}")
    except requests.RequestException as e:
        raise HTTPException(502, f"Network error calling Murf: {e}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)