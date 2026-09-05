from groq import Groq

import os
GROQ_API_KEY = os.getenv("GROQ_API_KEY")   # same key as SEAI Ask
client = Groq(api_key=GROQ_API_KEY)

def transcribe_audio(audio_file_path: str) -> str:
    with open(audio_file_path, "rb") as audio_file:
        transcription = client.audio.transcriptions.create(
            model="whisper-large-v3",      # high quality, or use 'whisper-large-v3-turbo'
            file=audio_file,
            response_format="text"
        )
    return transcription.strip()