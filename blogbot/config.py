import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
NAVER_BLOG_ID = os.getenv("NAVER_BLOG_ID", "")
# Claude는 Haiku 4.5만 사용한다 (토큰 비용 절감 — 사용자 결정 2026-09-19).
# .env나 요청에 Sonnet/Opus 등이 들어와도 enforce_model()이 Haiku로 바꾼다.
CLAUDE_MODEL = "claude-haiku-4-5"


def enforce_model(model_id: str) -> str:
    """Claude 계열이면 무조건 Haiku 4.5로 고정. Kimi 등 다른 계열은 그대로."""
    if model_id and model_id.startswith("claude") and model_id != CLAUDE_MODEL:
        return CLAUDE_MODEL
    return model_id or CLAUDE_MODEL


MODEL = enforce_model(os.getenv("BLOG_MODEL", CLAUDE_MODEL))

# Anthropic 크레딧 부족 등으로 Claude를 못 쓸 때 자동 전환할 모델.
# kimi-k3는 한글 오타가 잦고 그때는 교정(Claude)도 못 쓰므로 오타가 적은 k2.6을 쓴다.
FALLBACK_MODEL = os.getenv("FALLBACK_MODEL", "kimi-k2.6")

# 자료 조사: 1순위 Kimi 최신(research.KIMI_RESEARCH_MODELS), 실패 시 이 모델(Haiku)로 조사.
RESEARCH_MODEL = CLAUDE_MODEL
# 이미지 후보를 실제로 보고 문단과 가장 맞는 것을 고르는 비전 모델 (저렴한 Haiku).
IMAGE_PICK_MODEL = CLAUDE_MODEL

# Kimi (Moonshot AI) — OpenAI 호환 API. 국제 리전 기본, 중국 리전은 KIMI_BASE_URL로 교체.
# 여러 관습 지원: KIMI_API_KEY, MOONSHOT_API_KEY, KIMI_MOONSHOT_API_KEY 순으로 조회.
KIMI_API_KEY = (
    os.getenv("KIMI_API_KEY", "")
    or os.getenv("MOONSHOT_API_KEY", "")
    or os.getenv("KIMI_MOONSHOT_API_KEY", "")
)
KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")

# 사용자가 프로젝트 밖에 통합 .env 파일을 두는 경우에도 인식하도록 보조 경로도 로드.
# (이미 처음에 프로젝트 .env를 load_dotenv로 로드했음 — 여기선 override=False로 보완만)
def _drive_root() -> Path:
    """구글 드라이브 데스크톱 앱의 '내 드라이브' 폴더(계정이 하나라고 가정, 없으면 기본 경로)."""
    found = sorted((Path.home() / "Library/CloudStorage").glob("GoogleDrive-*/내 드라이브"))
    return found[0] if found else Path.home() / "Library/CloudStorage/GoogleDrive/내 드라이브"


_EXTRA_ENV_PATHS = [_drive_root() / "Envronment/.env"]
import logging as _logging
_logging.getLogger("dotenv.main").setLevel(_logging.ERROR)  # 통합 .env 비정형 라인 경고 억제
for _p in _EXTRA_ENV_PATHS:
    try:
        import signal as _signal
        def _timeout_handler(signum, frame):
            raise TimeoutError("cloud path timeout")
        _old = _signal.signal(_signal.SIGALRM, _timeout_handler)
        _signal.alarm(2)  # 2초 안에 응답 없으면 포기
        try:
            exists = _p.exists()
        finally:
            _signal.alarm(0)
            _signal.signal(_signal.SIGALRM, _old)
        if exists:
            load_dotenv(_p, override=False, verbose=False)
    except (OSError, TimeoutError):
        # launchd 등 GUI 세션 밖에서 실행되면 클라우드 저장소(File Provider)
        # 파일에 접근 권한이 없거나 응답이 없을 수 있음 — 무시하고 계속
        pass

# 보조 로드 후 재조회 (첫 번째 조회 때 없었을 수 있음)
if not KIMI_API_KEY:
    KIMI_API_KEY = (
        os.getenv("KIMI_API_KEY", "")
        or os.getenv("MOONSHOT_API_KEY", "")
        or os.getenv("KIMI_MOONSHOT_API_KEY", "")
    )

# OpenAI — 블로그 사진 생성 (사용자 결정 2026-10-03: AI 생성을 기본으로)
#  · 문단 → 이미지 프롬프트·캡션 작성: gpt-5.6-luna (텍스트 전용, 1M 토큰당 $0.2/$1.2)
#  · 이미지 생성: gpt-image-2.5-flare (실측 1536x1024 medium 1장 ≈ $0.011, 약 10초)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_PROMPT_MODEL = os.getenv("OPENAI_PROMPT_MODEL", "gpt-5.6-luna")
OPENAI_IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
OPENAI_IMAGE_QUALITY = os.getenv("OPENAI_IMAGE_QUALITY", "medium")
OPENAI_IMAGE_SIZE = os.getenv("OPENAI_IMAGE_SIZE", "1536x1024")
# 글 끝에 'AI 생성 이미지 포함' 고지 한 줄을 넣는다(캡션에는 출처를 쓰지 않음, 2026-10-26 결정).
# 인공지능기본법 제31조(생성형 AI 결과물 표시) 취지 — 끄려면 .env에 AI_IMAGE_NOTICE=0
AI_IMAGE_NOTICE = os.getenv("AI_IMAGE_NOTICE", "1") != "0"
AI_NOTICE_TEXT = "※ 이 글의 사진은 AI로 생성한 이미지입니다."
# 역사 글은 위키미디어 공용의 실제 유물·회화·지도를 먼저 쓴다 (무료). 끄려면 .env에 0.
HISTORY_USE_WIKIMEDIA = os.getenv("HISTORY_USE_WIKIMEDIA", "1") != "0"
# 사진 공급원: openai(AI 생성) | stock(Pexels/Unsplash 무료 사진). 키가 없으면 stock으로.
# 무료 사진 검색 보강 (키는 무료 가입으로 발급)
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "")
# Cloudflare Workers AI — 하루 10,000 뉴런 무료 ≈ FLUX-1-schnell 약 150장
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
CF_IMAGE_MODEL = os.getenv("CF_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
IMAGE_PROVIDERS = (("openai", "AI 생성 (OpenAI)"),
                   ("cloudflare", "AI 생성 무료 (Cloudflare FLUX)"),
                   ("stock", "무료 스톡 사진 (Pexels·Pixabay)"))
IMAGE_PROVIDER = os.getenv("IMAGE_PROVIDER", "openai")
if IMAGE_PROVIDER == "openai" and not OPENAI_API_KEY:
    IMAGE_PROVIDER = "stock"


def resolve_image_provider(requested: str = None) -> str:
    """요청값을 검증해 실제로 쓸 공급원을 정한다 (OpenAI 키가 없으면 stock)."""
    p = (requested or IMAGE_PROVIDER or "stock").strip()
    if p not in dict(IMAGE_PROVIDERS):
        p = IMAGE_PROVIDER
    if p == "openai" and not OPENAI_API_KEY:
        p = "cloudflare" if cloudflare_ready() else "stock"
    if p == "cloudflare" and not cloudflare_ready():
        p = "stock"
    return p


def cloudflare_ready() -> bool:
    return bool(CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN)


# GUI 드롭다운·CLI 검증에 쓰는 모델 카탈로그. (id, 한글 설명)
# Claude는 Haiku 4.5 하나만 노출한다 (상위 모델은 비용 때문에 사용 안 함).
AVAILABLE_MODELS = [
    # Claude (Anthropic) — 한글 오타가 없어 교정 패스 불필요
    ("claude-haiku-4-5", "Haiku 4.5 · 저렴·빠름 (기본)"),
    # Kimi (Moonshot AI) — KIMI_API_KEY 미설정 시 GUI가 자동 비활성.
    # 계정별로 접근 가능한 모델이 다르니 https://api.moonshot.ai/v1/models 로 확인.
    ("kimi-k3", "Kimi K3 · Moonshot 최신 (한글 오타 있음, 자동 교정)"),
    ("kimi-k2.6", "Kimi K2.6 · Moonshot 주력"),
    ("kimi-k2.7-code", "Kimi K2.7 Code · 구조화 출력 특화"),
    ("kimi-k2.7-code-highspeed", "Kimi K2.7 Code 고속"),
]


def is_kimi_model(model_id: str) -> bool:
    return model_id.startswith("kimi") or model_id.startswith("moonshot")

# 독자 설정 (2026-10-28 사용자 결정): 출퇴근 시간대(06~09시, 18~22시)에 자동 예약을 시작하면 직장인,
# 그 외에는 여성 독자. 문체는 prompts/style_guide.md(여성) / prompts/audience_worker.md(직장인 페르소나 대체).
AUDIENCES = {
    "women": {"label": "40~65세 여성",
              "image": "Korean women in their 40s-60s and their families"},
    "worker": {"label": "40~65세 직장인",
               "image": "Korean office workers in their 40s-60s (commuters, offices, meetings, cafes)"},
}


def audience_for(now=None) -> str:
    import datetime as _dt

    h = (now or _dt.datetime.now()).hour
    return "worker" if (6 <= h < 9 or 18 <= h < 22) else "women"


def time_note(queries: bool = False) -> str:
    """모델 프롬프트에 붙이는 기준 시점 안내 — 모델은 자기가 아는 옛 연도를 '최신'으로 착각하기 쉽다.

    2026-10-30 사용자 지정: 최신 통계·트렌드·기업 실적은 올해(실행 시점의 연도)를 기준으로 반영한다.
    """
    import datetime as _dt

    d = _dt.date.today()
    y = d.year
    s = (f"\n\n[기준 시점] 오늘은 {y}년 {d.month}월 {d.day}일이다. '최신'·'올해'·'현재'는 {y}년을 기준으로 한다. "
         f"통계·시장 동향·트렌드·기업 실적은 {y}년 자료(분기·월 단위 포함)를 가장 먼저 쓰고, 없을 때만 가장 가까운 이전 확정치를 쓰되 "
         f"반드시 '{y - 1}년 기준'처럼 자료에 적힌 연도를 밝힌다. 지난 연도 수치를 '올해'·'최근'·'현재'로 쓰지 말고, "
         f"자료의 연도를 임의로 {y}년으로 바꾸지도 마라. 네가 기억하는 옛 수치로 최신 수치를 대신하지 마라. "
         "역사적 사실에는 적용하지 않는다.")
    if queries:
        s += (f" 통계·실적·동향·전망을 겨냥한 검색어에는 '{y}'를 넣어 최신 자료가 걸리게 하라"
              f"(최소 2개, 역사 주제는 제외).")
    return s


# 네이트 판 인기글 모음 파일 (구글 드라이브 — 사용자의 별도 수집 작업이 채운다, 2026-10-31).
# 일간: 매일 06시 1~10위 / 시간별: 2시간마다 1~5위. blogbot은 사이트에 접속하지 않고 이 파일만 읽는다.
_DRIVE = _drive_root()
PANN_DAILY_FILE = Path(os.getenv("PANN_DAILY_FILE", str(_DRIVE / "nate pann daily/nate-pann-daily-top10.md")))
PANN_HOUR_FILE = Path(os.getenv("PANN_HOUR_FILE", str(_DRIVE / "nate pann hour/nate-pann-top5.md")))

DRAFTS_DIR = ROOT / "drafts"
INBOX_DIR = ROOT / "inbox"
PROCESSED_DIR = INBOX_DIR / "processed"
STYLE_GUIDE = ROOT / "prompts" / "style_guide.md"
NAVER_PROFILE_DIR = ROOT / ".naver_profile"
NEWS_PROFILE_DIR = ROOT / ".news_profile"

# 글 분야 — 네이버 블로그 카테고리(역사·문명/경제·투자/AI·테크/건강·라이프/사회·문화, 2026-10-05)와
# 1:1로 맞춘다. 기존 초안 호환을 위해 기존 3개 이름은 그대로 두고, 네이버 카테고리와는 이름으로 짝짓는다.
CATEGORIES = {"economy": "경제", "history": "역사", "life": "라이프",
              "tech": "AI·테크", "society": "사회·문화"}

# 글감 수집용 뉴욕타임스 RSS 피드 (헤드라인·요약은 무료 공개)
NYT_FEEDS = {
    "life": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Well.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/FashionandStyle.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Travel.xml",
    ],
    "economy": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Economy.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
    ],
    "history": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Books.xml",
    ],
    "tech": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/PersonalTech.xml",
    ],
    "society": [
        "https://rss.nytimes.com/services/xml/rss/nyt/US.xml",
        "https://rss.nytimes.com/services/xml/rss/nyt/Arts.xml",
    ],
}
