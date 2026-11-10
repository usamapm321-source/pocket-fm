import os

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://dramafy:dramafy@localhost:5432/dramafy")
DRAMAFY_API = os.environ.get("DRAMAFY_API", "https://v2.api.dramafy.com")
DRAMAFY_SITE = os.environ.get("DRAMAFY_SITE", "https://www.dramafy.com")
SCRAPE_CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "8"))
SCRAPE_ON_STARTUP = os.environ.get("SCRAPE_ON_STARTUP", "true").lower() == "true"
# 0 disables the periodic re-scrape
SCRAPE_INTERVAL_HOURS = float(os.environ.get("SCRAPE_INTERVAL_HOURS", "24"))
PARROTFM_API = os.environ.get("PARROTFM_API", "https://api.parrotfm.app")
PARROTFM_SITE = os.environ.get("PARROTFM_SITE", "https://parrotfm.app")
AUDIODRAMA_SITE = os.environ.get("AUDIODRAMA_SITE", "https://www.audiodrama.com")
LEARNOUTLOUD_SITE = os.environ.get("LEARNOUTLOUD_SITE", "https://www.learnoutloud.com")
# LearnOutLoud sections to scrape, as "section:Category,..." (the category is stored on each show)
LEARNOUTLOUD_SECTIONS = dict(
    entry.split(":", 1) for entry in os.environ.get(
        "LEARNOUTLOUD_SECTIONS", "Free-Audiobooks:Audiobooks,Podcast-Directory:Podcasts,Free-Courses:Courses"
    ).split(",") if ":" in entry
)
# First N titles (A-Z) per section; 0 = every title
LEARNOUTLOUD_MAX_ITEMS_PER_SECTION = int(os.environ.get("LEARNOUTLOUD_MAX_ITEMS_PER_SECTION", "0"))
ARCHIVE_API = os.environ.get("ARCHIVE_API", "https://archive.org")
# archive.org collections to scrape, as "collection:Category,..." (the category is stored on each show)
ARCHIVE_COLLECTIONS = dict(
    entry.split(":", 1) for entry in os.environ.get(
        "ARCHIVE_COLLECTIONS", "oldtimeradio:Old Time Radio,librivoxaudio:Audiobooks"
    ).split(",") if ":" in entry
)
# Most-downloaded items first; 0 = every item in the collection
ARCHIVE_MAX_ITEMS_PER_COLLECTION = int(os.environ.get("ARCHIVE_MAX_ITEMS_PER_COLLECTION", "2000"))
# Transcripts (episode text). Workers transcribing at once; 0 = queue requests but never process them
TRANSCRIBE_WORKERS = int(os.environ.get("TRANSCRIBE_WORKERS", "1"))
# faster-whisper model: tiny | base | small | medium | large-v3 (bigger = more accurate, slower)
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")  # cpu | cuda | auto
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
# CPU threads per worker; 0 = faster-whisper's default
WHISPER_CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", "0"))
# Audio files larger than this are not transcribed
TRANSCRIBE_MAX_AUDIO_MB = int(os.environ.get("TRANSCRIBE_MAX_AUDIO_MB", "500"))
# Platforms whose every episode is queued for a transcript after each scrape, e.g. "parrotfm,dramafy"
TRANSCRIBE_AUTO_PLATFORMS = [p.strip() for p in os.environ.get("TRANSCRIBE_AUTO_PLATFORMS", "").split(",") if p.strip()]
