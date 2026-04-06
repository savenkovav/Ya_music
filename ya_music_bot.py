import json
import logging
import os
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed
try:
    from yandex_music import Client as YandexMusicClient
except Exception:  # pragma: no cover
    YandexMusicClient = None


YANDEX_DOMAINS = {"music.yandex.ru", "music.yandex.com"}
TRACK_RE_1 = re.compile(r"^/album/(?P<album_id>\d+)/track/(?P<track_id>\d+)")
TRACK_RE_2 = re.compile(r"^/track/(?P<track_id>\d+)")
NEXT_DATA_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
WINDOW_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", re.DOTALL)
MMSS_RE = re.compile(r"^(?P<minutes>\d{1,2}):(?P<seconds>\d{2})$")
RU_DURATION_RE = re.compile(r"(?:(\d+)\s*мин\w*)?.*?(?:(\d+)\s*сек\w*)?", re.IGNORECASE)
_CACHED_YM_CLIENT = None
_CACHED_YM_TOKEN = None


class ParserError(Exception):
    pass


class UnsupportedLinkError(ParserError):
    pass


class RegionBlockedError(ParserError):
    pass


class TrackNotFoundError(ParserError):
    pass


class CaptchaRequiredError(ParserError):
    pass


class YandexTokenNotConfiguredError(ParserError):
    pass


@dataclass
class TrackRef:
    url: str
    track_id: str
    album_id: Optional[str] = None


@dataclass
class TrackInfo:
    album_title: Optional[str]
    title: str
    artist: str
    duration_seconds: int
    image_url: Optional[str] = None

    @property
    def duration_mmss(self) -> str:
        minutes = self.duration_seconds // 60
        seconds = self.duration_seconds % 60
        return f"{minutes:02d}:{seconds:02d}"


def setup_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )
    # Подавляем очень шумные логи Selenium, чтобы не тратить время на лишний вывод.
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("selenium.webdriver.remote.remote_connection").setLevel(logging.WARNING)


def extract_track_ref(text: str) -> TrackRef:
    cleaned = text.strip()
    parsed = urlparse(cleaned)

    if parsed.scheme not in {"http", "https"}:
        raise UnsupportedLinkError("Ссылка должна начинаться с http:// или https://")
    if parsed.netloc not in YANDEX_DOMAINS:
        raise UnsupportedLinkError("Поддерживаются только ссылки на music.yandex.ru")

    path = parsed.path.rstrip("/")
    match = TRACK_RE_1.match(path)
    if match:
        return TrackRef(url=cleaned, track_id=match.group("track_id"), album_id=match.group("album_id"))

    match = TRACK_RE_2.match(path)
    if match:
        return TrackRef(url=cleaned, track_id=match.group("track_id"))

    raise UnsupportedLinkError("Не удалось извлечь ID трека из ссылки")


def _get_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _get_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(1),
    retry=retry_if_exception_type(WebDriverException),
    reraise=True,
)
def fetch_track_page(url: str) -> str:
    logger = logging.getLogger(__name__)
    request_timeout = _get_env_int("REQUEST_TIMEOUT", 10)
    page_load_timeout = _get_env_int("SELENIUM_PAGELOAD_TIMEOUT", max(20, request_timeout * 2))
    wait_timeout = _get_env_int("SELENIUM_WAIT_TIMEOUT", max(10, request_timeout))

    options = webdriver.ChromeOptions()
    user_agent = os.getenv("USER_AGENT", "Mozilla/5.0").strip()
    if user_agent:
        options.add_argument(f"--user-agent={user_agent}")
    if _get_env_bool("SELENIUM_HEADLESS", True):
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ru-RU")

    chrome_bin = (os.getenv("CHROME_BIN") or "").strip()
    if chrome_bin:
        options.binary_location = chrome_bin

    service_path = (os.getenv("CHROMEDRIVER_PATH") or "").strip()
    service = Service(executable_path=service_path) if service_path else Service()

    driver = webdriver.Chrome(service=service, options=options)
    try:
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(url)

        try:
            WebDriverWait(driver, wait_timeout).until(
                lambda d: (
                    d.find_elements(By.CSS_SELECTOR, "script[type='application/ld+json']")
                    or d.find_elements(By.CSS_SELECTOR, "span.PageHeaderTitle_title__caKyB")
                    or d.find_elements(By.CSS_SELECTOR, "[class*='TrackModalTitle_trackTitle__']")
                    or d.find_elements(By.CSS_SELECTOR, "form[action*='showcaptcha']")
                )
            )
        except TimeoutException:
            logger.debug("Selenium wait timeout: продолжаем с текущим DOM")

        extra_ms = _get_env_int("SELENIUM_EXTRA_WAIT_MS", 0)
        if extra_ms > 0:
            time.sleep(extra_ms / 1000.0)

        html = driver.page_source or ""
        final_url = (driver.current_url or "").lower()
    finally:
        driver.quit()

    if "/showcaptcha" in final_url or "showcaptcha" in html.lower():
        raise CaptchaRequiredError("Яндекс запросил капчу для этой ссылки")
    if "Яндекс Музыка недоступна в вашем регионе" in html:
        raise RegionBlockedError("Яндекс Музыка недоступна в вашем регионе")
    if not html.strip():
        raise ParserError("Получен пустой ответ от сервера")
    return html


def _yandex_api_track_specs(track_ref: TrackRef) -> list[str]:
    specs: list[str] = []
    if track_ref.album_id:
        specs.append(f"{track_ref.track_id}:{track_ref.album_id}")
    specs.append(track_ref.track_id)
    seen: set[str] = set()
    out: list[str] = []
    for s in specs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _trackinfo_from_yandex_track(track) -> Optional[TrackInfo]:
    if not track:
        return None
    title = (getattr(track, "title", None) or "").strip()
    artists_raw = getattr(track, "artists", None) or []
    artists = [getattr(artist, "name", "").strip() for artist in artists_raw if getattr(artist, "name", "").strip()]
    artist = ", ".join(artists).strip()
    duration_ms = getattr(track, "duration_ms", None)
    duration_seconds = _duration_to_seconds(duration_ms)

    image_url: Optional[str] = None
    albums = getattr(track, "albums", None) or []
    album_title: Optional[str] = None
    if albums:
        album_title = (getattr(albums[0], "title", None) or "").strip() or None

    cover_uri = getattr(track, "cover_uri", None)
    if not cover_uri and albums:
        cover_uri = getattr(albums[0], "cover_uri", None)
    if isinstance(cover_uri, str) and cover_uri.strip():
        normalized = cover_uri.strip().lstrip("/")
        if normalized.startswith("http://") or normalized.startswith("https://"):
            image_url = normalized
        else:
            image_url = f"https://{normalized}"
        image_url = image_url.replace("%%", "600x600")

    if title and artist and duration_seconds is not None:
        return TrackInfo(
            album_title=album_title,
            title=title,
            artist=artist,
            duration_seconds=duration_seconds,
            image_url=image_url,
        )
    return None


def fetch_track_info_via_api(track_ref: TrackRef, token: str) -> Optional[TrackInfo]:
    global _CACHED_YM_CLIENT, _CACHED_YM_TOKEN
    logger = logging.getLogger(__name__)
    if not token:
        return None
    if YandexMusicClient is None:
        logger.warning("Библиотека yandex-music недоступна, API fallback пропущен")
        return None

    try:
        if _CACHED_YM_CLIENT is None or _CACHED_YM_TOKEN != token:
            _CACHED_YM_CLIENT = YandexMusicClient(token).init()
            _CACHED_YM_TOKEN = token
        client = _CACHED_YM_CLIENT
        for spec in _yandex_api_track_specs(track_ref):
            tracks = client.tracks([spec])
            if not tracks:
                continue
            info = _trackinfo_from_yandex_track(tracks[0])
            if info:
                logger.info("Успех: API fallback YANDEX_MUSIC_TOKEN (id=%s)", spec)
                return info
    except Exception:
        logger.exception("Ошибка API fallback yandex-music")
    return None


def _duration_to_seconds(value) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        if value > 100000:
            return value // 1000
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.isdigit():
            ivalue = int(value)
            return ivalue // 1000 if ivalue > 100000 else ivalue
        m = re.match(r"^PT(?:(\d+)M)?(?:(\d+)S)?$", value)
        if m:
            minutes = int(m.group(1) or 0)
            seconds = int(m.group(2) or 0)
            return minutes * 60 + seconds
    return None


def _parse_duration_label_ru(value: str) -> Optional[int]:
    text = (value or "").strip().lower()
    if not text:
        return None
    match = RU_DURATION_RE.search(text)
    if not match:
        return None
    minutes = int(match.group(1) or 0)
    seconds = int(match.group(2) or 0)
    if minutes == 0 and seconds == 0:
        return None
    return minutes * 60 + seconds


def _parse_duration_mmss(value: str) -> Optional[int]:
    text = (value or "").strip()
    match = MMSS_RE.match(text)
    if not match:
        return None
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    return minutes * 60 + seconds


def _clean_title_og(og_title: str) -> tuple[Optional[str], Optional[str]]:
    val = unescape(og_title).strip()
    if not val:
        return None, None
    parts = re.split(r"\s[—–\-]\s", val, maxsplit=1)
    if len(parts) == 2:
        artist, title = parts[0].strip(), parts[1].strip()
        if title.endswith(" | Яндекс Музыка"):
            title = title.removesuffix(" | Яндекс Музыка").strip()
        return title or None, artist or None
    return None, None


def _extract_from_ld_json(soup: BeautifulSoup) -> Optional[TrackInfo]:
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw = script.string or script.text or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        entries: list = []
        if isinstance(data, list):
            entries.extend(data)
        elif isinstance(data, dict):
            if "@graph" in data and isinstance(data["@graph"], list):
                entries.extend(data["@graph"])
            else:
                entries.append(data)
        for item in entries:
            if not isinstance(item, dict):
                continue
            title = item.get("name")
            by_artist = item.get("byArtist")
            artist = None
            if isinstance(by_artist, dict):
                artist = by_artist.get("name")
            duration = _duration_to_seconds(item.get("duration"))
            album_obj = item.get("inAlbum") or item.get("isPartOf")
            album_title: Optional[str] = None
            if isinstance(album_obj, dict):
                album_title = (album_obj.get("name") or "").strip() or None
            if title and artist and duration is not None:
                return TrackInfo(
                    album_title=album_title,
                    title=title.strip(),
                    artist=artist.strip(),
                    duration_seconds=duration,
                )
    return None


def _deep_find(obj, keys: set[str], out: list):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and value:
                out.append(value)
            _deep_find(value, keys, out)
    elif isinstance(obj, list):
        for item in obj:
            _deep_find(item, keys, out)


def _extract_from_json_blob(blob: str) -> Optional[TrackInfo]:
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None

    titles: list[str] = []
    artists: list[str] = []
    durations: list[int] = []

    title_candidates: list = []
    artist_candidates: list = []
    duration_candidates: list = []
    _deep_find(data, {"title", "name"}, title_candidates)
    _deep_find(data, {"artists", "artist", "byArtist"}, artist_candidates)
    _deep_find(data, {"durationMs", "duration", "durationSec"}, duration_candidates)

    for t in title_candidates:
        if isinstance(t, str) and t.strip():
            normalized = t.strip()
            if normalized.lower() not in {"яндекс музыка", "yandex music"}:
                titles.append(normalized)

    for a in artist_candidates:
        if isinstance(a, str) and a.strip():
            artists.append(a.strip())
        elif isinstance(a, list):
            names = []
            for item in a:
                if isinstance(item, dict) and item.get("name"):
                    names.append(str(item["name"]).strip())
                elif isinstance(item, str):
                    names.append(item.strip())
            if names:
                artists.append(", ".join([x for x in names if x]))
        elif isinstance(a, dict):
            if a.get("name"):
                artists.append(str(a["name"]).strip())

    for d in duration_candidates:
        sec = _duration_to_seconds(d)
        if sec is not None and sec > 0:
            durations.append(sec)

    if titles and artists and durations:
        return TrackInfo(album_title=None, title=titles[0], artist=artists[0], duration_seconds=durations[0])
    return None


def _extract_album_title_from_soup(soup: BeautifulSoup) -> Optional[str]:
    # Пользовательский селектор: заголовок блока PageHeader
    album_span = soup.select_one("div.PageHeaderTitle_heading__UADXi span.PageHeaderTitle_title__caKyB")
    if album_span:
        text = album_span.get_text(strip=True)
        if text:
            return text

    # Fallback: первый заголовок с классом PageHeaderTitle_title__
    album_candidates = soup.select("span.PageHeaderTitle_title__caKyB")
    for candidate in album_candidates:
        text = candidate.get_text(strip=True)
        if text:
            return text
    return None


def _extract_from_page_header(soup: BeautifulSoup) -> Optional[TrackInfo]:
    logger = logging.getLogger(__name__)
    title: Optional[str] = None
    artist: Optional[str] = None
    duration_seconds: Optional[int] = None

    # Сначала пробуем явный заголовок трека из модального окна
    title_selectors = [
        "span.TrackModalTitle_trackTitle__",
        "[class*='TrackModalTitle_trackTitle__']",
    ]
    for selector in title_selectors:
        title_span = soup.select_one(selector)
        if title_span:
            title = title_span.get_text(strip=True)
            logger.debug("Заголовок трека найден по селектору: %s", selector)
            break

    # Для PageHeader берём последний заголовок: часто первый — альбом, второй — трек
    if not title:
        header_titles = [
            node.get_text(strip=True)
            for node in soup.select("span.PageHeaderTitle_title__caKyB")
            if node.get_text(strip=True)
        ]
        if header_titles:
            title = header_titles[-1]
            logger.debug("Заголовок трека найден в PageHeaderTitle_title__ (последний элемент)")

    artist_selectors = [
        "a.PageHeaderAlbumMeta_artistLink__eTSrZ span.PageHeaderAlbumMeta_artistLabel__2WZSM",
        "a.TrackModalTitle_link__kzVsl span.TrackModalTitle_artistCaption__Sj1CR",
        "[class*='TrackModalTitle_link__'] [class*='TrackModalTitle_artistCaption__']",
    ]
    for selector in artist_selectors:
        artist_span = soup.select_one(selector)
        if artist_span:
            artist = artist_span.get_text(strip=True)
            logger.debug("Артист найден по селектору: %s", selector)
            break

    if not artist:
        artist_link_selectors = [
            "a.PageHeaderAlbumMeta_artistLink__eTSrZ",
            "a.TrackModalTitle_link__kzVsl",
            "[class*='PageHeaderAlbumMeta_artistLink__']",
            "[class*='TrackModalTitle_link__']",
        ]
        for selector in artist_link_selectors:
            artist_link = soup.select_one(selector)
            if not artist_link:
                continue
            aria_label = (artist_link.get("aria-label") or "").strip()
            if not aria_label:
                continue
            match = re.search(r"^\s*Артист\s+(.+?)\s*$", aria_label, flags=re.IGNORECASE)
            if match:
                candidate = match.group(1).strip(" .,:;!?\t\r\n")
                if candidate:
                    artist = candidate
                    logger.debug("Артист извлечен из aria-label по селектору: %s", selector)
                    break

    duration_wrap = soup.select_one("span.CommonControlsBar_duration__un38A, [class*='CommonControlsBar_duration__']")
    if duration_wrap:
        aria_label = duration_wrap.get("aria-label", "")
        duration_seconds = _parse_duration_label_ru(aria_label)
        if duration_seconds is not None:
            logger.debug("Длительность извлечена из aria-label")
        if duration_seconds is None:
            inner = duration_wrap.select_one("span[aria-hidden='true']")
            if inner:
                duration_seconds = _parse_duration_mmss(inner.get_text(strip=True))
                if duration_seconds is not None:
                    logger.debug("Длительность извлечена из вложенного span aria-hidden")
            if duration_seconds is None:
                duration_seconds = _parse_duration_mmss(duration_wrap.get_text(strip=True))
                if duration_seconds is not None:
                    logger.debug("Длительность извлечена из текста блока длительности")

    if title and artist and duration_seconds is not None:
        return TrackInfo(
            album_title=_extract_album_title_from_soup(soup),
            title=title,
            artist=artist,
            duration_seconds=duration_seconds,
        )
    logger.debug(
        "Из header/modal не удалось извлечь все поля: title=%s artist=%s duration=%s",
        bool(title),
        bool(artist),
        duration_seconds is not None,
    )
    return None


def _extract_cover_url(soup: BeautifulSoup) -> Optional[str]:
    logger = logging.getLogger(__name__)

    # 1) Наиболее стабильный источник для превью
    og_image = soup.find("meta", attrs={"property": "og:image"})
    if og_image and og_image.get("content"):
        url = str(og_image.get("content")).strip()
        if url:
            logger.debug("Обложка найдена в og:image")
            return url

    # 2) Явный селектор из header страницы
    img = soup.select_one("img.PageHeaderCover_coverImage__i0wBv, img[class*='PageHeaderCover_coverImage__']")
    if img:
        src = (img.get("src") or "").strip()
        if src:
            logger.debug("Обложка найдена в img src")
            return src

        # Если src пустой, берём первый URL из srcset
        srcset = (img.get("srcset") or "").strip()
        if srcset:
            first_item = srcset.split(",")[0].strip()
            if first_item:
                url = first_item.split(" ")[0].strip()
                if url:
                    logger.debug("Обложка найдена в img srcset")
                    return url

    return None


def parse_track_info(html: str) -> TrackInfo:
    logger = logging.getLogger(__name__)
    soup = BeautifulSoup(html, "lxml")
    cover_url = _extract_cover_url(soup)
    album_title = _extract_album_title_from_soup(soup)

    logger.debug("Попытка извлечения из application/ld+json")
    ld_json_info = _extract_from_ld_json(soup)
    if ld_json_info:
        if not ld_json_info.album_title:
            ld_json_info.album_title = album_title
        ld_json_info.image_url = cover_url
        logger.debug("Успех: application/ld+json")
        return ld_json_info

    logger.debug("Попытка извлечения из og/meta")
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    title: Optional[str] = None
    artist: Optional[str] = None
    if og_title_tag and og_title_tag.get("content"):
        title, artist = _clean_title_og(og_title_tag["content"])

    duration = None
    og_duration = soup.find("meta", attrs={"property": "music:duration"})
    if og_duration and og_duration.get("content"):
        duration = _duration_to_seconds(og_duration["content"])

    if title and artist and duration is not None:
        logger.debug("Успех: og/meta")
        return TrackInfo(
            album_title=album_title,
            title=title,
            artist=artist,
            duration_seconds=duration,
            image_url=cover_url,
        )

    logger.debug("Попытка извлечения из header/modal селекторов")
    page_header_info = _extract_from_page_header(soup)
    if page_header_info:
        if not page_header_info.album_title:
            page_header_info.album_title = album_title
        page_header_info.image_url = cover_url
        logger.debug("Успех: header/modal селекторы")
        return page_header_info

    logger.debug("Попытка извлечения из __NEXT_DATA__")
    next_data_match = NEXT_DATA_RE.search(html)
    if next_data_match:
        info = _extract_from_json_blob(next_data_match.group(1))
        if info:
            if not info.album_title:
                info.album_title = album_title
            info.image_url = cover_url
            logger.debug("Успех: __NEXT_DATA__")
            return info

    logger.debug("Попытка извлечения из window.__INITIAL_STATE__")
    window_match = WINDOW_STATE_RE.search(html)
    if window_match:
        info = _extract_from_json_blob(window_match.group(1))
        if info:
            if not info.album_title:
                info.album_title = album_title
            info.image_url = cover_url
            logger.debug("Успех: window.__INITIAL_STATE__")
            return info

    if "Яндекс Музыка недоступна в вашем регионе" in html:
        raise RegionBlockedError("Яндекс Музыка недоступна в вашем регионе")

    logger.debug("Не удалось извлечь трек ни одним из методов")
    raise TrackNotFoundError("Не удалось извлечь данные трека из страницы")


def format_track_info(track_info: TrackInfo) -> str:
    album_line = f"💿 Альбом: {track_info.album_title}\n" if track_info.album_title else ""
    return (
        album_line
        + 
        f"🎵 Название трека: {track_info.title}\n"
        f"👤 Артист: {track_info.artist}\n"
        f"⏱ Длительность: {track_info.duration_mmss} ({track_info.duration_seconds} сек)"
    )


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    await update.message.reply_text(
        "Пришли ссылку на трек Яндекс.Музыки.\n"
        "Пример: https://music.yandex.ru/album/38720854/track/144156104"
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ = context
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    logging.info("Incoming message: %s", text)
    await update.message.reply_text("Ожидайте, происходит извлечение информации...")

    try:
        track_ref = extract_track_ref(text)
        track_info: Optional[TrackInfo] = None
        use_fallback = _get_env_bool("USE_YANDEX_TOKEN_FALLBACK", False)
        env_token = (os.getenv("YANDEX_MUSIC_TOKEN") or "").strip()
        api_attempted = False

        # Быстрый путь: если есть токен и fallback включён, сначала пробуем API без Selenium.
        if use_fallback and env_token:
            track_info = fetch_track_info_via_api(track_ref, env_token)
            api_attempted = True

        if track_info is None:
            try:
                html = fetch_track_page(track_ref.url)
            except CaptchaRequiredError:
                logging.warning("Получена капча при загрузке страницы Яндекс.Музыки")
                if not use_fallback:
                    logging.warning("API fallback отключен (USE_YANDEX_TOKEN_FALLBACK=false)")
                    raise
                if not env_token:
                    raise YandexTokenNotConfiguredError("YANDEX_MUSIC_TOKEN не задан")
                if not api_attempted:
                    track_info = fetch_track_info_via_api(track_ref, env_token)
                if track_info is None:
                    logging.error("После капчи API не вернул данные трека (проверьте YANDEX_MUSIC_TOKEN)")
                    raise TrackNotFoundError("Не удалось получить трек через API после капчи")
            else:
                try:
                    track_info = parse_track_info(html)
                except TrackNotFoundError:
                    logging.warning("Парсинг HTML не извлёк данные трека, пробуем API fallback")
                    if use_fallback and env_token and not api_attempted:
                        track_info = fetch_track_info_via_api(track_ref, env_token)
                    if track_info is None:
                        raise TrackNotFoundError("Не удалось извлечь данные трека из страницы")

        if track_info is None:
            raise TrackNotFoundError("Не удалось извлечь данные трека")

        send_cover = _get_env_bool("SEND_COVER", True)
        if send_cover and track_info.image_url:
            try:
                await update.message.reply_photo(photo=track_info.image_url)
            except Exception:
                logging.exception("Не удалось отправить обложку трека")
        await update.message.reply_text(format_track_info(track_info))
    except UnsupportedLinkError as exc:
        await update.message.reply_text(f"Некорректная ссылка: {exc}")
    except RegionBlockedError:
        await update.message.reply_text("Трек недоступен в вашем регионе.")
    except CaptchaRequiredError:
        await update.message.reply_text(
            "Яндекс.Музыка запросила капчу для этой ссылки, поэтому парсинг HTML недоступен.\n"
            "Включите `USE_YANDEX_TOKEN_FALLBACK=true` и задайте `YANDEX_MUSIC_TOKEN` в файле `.env`."
        )
    except YandexTokenNotConfiguredError:
        await update.message.reply_text(
            "Нужен токен Яндекс.Музыки.\n"
            "Добавьте `YANDEX_MUSIC_TOKEN` в файл `.env` и перезапустите бота."
        )
    except TrackNotFoundError:
        await update.message.reply_text("Не удалось извлечь данные трека из этой ссылки.")
    except WebDriverException:
        logging.exception("WebDriver error while fetching track page")
        await update.message.reply_text("Сервис временно недоступен. Попробуйте позже.")
    except Exception:
        logging.exception("Unexpected error")
        await update.message.reply_text("Произошла непредвиденная ошибка. Попробуйте позже.")


def build_application() -> Application:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    return app


def _verify_env_yandex_token_at_startup() -> None:
    # При старте проверяем токен из .env, если включен API fallback.
    if not _get_env_bool("USE_YANDEX_TOKEN_FALLBACK", False):
        return
    token = (os.getenv("YANDEX_MUSIC_TOKEN") or "").strip()
    if not token:
        logging.warning("USE_YANDEX_TOKEN_FALLBACK=true, но YANDEX_MUSIC_TOKEN пустой")
        return
    if YandexMusicClient is None:
        logging.warning("Библиотека yandex-music недоступна, проверка токена пропущена")
        return
    try:
        YandexMusicClient(token).init()
        logging.info("YANDEX_MUSIC_TOKEN проверен при запуске: OK")
    except Exception:
        logging.exception("YANDEX_MUSIC_TOKEN в .env недействителен или API недоступен")


def main() -> None:
    load_dotenv()
    setup_logging()
    _verify_env_yandex_token_at_startup()
    logging.info("Starting Ya Music bot")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
