__version__ = "0.1.0"

# macOS 시스템 파이썬(LibreSSL)에서 뜨는 urllib3 경고 숨김 — 동작에는 영향 없음.
# urllib3을 import하면 그 시점에 경고가 발생하므로, import 없이 메시지로 필터링한다.
import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL.*")
