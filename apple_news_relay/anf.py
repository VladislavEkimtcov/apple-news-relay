"""Turn Apple News Format (ANF) documents into a small, clean block list.

ANF is a layout language: nested containers, styles, layouts, ad slots,
publisher end-cards. A reader only needs the story, so this module flattens
the component tree into blocks:

    heading    {level, text}
    paragraph  {text, links?}
    quote      {text}
    image      {url, width, height, caption?}
    gallery    {images: [image...]}
    video      {url?, poster?, caption?, aspect_ratio?}   url: HLS or MP4 as the publisher serves it
    embed      {provider, url}
    divider    {}

Advertising is always removed (banner/MREC/replica ad components and the
document's ad settings carry no story content). Publisher promotions
(newsletter sign-ups, "follow us", subscribe buttons, asides) are tagged and
removed unless the caller asks to keep them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional

from .newscache import asset_image_url

AD_ROLES = {"banner_advertisement", "medium_rectangle_advertisement", "replica_advertisement",
            "advertisement", "ad"}
SKIP_ROLES = {"title", "logo", "spacer", "author", "byline", "intro", "illustrator", "photographer"}
HEADING_ROLES = {"heading": 2, "heading1": 1, "heading2": 2, "heading3": 3,
                 "heading4": 4, "heading5": 5, "heading6": 6}
IMAGE_ROLES = {"photo", "image", "figure", "portrait", "logo_image"}
EMBED_ROLES = {"tweet": "twitter", "instagram": "instagram", "facebook_post": "facebook",
               "embedwebvideo": "video", "embedvideo": "video", "social_embed": "social",
               "tiktok": "tiktok", "htmltable": "table", "map": "map"}
PROMO_URL = re.compile(r"subscribe|newsletter|signup|sign-up|apps\.apple\.com|/donate|donate\.|"
                       r"apple\.news/[A-Za-z0-9_-]+\?subscribe", re.I)
PROMO_TEXT = re.compile(r"^\s*(follow us|subscribe|sign up|download (the|our)|get (the|our) .{0,30}app|"
                        r"read more:|related:|more from|support (our|independent)|donate)", re.I)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p", "li") and self.parts:
            self.parts.append("\n")


def plain_text(text: str, fmt: Optional[str]) -> str:
    if fmt == "html" or (fmt is None and "<" in text and ">" in text and re.search(r"</?[a-z]+[^>]*>", text)):
        parser = _Text()
        parser.feed(text)
        text = "".join(parser.parts)
    elif fmt == "markdown":
        text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
        text = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text)
    return text.strip()


@dataclass
class Document:
    title: Optional[str]
    subtitle: Optional[str]
    byline: Optional[str]
    canonical_url: Optional[str]
    language: Optional[str]
    blocks: List[dict] = field(default_factory=list)
    removed: Dict[str, int] = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        return sum(len(b.get("text", "")) for b in self.blocks if b["type"] in ("paragraph", "heading", "quote"))

    @property
    def word_count(self) -> int:
        return sum(len(b.get("text", "").split()) for b in self.blocks if b["type"] in ("paragraph", "heading", "quote"))


class _Converter:
    def __init__(self, doc: dict):
        self.doc = doc
        self.images: Dict[str, dict] = {}
        for res in (doc.get("resources") or {}).values():
            if isinstance(res, dict) and res.get("imageIdentifier"):
                self.images[res["imageIdentifier"]] = res
        self.blocks: List[dict] = []
        self.media_seen: set = set()
        self.removed: Dict[str, int] = {}
        self.byline: Optional[str] = None
        self.intro: Optional[str] = None

    def count(self, reason: str):
        self.removed[reason] = self.removed.get(reason, 0) + 1

    def image(self, comp: dict) -> Optional[dict]:
        ident = comp.get("imageIdentifier")
        url = comp.get("URL")
        res = self.images.get(ident) if ident else None
        width = height = None
        if res:
            url = res.get("URL") or url
            dims = res.get("dimensions") or {}
            width, height = dims.get("width"), dims.get("height")
        if not url:
            return None
        if url.startswith("asset://"):
            url = asset_image_url(url[len("asset://"):])
        elif not url.startswith("http"):
            return None
        block = {"type": "image", "url": url, "width": width, "height": height}
        caption = comp.get("caption")
        if isinstance(caption, dict):
            caption = caption.get("text")
        if not caption and isinstance(comp.get("captionComponent"), dict):
            caption = comp["captionComponent"].get("text")
        if caption:
            block["caption"] = plain_text(str(caption), None)
        return block

    def media(self, role: str, comp: dict) -> Optional[dict]:
        """A video or audio clip. Its stream is a document resource (``resourceIdentifier``) or the
        component's own ``URL``. Hidden copies (``hidden`` without a ``conditional`` that could show
        them) are dropped, and a clip already placed (layouts for phone and tablet) only counts once."""
        if comp.get("hidden") and not comp.get("conditional"):
            return None
        res = (self.doc.get("resources") or {}).get(comp.get("resourceIdentifier") or "") or {}
        url = res.get("URL") or comp.get("URL")
        url = url if isinstance(url, str) and url.startswith(("https://", "http://")) else None
        key = url or comp.get("stillImageIdentifier") or comp.get("identifier")
        if key in self.media_seen:
            return None
        self.media_seen.add(key)
        poster = self.image({"imageIdentifier": comp.get("stillImageIdentifier")}) if comp.get("stillImageIdentifier") else None
        block = {"type": role, "url": url, "poster": poster["url"] if poster else None}
        caption = comp.get("caption")
        if isinstance(caption, dict):
            caption = caption.get("text")
        if caption:
            block["caption"] = plain_text(str(caption), None)
        ratio = comp.get("aspectRatio")
        if isinstance(ratio, (int, float)) and 0.3 < ratio < 4:
            block["aspect_ratio"] = round(float(ratio), 4)
        elif poster and poster.get("width") and poster.get("height"):
            block["aspect_ratio"] = round(poster["width"] / poster["height"], 4)
        return block

    def walk(self, comps, in_aside: bool = False):
        for comp in comps or []:
            if isinstance(comp, dict):
                self.component(comp, in_aside)

    def component(self, comp: dict, in_aside: bool):
        role = (comp.get("role") or comp.get("type") or "").lower()
        if role in AD_ROLES or "advertisement" in role:
            self.count("advertisement")
            return
        if role == "byline" and comp.get("text"):
            self.byline = self.byline or plain_text(comp["text"], comp.get("format"))
        if role == "intro" and comp.get("text"):
            self.intro = self.intro or plain_text(comp["text"], comp.get("format"))
        if role in ("aside", "pullquote_container") or role == "aside":
            in_aside = True
        if "components" in comp:
            self.walk(comp["components"], in_aside or role == "aside")
            return
        if role in SKIP_ROLES:
            return
        block = self.block(role, comp)
        if block is None:
            return
        if in_aside:
            block["promo"] = True
        if block["type"] == "paragraph" and PROMO_TEXT.match(block["text"]) and len(block["text"]) < 160:
            block["promo"] = True
        self.blocks.append(block)

    def block(self, role: str, comp: dict) -> Optional[dict]:
        if role in HEADING_ROLES and comp.get("text"):
            return {"type": "heading", "level": HEADING_ROLES[role], "text": plain_text(comp["text"], comp.get("format"))}
        if role in ("body", "text") and comp.get("text"):
            text = plain_text(comp["text"], comp.get("format"))
            if not text:
                return None
            block: dict = {"type": "paragraph", "text": text}
            links = [a.get("URL") for a in comp.get("additions") or [] if isinstance(a, dict) and a.get("URL")]
            if links:
                block["links"] = links
            return block
        if role in ("quote", "pullquote") and comp.get("text"):
            return {"type": "quote", "text": plain_text(comp["text"], comp.get("format"))}
        if role == "caption" and comp.get("text"):
            text = plain_text(comp["text"], comp.get("format"))
            last = self.blocks[-1] if self.blocks else None
            if last and last["type"] == "image" and not last.get("caption"):
                last["caption"] = text
                return None
            if last and last.get("caption") == text:
                return None
            return {"type": "caption", "text": text}
        if role in IMAGE_ROLES:
            return self.image(comp)
        if role in ("gallery", "mosaic"):
            images = [i for i in (self.image(item) for item in comp.get("items") or []) if i]
            return {"type": "gallery", "images": images} if images else None
        if role in ("video", "audio"):
            return self.media(role, comp)
        if role in EMBED_ROLES:
            return {"type": "embed", "provider": EMBED_ROLES[role], "url": comp.get("URL")}
        if role == "divider":
            return {"type": "divider"}
        if role == "link_button":
            action = comp.get("action") or {}
            url = action.get("URL") or comp.get("URL")
            text = comp.get("text") or ""
            if not url:
                return None
            return {"type": "link", "text": plain_text(text, None), "url": url,
                    "promo": bool(PROMO_URL.search(url) or PROMO_TEXT.match(text or ""))}
        return None


def convert(doc: dict, keep_promotions: bool = False) -> Document:
    """Flatten an ANF document. Ads are always dropped; promotions too unless
    ``keep_promotions``."""
    conv = _Converter(doc)
    conv.walk(doc.get("components"))
    blocks = []
    for block in conv.blocks:
        if block.get("promo") and not keep_promotions:
            conv.count("promotion")
            continue
        if not keep_promotions:
            block.pop("promo", None)
        if blocks and block["type"] == "divider" and blocks[-1]["type"] == "divider":
            continue
        blocks.append(block)
    while blocks and blocks[-1]["type"] in ("divider", "caption"):
        blocks.pop()
    if doc.get("advertisement") or doc.get("autoplacement") or doc.get("advertisingSettings"):
        conv.count("ad_settings")
    meta = doc.get("metadata") or {}
    authors = meta.get("authors")
    byline = conv.byline or (", ".join(authors) if isinstance(authors, list) and authors else None)
    return Document(
        title=doc.get("title"),
        subtitle=doc.get("subtitle") or conv.intro or meta.get("excerpt"),
        byline=byline,
        canonical_url=meta.get("canonicalURL"),
        language=doc.get("language"),
        blocks=blocks,
        removed=conv.removed,
    )


def clean_byline(text: Optional[str], publisher: Optional[str] = None) -> Optional[str]:
    """A byline as names only: ``"by Dennis Romero | NBC NEWS"`` -> ``"Dennis Romero"``.

    Drops a leading "By" and a trailing publisher name (after ``|``, ``·``, a comma or a dash),
    and a byline that is only a date ("Published on November 23, 2026") altogether."""
    if not text:
        return text
    t = re.sub(r"\s+", " ", text).strip()
    if re.match(r"^(published|updated|posted)\b", t, re.I):
        # A dateline in the byline slot ("Published on November 23, 2026"): names only if it has them.
        named = re.search(r"\bby\s+(.+)$", t, re.I)
        if not named:
            return None
        t = named.group(1)
    t = re.sub(r"^by\s+", "", t, flags=re.I)
    # "Sudiksha Kochi - 11/23/26 06:00 AM EST": the date goes, the reader shows its own.
    t = re.sub(r"\s*(?:[|·•,]|\s[-–—])\s*(?:\d{1,2}/\d{1,2}/\d{2,4}|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{1,2},? \d{4}).*$",
               "", t, flags=re.I)
    if publisher:
        t = re.sub(r"\s*(?:[|·•,]|\s[-–—])\s*" + re.escape(publisher.strip()) + r"\.?$", "", t, flags=re.I)
    return t.strip() or text


def preview(blocks: List[dict], max_chars: int = 700, max_paragraphs: int = 3) -> List[dict]:
    """The opening of a story: leading media plus the first few paragraphs,
    the way a publisher shows a lede before its paywall."""
    out: List[dict] = []
    chars = 0
    paragraphs = 0
    for block in blocks:
        if block["type"] == "paragraph":
            if paragraphs >= max_paragraphs or chars >= max_chars:
                break
            paragraphs += 1
            chars += len(block["text"])
        elif block["type"] in ("heading", "quote") and paragraphs:
            break
        out.append(block)
    return out
