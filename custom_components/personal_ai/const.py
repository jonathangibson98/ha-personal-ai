"""Constants for the Personal AI integration."""

import logging
import re
from enum import StrEnum

from homeassistant.const import CONF_LLM_HASS_API, CONF_PROMPT
from homeassistant.helpers import llm

DOMAIN = "personal_ai"
LOGGER = logging.getLogger(__package__)


class PersonalAiConfigKey:
    """Configuration keys for Personal AI."""

    BASE_URL = "base_url"
    CONVERSATION_MODE = "conversation_mode"
    GENERATE_DATA = "generate_data"
    FILENAME_TEMPLATE = "filename_template"
    GENERATE_IMAGE = "generate_image"
    IMAGE_FORMAT = "image_format"
    IMAGE_QUALITY = "image_quality"
    IMAGE_RESPONSE_FORMAT = "image_response_format"
    IMAGE_SIZE = "image_size"
    MANUAL_PROMPTING = "manual_prompting"
    PARALLEL_TOOL_CALLS = "parallel_tool_calls"
    READ_WEBPAGES = "read_webpages"
    RECOMMENDED = "recommended"
    SAVE_IMAGES = "save_images"
    SAVE_PATH = "save_path"
    SEARCH_API_KEY = "search_api_key"
    SEARCH_ENGINE_ID = "search_engine_id"
    SEARCH_MAX_RESULTS = "search_max_results"
    SEARCH_PROVIDER = "search_provider"
    SERVER_NAME = "server_name"
    STRIP_EMOJIS = "strip_emojis"
    STRIP_EMPHASIS = "strip_emphasis"
    STRIP_LATEX = "strip_latex"
    SUPPORT_ATTACHMENTS = "support_attachments"
    TEMPERATURE = "temperature"
    WEB_SEARCH = "web_search"


class PersonalAiSubentryType(StrEnum):
    """Subentry types supported by Personal AI."""

    AI_IMAGE = "ai_image"
    AI_TASK_DATA = "ai_task_data"
    CONVERSATION = "conversation"


RECOMMENDED_CONVERSATION_OPTIONS = {
    PersonalAiConfigKey.RECOMMENDED: True,
    CONF_LLM_HASS_API: [llm.LLM_API_ASSIST],
    CONF_PROMPT: llm.DEFAULT_INSTRUCTIONS_PROMPT,
}

# --- Conversation modes ------------------------------------------------------
MODE_HYBRID = "hybrid"  # general chat + home control; the model picks per message
MODE_HOME = "home"  # classic Assist behaviour, focused on the house
MODE_CHAT = "chat"  # plain AI conversation, no access to home entities
CONVERSATION_MODES = (MODE_HYBRID, MODE_HOME, MODE_CHAT)
DEFAULT_CONVERSATION_MODE = MODE_HYBRID

DATE_LINE = "Current date and time: {{ now().strftime('%A %d %B %Y, %H:%M') }}."
VOICE_LINE = (
    "Your replies may be read aloud, so keep them short and conversational, with no "
    "Markdown, emojis, bullet lists or web addresses unless the user asks for detail."
)

MODE_PROMPTS = {
    MODE_HYBRID: (
        "You are a friendly, knowledgeable AI assistant that also looks after the user's smart "
        "home. Hold natural conversations and answer general questions (facts, advice, writing, "
        "maths, coding, recommendations, small talk) from your own knowledge. The home is only "
        "one of the things you help with: never refuse or steer away from a question because it "
        "is not about the house, and never say you can only talk about the home. Use the smart "
        "home tools only when the user wants to control a device or asks about the current "
        "state of the home.\n" + VOICE_LINE + "\n" + DATE_LINE
    ),
    MODE_HOME: llm.DEFAULT_INSTRUCTIONS_PROMPT,
    MODE_CHAT: (
        "You are a friendly, knowledgeable AI assistant. Hold natural conversations and answer "
        "any question from your own knowledge: facts, advice, writing, maths, coding, "
        "recommendations, small talk. You have no access to the user's home or devices, so if "
        "asked to control or check something in the house, say that this assistant cannot do "
        "that.\n" + VOICE_LINE + "\n" + DATE_LINE
    ),
}

# --- Web search -------------------------------------------------------------
SEARCH_PROVIDER_SERPER = "serper"  # Google results via serper.dev (works for new users)
SEARCH_PROVIDER_GOOGLE_CSE = "google_cse"  # Google Programmable Search (closes 1 Jan 2027)
SEARCH_PROVIDERS = (SEARCH_PROVIDER_SERPER, SEARCH_PROVIDER_GOOGLE_CSE)
DEFAULT_SEARCH_PROVIDER = SEARCH_PROVIDER_SERPER
DEFAULT_SEARCH_RESULTS = 5
WEB_SEARCH_API_PREFIX = f"{DOMAIN}_web_search_"

# --- AI Image task defaults -------------------------------------------------
# Default save folder lives under <config>/www so files are reachable at
# http://<ha>:8123/local/personal_ai/<file>. Users can point it anywhere that is
# inside HA's allowlist_external_dirs (e.g. /media/ai_images).
DEFAULT_IMAGE_SAVE_SUBDIR = "www/personal_ai"
DEFAULT_IMAGE_FILENAME_TEMPLATE = "{date}_{time}_{slug}"
DEFAULT_IMAGE_SIZE = "1024x1024"
DEFAULT_IMAGE_QUALITY = "auto"
DEFAULT_IMAGE_FORMAT = "png"
DEFAULT_IMAGE_RESPONSE_FORMAT = "b64_json"

IMAGE_SIZE_OPTIONS = ("auto", "256x256", "512x512", "1024x1024", "1024x1536", "1536x1024")
IMAGE_QUALITY_OPTIONS = ("auto", "low", "medium", "high", "standard", "hd")
IMAGE_FORMAT_OPTIONS = ("png", "jpeg", "webp")
# "omit" = don't send response_format at all (needed for gpt-image-1 style servers)
IMAGE_RESPONSE_FORMAT_OPTIONS = ("b64_json", "url", "omit")

EVENT_IMAGE_SAVED = f"{DOMAIN}_image_saved"

MAX_TOOL_ITERATIONS = 10
TIMEOUT = 10.0
IMAGE_TIMEOUT = 300.0  # image models are slow, esp. on local GPUs

CURRENCY_PATTERN = re.compile(r"\$\d+(?:,\d{3})*(?:\.\d+)?(?=\b|\s|[,\.;:!\?]|\Z)")
CURRENCY_PLACEHOLDER_PREFIX = "\x00CURRENCY_"
CURRENCY_PLACEHOLDER_SUFFIX = "\x00"

THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"

EMPHASIS_MARKERS = ("*", "_")
MAX_EMPHASIS_MARKER_LEN = 3

LATEX_MATH_SPAN = re.compile(
    r"""
    \$\$[\s\S]+?\$\$
  | \$(?!\s)(?:\\.|[^\$\n\\])+?(?<!\s)\$
  | \\\([\s\S]*?\\\)
  | \\\[[\s\S]*?\\\]
    """,
    re.VERBOSE,
)

GEMINI_MIME_TYPES_SUPPORTED = (
    "application/dart",
    "application/ecmascript",
    "application/gzip",
    "application/json",
    "application/msword",
    "application/pdf",
    "application/protobuf",
    "application/typescript",
    "application/vnd.apache.parquet",
    "application/vnd.curl",
    "application/vnd.ibm.secure-container",
    "application/vnd.jupyter",
    "application/vnd.ms-excel",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "application/x-csh",
    "application/x-hwp",
    "application/x-latex",
    "application/x-php",
    "application/x-powershell",
    "application/x-sh",
    "application/x-zsh",
    "application/xml",
    "application/zip",
    "audio/G722",
    "audio/L16",
    "audio/MP4A-LATM",
    "audio/aac",
    "audio/adts",
    "audio/aiff",
    "audio/amr",
    "audio/g723",
    "audio/iLBC",
    "audio/m4a",
    "audio/mpeg",
    "audio/ogg",
    "audio/pcm",
    "audio/wav",
    "audio/x-ac3",
    "audio/x-adpcm",
    "audio/x-caf",
    "audio/x-dca",
    "audio/x-eac3",
    "audio/x-flac",
    "audio/x-gsm",
    "audio/x-oma",
    "audio/x-speex",
    "audio/x-tta",
    "audio/x-voc",
    "audio/x-wavpack",
    "image/avif",
    "image/bmp",
    "image/gif",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/svg+xml",
    "image/tiff",
    "image/webp",
    "image/x-adobe-dng",
    "text/cache-manifest",
    "text/calendar",
    "text/css",
    "text/csv",
    "text/dns",
    "text/html",
    "text/javascript",
    "text/jsx",
    "text/markdown",
    "text/plain",
    "text/provenance-notation",
    "text/rtf",
    "text/sgml",
    "text/shaclc",
    "text/shex",
    "text/spdx",
    "text/tab-separated-values",
    "text/texmacs",
    "text/troff",
    "text/tsx",
    "text/turtle",
    "text/uri-list",
    "text/vcard",
    "text/vnd.graphviz",
    "text/vnd.in3d.3dml",
    "text/vnd.in3d.spot",
    "text/vnd.sun.j2me.app-descriptor",
    "text/vnd.wap.wml",
    "text/vnd.wap.wmlscript",
    "text/vtt",
    "text/wgsl",
    "text/x-asm",
    "text/x-bibtex",
    "text/x-boo",
    "text/x-c++hdr",
    "text/x-c++src",
    "text/x-cassandra",
    "text/x-chdr",
    "text/x-coffeescript",
    "text/x-component",
    "text/x-csharp",
    "text/x-csrc",
    "text/x-cuda",
    "text/x-d",
    "text/x-diff",
    "text/x-emacs-lisp",
    "text/x-erlang",
    "text/x-gff3",
    "text/x-go",
    "text/x-haskell",
    "text/x-java-properties",
    "text/x-java-source",
    "text/x-kotlin",
    "text/x-lilypond",
    "text/x-lisp",
    "text/x-literate-haskell",
    "text/x-lua",
    "text/x-moc",
    "text/x-objcsrc",
    "text/x-pascal",
    "text/x-perl",
    "text/x-perl-script",
    "text/x-python-script",
    "text/x-r-markdown",
    "text/x-rsrc",
    "text/x-rst",
    "text/x-ruby-script",
    "text/x-rust",
    "text/x-sass",
    "text/x-scala",
    "text/x-scheme",
    "text/x-scss",
    "text/x-setext",
    "text/x-sfv",
    "text/x-siesta",
    "text/x-sos",
    "text/x-sql",
    "text/x-swift",
    "text/x-tcl",
    "text/x-tex",
    "text/x-vbasic",
    "text/x-vcalendar",
    "text/yaml",
    "video/3gpp",
    "video/3gpp2",
    "video/avi",
    "video/mp4",
    "video/mpeg",
    "video/mpg",
    "video/quicktime",
    "video/webm",
    "video/x-flv",
    "video/x-m4v",
    "video/x-matroska",
    "video/x-ms-wmv",
)
