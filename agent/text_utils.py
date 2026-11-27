import re
from collections.abc import AsyncIterable, AsyncIterator

_URL_RE = re.compile(r"https?://\S+")
_MARKDOWN_RE = re.compile(r"\*\*|__|[*#`~]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "\uFE0F"
    "\u200D"
    "]+",
)
_LIST_BREAK_RE = re.compile(r"\n\s*(?:\d+[.)]|[-•])\s+")

_OPEN_TAG = "<think>"
_CLOSE_TAG = "</think>"


def clean_chunk(text: str) -> str:
    """Limpia sintaxis visual (Markdown, URLs, emojis, saltos de lista) sin perder espacios entre tokens."""
    cleaned = _URL_RE.sub("el enlace", text)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = _EMOJI_RE.sub("", cleaned)
    cleaned = _LIST_BREAK_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("\r\n", " ").replace("\n", " ")
    return cleaned


def _partial_tail(buf: str, tag: str) -> str:
    """Devuelve el sufijo más largo de `buf` que sea prefijo propio de `tag`."""
    max_len = min(len(buf), len(tag) - 1)
    for length in range(max_len, 0, -1):
        suffix = buf[-length:]
        if tag.startswith(suffix):
            return suffix
    return ""


async def speech_text_filter(chunks: AsyncIterable[str]) -> AsyncIterator[str]:
    """Filtra bloques <think>...</think> aunque lleguen partidos entre chunks y limpia texto para TTS."""
    in_think = False
    carry = ""

    async for chunk in chunks:
        if not chunk:
            continue
        buf = carry + chunk
        carry = ""

        while buf:
            if not in_think:
                idx = buf.find(_OPEN_TAG)
                if idx != -1:
                    if idx > 0:
                        cleaned = clean_chunk(buf[:idx])
                        if cleaned:
                            yield cleaned
                    buf = buf[idx + len(_OPEN_TAG) :]
                    in_think = True
                else:
                    tail = _partial_tail(buf, _OPEN_TAG)
                    head = buf[: len(buf) - len(tail)] if tail else buf
                    if head:
                        cleaned = clean_chunk(head)
                        if cleaned:
                            yield cleaned
                    carry = tail
                    buf = ""
            else:
                idx = buf.find(_CLOSE_TAG)
                if idx != -1:
                    buf = buf[idx + len(_CLOSE_TAG) :]
                    in_think = False
                else:
                    carry = _partial_tail(buf, _CLOSE_TAG)
                    buf = ""

    if carry and not in_think:
        cleaned = clean_chunk(carry)
        if cleaned:
            yield cleaned
