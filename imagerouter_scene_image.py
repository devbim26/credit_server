"""
title: ImageRouter Scene Image Generator
author: Professor Patterns (based on Wavespeed AI Image Generator)
description: OpenWebUI Pipe for image generation via ImageRouter (OpenAI-compatible /v1/openai API).
version: 2.3.0
license: MIT

Provider:
- ImageRouter Images Generations API (/v1/openai/images/generations)
Model (default): google/nano-banana-pro

Notes:
- Returns images as base64 data URLs in markdown.
- Supports optional input images (image-to-image) via multipart image[] fields.
"""

import asyncio
import base64
import dataclasses
import io
import json
import logging
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from PIL import Image
from pydantic import BaseModel, Field

# ==================== CUSTOM EXCEPTIONS ====================


class ImageRouterPipeError(Exception):
    """Base exception for ImageRouter Gemini Pipe errors."""


class ImageProcessingError(ImageRouterPipeError):
    """Exception for image processing errors."""


class APIError(ImageRouterPipeError):
    """Exception for API communication errors."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(
            f"API request failed with status {status_code}: {message[:2000]}"
        )


class GenerationError(ImageRouterPipeError):
    """Exception for image generation failures."""


class TimeoutError(ImageRouterPipeError):
    """Exception for timeout errors."""


class ValidationError(ImageRouterPipeError):
    """Exception for validation errors."""


# ==================== MODEL CONFIGURATION ====================

# Списки размеров — из GET /v3/models ImageRouter (зафиксировано 2026-08-17).
# "custom" у модели = произвольный размер (передаём вычисленный без снапа).
_NANO_BANANA_PRO_SIZES = [
    "1024x1024",
    "848x1264",
    "1264x848",
    "896x1200",
    "1200x896",
    "928x1152",
    "1152x928",
    "768x1376",
    "1376x768",
    "1584x672",
    "2048x2048",
    "1696x2528",
    "2528x1696",
    "1792x2400",
    "2400x1792",
    "1856x2304",
    "2304x1856",
    "1536x2752",
    "2752x1536",
    "3168x1344",
    "4096x4096",
    "3392x5056",
    "5056x3392",
    "3584x4800",
    "4800x3584",
    "3712x4608",
    "4608x3712",
    "3072x5504",
    "5504x3072",
    "6336x2688",
]
_NANO_BANANA_2_SIZES = [
    "512x512",
    "256x1024",
    "192x1536",
    "424x632",
    "632x424",
    "448x600",
    "600x448",
    "464x576",
    "576x464",
    "384x688",
    "688x384",
    "792x168",
    "1024x256",
    "1536x192",
    "1024x1024",
    "512x2048",
    "384x3072",
    "848x1264",
    "1264x848",
    "896x1200",
    "1200x896",
    "2048x512",
    "928x1152",
    "1152x928",
    "768x1376",
    "1376x768",
    "1584x672",
    "3072x384",
    "2048x2048",
    "1024x4096",
    "768x6144",
    "1696x2528",
    "2528x1696",
    "1792x2400",
    "2400x1792",
    "4096x1024",
    "1856x2304",
    "2304x1856",
    "1536x2752",
    "2752x1536",
    "3168x1344",
    "6144x768",
    "4096x4096",
    "2048x8192",
    "1536x12288",
    "3392x5056",
    "5056x3392",
    "3584x4800",
    "4800x3584",
    "8192x2048",
    "3712x4608",
    "4608x3712",
    "3072x5504",
    "5504x3072",
    "6336x2688",
    "12288x1536",
]
_NANO_BANANA_2_LITE_SIZES = [
    "1024x1024",
    "1264x848",
    "848x1264",
    "1200x896",
    "896x1200",
    "928x1152",
    "1152x928",
    "768x1376",
    "1376x768",
    "1584x672",
    "2048x512",
    "512x2048",
    "3072x384",
    "384x3072",
]
_GPT_IMAGE_2_SIZES = [
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2560x1440",
    "3840x2160",
]
_GPT_IMAGE_2_FREE_SIZES = ["auto", "1024x1024"]
_GPT_IMAGE_1_SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536"]
_SEEDREAM_45_SIZES = [
    "2048x2048",
    "2304x1728",
    "1728x2304",
    "2560x1440",
    "1440x2560",
    "2496x1664",
    "1664x2496",
    "3024x1296",
    "4096x4096",
    "4608x3456",
    "3456x4608",
    "5120x2880",
    "2880x5120",
    "4992x3328",
    "3328x4992",
    "6048x2592",
]
_SEEDREAM_50_LITE_SIZES = [
    "2048x2048",
    "2304x1728",
    "1728x2304",
    "2848x1600",
    "1600x2848",
    "2496x1664",
    "1664x2496",
    "3136x1344",
    "3072x3072",
    "3456x2592",
    "2592x3456",
    "4096x2304",
    "2304x4096",
    "3744x2496",
    "2496x3744",
    "4704x2016",
]
_GROK_QUALITY_SIZES = [
    "1024x1024",
    "896x1280",
    "1280x896",
    "768x1408",
    "1408x768",
    "864x1296",
    "1296x864",
    "576x1248",
    "1248x576",
    "576x1280",
    "1280x576",
    "704x1408",
    "1408x704",
    "2048x2048",
    "1712x2432",
    "2432x1712",
    "1504x2752",
    "2752x1504",
    "1664x2496",
    "2496x1664",
    "1376x2976",
    "2976x1376",
    "1360x3008",
    "3008x1360",
    "1440x2880",
    "2880x1440",
]
_AUTO_IMAGE_SIZES = ["auto", "1024x1024", "1536x1024", "1024x1536"]

MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
    "google/nano-banana-pro": {
        "api_name": "google/nano-banana-pro",
        "display_name": "Nano Banana Pro (Gemini 3 Pro Image)",
        "description": "Google Gemini image-generation (alias gemini-3-pro-image).",
        "sizes": _NANO_BANANA_PRO_SIZES,
    },
    "google/nano-banana-2": {
        "api_name": "google/nano-banana-2",
        "display_name": "Nano Banana 2 (Gemini 3.1 Flash Image)",
        "description": "Google Gemini 3.1 Flash image-generation (alias nano-banana-2).",
        "sizes": _NANO_BANANA_2_SIZES,
    },
    "google/nano-banana-2-lite": {
        "api_name": "google/nano-banana-2-lite",
        "display_name": "Nano Banana 2 Lite (Gemini 3.1 Flash Image)",
        "description": "Облегчённый/дешёвый Gemini 3.1 Flash image-generation.",
        "sizes": _NANO_BANANA_2_LITE_SIZES,
    },
    "openai/gpt-image-2": {
        "api_name": "openai/gpt-image-2",
        "display_name": "GPT Image 2 (OpenAI)",
        "description": "OpenAI latest image generation (alias gpt-image-latest); до 3840x2160.",
        "sizes": _GPT_IMAGE_2_SIZES,
    },
    "openai/gpt-image-2:free": {
        "api_name": "openai/gpt-image-2:free",
        "display_name": "GPT Image 2 Free (OpenAI)",
        "description": "Бесплатный gpt-image-2 для быстрых тестов (только 1024x1024).",
        "sizes": _GPT_IMAGE_2_FREE_SIZES,
    },
    "openai/gpt-image-1": {
        "api_name": "openai/gpt-image-1",
        "display_name": "GPT Image 1 (OpenAI)",
        "description": "OpenAI image generation/editing.",
        "sizes": _GPT_IMAGE_1_SIZES,
    },
    "bytedance/seedream-4.5": {
        "api_name": "bytedance/seedream-4.5",
        "display_name": "Seedream 4.5 (ByteDance)",
        "description": "ByteDance Seedream image-generation.",
        "sizes": _SEEDREAM_45_SIZES,
    },
    "bytedance/seedream-5.0-lite": {
        "api_name": "bytedance/seedream-5.0-lite",
        "display_name": "Seedream 5.0 Lite (ByteDance)",
        "description": "Новейший облегчённый Seedream.",
        "sizes": _SEEDREAM_50_LITE_SIZES,
    },
    "xai/grok-imagine-image-quality": {
        "api_name": "xAI/grok-imagine-image-quality",
        "display_name": "Grok Imagine Image Quality (xAI)",
        "description": "xAI Grok Imagine image-generation.",
        "sizes": _GROK_QUALITY_SIZES,
    },
    "imagerouter/auto-image": {
        "api_name": "imagerouter/auto-image",
        "display_name": "Auto Image (ImageRouter)",
        "description": "Автороутинг: ImageRouter сам подбирает модель под запрос.",
        "sizes": _AUTO_IMAGE_SIZES,
    },
}

# Slug'и (api_name) всех моделей — единый источник enum для Admin и User Valves.
# Порядок зафиксирован для стабильного UI-списка (не sorted — чтобыgemini шли первыми).
IMAGE_MODEL_ENUM: List[str] = [cfg["api_name"] for cfg in MODEL_CONFIGS.values()]

MODEL_IDS: List[str] = list(MODEL_CONFIGS.keys())
MODEL_API_NAMES: Set[str] = {cfg["api_name"] for cfg in MODEL_CONFIGS.values()}

# Версия pipe: видна в имени модели в списке OpenWebUI и в строке деталей
# под каждым сгенерированным изображением (⚙️ ... | v2.1.0). Поднимайте при
# каждом изменении поведения — по ней легко проверить, какой код задеплоен.
PIPE_VERSION = "2.3.0"

# Vision-language (VL) models (text output) used for:
# - уточняющие вопросы по загруженной картинке
# - помощь в создании промптов
VL_MODEL_CONFIGS: Dict[str, Dict[str, Any]] = {
    "google/gemini-3.5-flash-lite": {
        "api_name": "google/gemini-3.5-flash-lite",
        "display_name": "Gemini 3.5 Flash Lite (VL)",
        "description": "VL model for image Q&A and prompt assistance via ImageRouter",
    }
}

# Дефолтная VL-модель. ВАЖНО: должна быть vision-capable (принимает image_url).
# Раньше дефолтом был openai/gpt-5.2-chat, но это ТЕКСТОВАЯ (не vision) модель —
# upstream OpenAI резектит image_url с 400 BadRequestForDependentService.
# google/gemini-3.5-flash-lite — vision-capable (подтверждено /v3/models:
# input_modalities=image), дешёвая, подходит для анализа архитектурных фото
# (построение SCENE_BRIEF, чтение разметки).
DEFAULT_VL_MODEL = "google/gemini-3.5-flash-lite"

# ==================== CONSTANTS ====================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ImageRouterScenePipe")

# Дефолтные эндпоинты. Базовый URL настраивается админ-вентилем
# IMAGEROUTER_API_BASE_URL и подставляется в ImageRouterProvider.__init__.
IMAGEROUTER_DEFAULT_BASE_URL = "https://api.imagerouter.io/v1/openai"

# Дефолтные таймауты; read переопределяется вентилем READ_TIMEOUT_SECONDS.
HTTP_TIMEOUTS = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)

MAX_IMAGE_SIZE = 1024
MAX_DOWNLOAD_SIZE = 10 * 1024 * 1024  # 10MB
MAX_RECURSION_DEPTH = 10

KNOWN_VALUES = {
    "MODEL": set(MODEL_IDS) | MODEL_API_NAMES,
    # MODE validation now uses SCENE_MODES (defined below); this entry is kept
    # only as data for the legacy _omni_search_settings fallback (uncalled in
    # the canonical __user__["valves"] path) and is not used by pipe().
    "MODE": {"generate_image", "prompt_assist"},
    "ASPECT_RATIO": {
        "1:1",
        "3:2",
        "2:3",
        "3:4",
        "4:3",
        "4:5",
        "5:4",
        "9:16",
        "16:9",
        "21:9",
        # Расширенный набор ImageRouter (провайдер clamp-нет неподдерживаемое).
        "auto",
        "1:2",
        "2:1",
        "1:4",
        "4:1",
        "1:8",
        "8:1",
        "9:21",
    },
    # RESOLUTION задаётся вентилем UserValves.RESOLUTION (см. ниже).
    "CAMERA_ANGLE": {
        "auto",
        "none",
        "drone",
        "eye_level",
        "birds_eye",
        "worms_eye",
        "close_up",
        "wide_angle",
        "portrait",
        "landscape",
        "isometric",
    },
    "STYLE": {
        "none",
        "photorealistic",
        "sketch",
        "watercolor",
        "blueprint",
        "architectural_render",
        "line_art",
        "minimalist",
        "interior_design",
        "cyberpunk",
        "material_transfer_by_arrow",
        "moodboard_collage",
    },
    "OUTPUT_FORMAT": {"jpeg", "png", "jpg", "webp"},
    "RESOLUTION": {"1K", "2K", "4K"},
}

# ==================== SCENE CONSISTENCY MODE (spec: 2026-08-09) ====================

# Режимы генерации (UserValves.MODE). interior/facade/masterplan — tailored
# (тянут свой промпт-пакет); free — генерация без паспорта; prompt_assist — VL-помощник.
SCENE_MODES: Set[str] = {
    "interior",
    "facade",
    "masterplan",
    "free",
    "prompt_assist",
}

# Типы SCENE_BRIEF (маркер <!--SCENE_BRIEF:type-->).
# Внимание: "exterior" (не "facade") — маркер паспорта фасада.
BRIEF_TYPES: Set[str] = {"interior", "exterior", "masterplan"}

BRIEF_MARKER_PREFIX = "<!--SCENE_BRIEF:"
BRIEF_MARKER_RE = re.compile(
    r"<!--SCENE_BRIEF:(interior|exterior|masterplan)-->", re.IGNORECASE
)

# Лимиты (spec раздел 8).
MAX_SCENE_BRIEF_CHARS = 4000
MAX_ANNOTATION_MARKS = 12
MAX_VL_RETRY = 1
VL_CAPTURE_TIMEOUT = 90
VL_ANNOTATION_TIMEOUT = 60

# ==================== UI LABELS (RU) FOR USER VALVES ====================
#
# В OpenWebUI выпадающие списки для enum обычно показывают значения как есть.
# Чтобы пользователь видел русский текст, используем русские варианты в UI,
# а внутри пайпа нормализуем их в канонические значения.

STYLE_UI_ENUM_RU: List[str] = [
    "нет",
    "фотореализм",
    "эскиз",
    "акварель",
    "чертёж",
    "3d-рендер",
    "лайн-арт",
    "минимализм",
    "интерьер",
    "киберпанк",
    "перенос материалов (стрелки)",
    "мудборд-коллаж",
]

STYLE_UI_ALIASES: Dict[str, str] = {
    # none
    "none": "none",
    "нет": "none",
    # styles
    "photorealistic": "photorealistic",
    "фотореализм": "photorealistic",
    "sketch": "sketch",
    "эскиз": "sketch",
    "watercolor": "watercolor",
    "акварель": "watercolor",
    "blueprint": "blueprint",
    "чертёж": "blueprint",
    "чертеж": "blueprint",
    "architectural_render": "architectural_render",
    "3d-рендер": "architectural_render",
    "3d рендер": "architectural_render",
    "line_art": "line_art",
    "лайн-арт": "line_art",
    "лайн арт": "line_art",
    "minimalist": "minimalist",
    "минимализм": "minimalist",
    "interior_design": "interior_design",
    "интерьер": "interior_design",
    "cyberpunk": "cyberpunk",
    "киберпанк": "cyberpunk",
    "material_transfer_by_arrow": "material_transfer_by_arrow",
    "перенос материалов (стрелки)": "material_transfer_by_arrow",
    "moodboard_collage": "moodboard_collage",
    "мудборд-коллаж": "moodboard_collage",
    "мудборд коллаж": "moodboard_collage",
}

CAMERA_ANGLE_UI_ENUM_RU: List[str] = [
    "авто",
    "нет",
    "дрон",
    "уровень глаз",
    "вид сверху",
    "снизу вверх",
    "крупный план",
    "широкий угол",
    "портрет",
    "пейзаж",
    "изометрия",
]

CAMERA_ANGLE_UI_ALIASES: Dict[str, str] = {
    # base
    "auto": "auto",
    "авто": "auto",
    "none": "none",
    "нет": "none",
    # angles
    "drone": "drone",
    "дрон": "drone",
    "eye_level": "eye_level",
    "уровень глаз": "eye_level",
    "birds_eye": "birds_eye",
    "вид сверху": "birds_eye",
    "worms_eye": "worms_eye",
    "снизу вверх": "worms_eye",
    "close_up": "close_up",
    "крупный план": "close_up",
    "wide_angle": "wide_angle",
    "широкий угол": "wide_angle",
    "portrait": "portrait",
    "портрет": "portrait",
    "landscape": "landscape",
    "пейзаж": "landscape",
    "isometric": "isometric",
    "изометрия": "isometric",
}

# Allow RU + EN inputs when extracting valves.
KNOWN_VALUES["STYLE"] |= set(STYLE_UI_ALIASES.keys())
KNOWN_VALUES["CAMERA_ANGLE"] |= set(CAMERA_ANGLE_UI_ALIASES.keys())


def _normalize_style_value(v: str) -> str:
    key = (v or "").lower().strip()
    return STYLE_UI_ALIASES.get(key, key)


def _normalize_camera_angle_value(v: str) -> str:
    key = (v or "").lower().strip()
    return CAMERA_ANGLE_UI_ALIASES.get(key, key)


# ==================== STYLE PROMPTS (copied from wave.py) ====================

STYLE_PROMPTS: Dict[str, str] = {
    "photorealistic": (
        "Photorealistic 8K visualization indistinguishable from high-end architectural photography. "
        "Faithfully preserves every element from the input image — exact geometry, materials, "
        "window placements, lighting conditions, and context. Realistic textures (brick, concrete, "
        "glass, wood), physically based global illumination, accurate depth of field, natural or "
        "cinematic HDR lighting, subtle lens effects (vignetting, chromatic aberration only if present "
        "in source), no stylization, no enhancement beyond resolution and clarity. Studio-grade "
        "post-processing only to match real-world camera output."
    ),
    "sketch": (
        "Rough architectural hand-drawn sketch in pencil on white paper, concept-stage appearance. "
        "All structural and spatial elements from the input image — walls, openings, stairs, furniture, "
        "trees — must be retained in correct position and proportion, even if rendered with loose, "
        "gestural lines. No clean linework; visible sketch marks, light smudging, and construction lines "
        "allowed. No color, no digital perfection — authentic analog sketch aesthetic only. No invented "
        "details or missing components."
    ),
    "watercolor": (
        "Traditional watercolor painting style: soft, translucent washes, visible paper grain, wet-on-wet "
        "blending, delicate pastel or earth-tone palette. Every architectural or landscape element from "
        "the source image — building forms, windows, vegetation, paths — must remain present and recognizable, "
        "though softly rendered. No hard edges, no digital crispness. Pigment blooms and brushstroke texture "
        "encouraged, but composition and element count must match input exactly — no additions, no omissions, "
        "no reinterpretation of layout."
    ),
    "blueprint": (
        "Architectural elevation drawing in Russian GOST standard style, white background, clean black and gray "
        "technical lines only, orthographic projection, no perspective, no shadows, no color. Faithfully accurate "
        "representation of the real building from the input photo: exact number of floors, precise window arrangement, "
        "balconies, facade panels, cornices, and all visible architectural elements preserved without invention or "
        "omission. Include dimension lines, level marks, and labels in Cyrillic. Drafting-quality linework: thick lines "
        "for main contours, thin lines for details and dimensions, dashed lines for hidden edges if inferred. Minimalist, "
        "professional technical documentation aesthetic."
    ),
    "architectural_render": (
        "Professional architectural 3D rendering in the style of high-end V-Ray and Unreal Engine 5, photorealistic "
        "visualization with physically accurate global illumination, ray-traced reflections and shadows, realistic "
        "material textures (concrete, glass, wood, metal), natural daylight or cinematic HDR lighting, depth of field, "
        "subtle ambient occlusion, sharp architectural detailing, clean composition with balanced contrast, studio-quality "
        "post-processing — indistinguishable from real photography. All elements from the input image — massing, "
        "fenestration, landscaping, context — must be preserved exactly; no idealization, no added vegetation or furniture "
        "unless present in source."
    ),
    "line_art": (
        "Clean black-and-white technical line art, ink-drawing aesthetic, high contrast, sharp vector-like contours. "
        "Only outlines — no fills, no shading, no texture. Every visible feature from the source image (walls, windows, "
        "doors, structural lines, furniture silhouettes, trees as outlines) must be included with precise geometry and "
        "correct spatial relationships. No sketchiness, no artistic distortion — pure, minimal, reproducible linework "
        "suitable for vector conversion. No elements added or removed."
    ),
    "minimalist": (
        "Minimalist architectural visualization in Bauhaus-inspired style: clean geometry, uncluttered composition, "
        "monochrome or muted neutral palette, soft diffused lighting, emphasis on form and negative space. Despite "
        "simplification, all key elements from the input image — building volume, openings, major landscape features — "
        "must remain present and correctly positioned. No decorative additions; reduction is allowed only through "
        "abstraction of texture and detail, never by omitting structural or spatial components. Zen-like calm, but "
        "architecturally truthful."
    ),
    "interior_design": (
        "Professional interior design photography in Architectural Digest style: naturally lit, staged with authentic "
        "furniture and decor exactly as in the source image, warm ambient lighting, subtle depth of field. Every object, "
        "surface, and layout element — sofas, tables, lamps, rugs, wall finishes, window treatments — must be preserved "
        "without substitution, addition, or removal. No 'idealized' styling: if the source shows a simple room, do not "
        "add luxury items. Focus on realism, spatial clarity, and accurate material representation — not fantasy staging."
    ),
    "cyberpunk": (
        "Cyberpunk aesthetic applied to the original scene: neon-drenched urban environment, high-contrast lighting with "
        "electric blues, purples, and pinks, volumetric fog, rain-wet surfaces, holographic signage, retro-futuristic "
        "architecture. Despite stylization, all structural and spatial elements from the input image — buildings, windows, "
        "roads, furniture — must remain unchanged in position, count, and form. No added sci-fi props (e.g., drones, robots) "
        "unless present in source. Stylization limited to lighting, color grading, and surface effects — not geometry or layout."
    ),
    "material_transfer_by_arrow": (
        "Strict material substitution based on an annotated design collage. The input contains a primary image "
        "(architectural facade, floor plan, or interior photo) and surrounding reference photos of materials, textures, "
        "or fixtures, linked to specific zones by red arrows. The AI must treat the primary image as sacred: its geometry, "
        "structure, layout, proportions, perspective, and all non-targeted elements must remain 100% unchanged. Only the areas "
        "explicitly indicated by red arrows may be modified — and only by applying the exact material, color, texture, or finish "
        "shown in the corresponding reference image. No extrapolation, no enhancement, no cleanup beyond the arrow-marked zones. "
        "If an arrow points to a wall section and links to a brick photo, replace only that wall section with that exact brick — "
        "nothing else. The result must be a minimally altered version of the original, with edits so precise they appear as if "
        "done in a professional image editor. Do not reinterpret, do not restyle, do not invent."
    ),
    "moodboard_collage": (
        "Generate a professional interior or architectural moodboard collage. The composition must include: a central floor plan "
        "or perspective sketch, surrounded by high-quality reference images of materials, furniture, lighting, textures, and "
        "color swatches — all arranged harmoniously on a clean white or light-gray background. Each reference item must be relevant "
        "to the described design brief (e.g., 'Scandinavian living room', 'industrial kitchen', 'brutalist facade'). Include subtle "
        "labels (e.g., 'Oak Flooring', 'Concrete Wall', 'Pendant Light') in a minimalist sans-serif font. No red arrows or connectors — "
        "this is a presentation board, not an instruction. Style should resemble curated Pinterest boards, ArchDaily project pages, or "
        "professional design studio mockups: balanced, uncluttered, visually cohesive."
    ),
}

# ==================== CAMERA ANGLE PROMPTS (copied from wave.py) ====================

CAMERA_ANGLE_PROMPTS: Dict[str, str] = {
    "auto": (
        "Preserve the exact camera perspective, angle, and projection type of the original input image — do not change viewpoint, "
        "focal length, or spatial orientation. Apply only the requested visual style (e.g., blueprint, isometric, render) while keeping "
        "the original composition and framing completely intact. No reinterpretation of camera position; fidelity to source perspective is mandatory."
    ),
    "eye_level": (
        "Eye-level perspective (1.6–1.7m height), natural human viewpoint, neutral lens (35–50mm equivalent). Every visible object from "
        "the source image — windows, doors, furniture, fixtures, vegetation — must appear unchanged in position, shape, count, and alignment. "
        "No invented details, no removals, no stylization. Moderate depth of field, realistic proportions, interior-appropriate lighting."
    ),
    "worms_eye": (
        "Extreme worm's-eye view from ground level looking straight up. All vertical elements — columns, walls, windows, balconies, "
        "ceiling features — must match the source image exactly in number, alignment, and form. No extra floors, no missing features, "
        "no decorative additions. Wide-angle distortion allowed only as optical effect, not as content alteration."
    ),
    "close_up": (
        "Extreme close-up macro shot focused on a specific detail from the input image. The depicted texture, material, joint, or component "
        "must be identical to the original — no enhancement, cleaning, synthesis, or hallucination of missing parts. Surrounding context may be "
        "blurred, but the core subject must be unchanged and recognizably extracted from source."
    ),
    "wide_angle": (
        "Ultra-wide-angle lens (12–16mm full-frame equivalent), expansive field of view. All objects present in the original scene must remain "
        "visible and unaltered — same positions, counts, forms, and spatial relationships. Slight barrel distortion is acceptable as lens "
        "characteristic, but no content must be cropped, deleted, added, or restructured."
    ),
    "portrait": (
        "Portrait focal length (85mm), shallow depth of field. If the subject is derived from a source image (e.g., a person, object, or "
        "architectural detail), their appearance, pose, proportions, and key features must be preserved without artistic reinterpretation, "
        "beautification, or omission."
    ),
    "isometric": (
        "Faithful isometric 3D reconstruction from a top-down architectural plan, preserving every element exactly as in the input image: walls, "
        "doors, windows, stairs, furniture, trees, roads, buildings, landscaping features — no additions, no omissions, no geometric alterations. "
        "Clean vector-style rendering, low-poly aesthetic with soft shading, diorama-like presentation, consistent scale, muted or architectural "
        "color palette. Strict 30-degree isometric projection, no perspective distortion, all plan elements extruded into 3D space with clear "
        "visibility and proportional height, orthographic lighting, top-down isometric view as used in technical and urban illustrations."
    ),
    "drone": (
        "High-altitude aerial drone photograph, straight-down nadir view, minimal perspective distortion, clear overview of all existing site "
        "elements — buildings, roads, trees, paths, parking, infrastructure — must be preserved exactly as in the input image. No additions, "
        "no omissions, no repositioning. Evenly lit, midday sun, deep depth of field, no tilt or banking."
    ),
    "birds_eye": (
        "True top-down bird's-eye orthographic view, directly overhead (90° angle), zero perspective. All layout elements — building footprints, "
        "roads, sidewalks, greenery, plot boundaries — must be replicated with 1:1 fidelity to the input plan. No elements added, removed, "
        "or repositioned. Uniform scale across frame, no vanishing points, plan-accurate geometry only."
    ),
    "landscape": (
        "Wide landscape lens (16–24mm full-frame equivalent), deep depth of field, sharp focus from foreground to horizon. The entire scene — "
        "buildings, terrain, vegetation, water bodies, urban fabric — must reflect the source composition exactly, with no elements added, "
        "removed, or restructured. Natural lighting only (e.g., overcast or golden hour); no dramatization that obscures or alters original features."
    ),
}


# ==================== SCENE BRIEF TEMPLATES (spec раздел 4) ====================
#
# Шаблоны паспорта сцены по типам. VL заполняет их при захвате.
# ВАЖНО: ключ словаря BRIEF_TEMPLATES для фасада — "exterior" (как в маркере),
# хотя MODE пользователя — "facade".

INTERIOR_BRIEF_TEMPLATE = """<!--SCENE_BRIEF:interior-->
# СЦЕНА: Интерьер — {title}

## Пространство
Тип: {room_type}. Площадь ~{area} м². Высота потолка ~{ceiling} м.
Форма: {shape}. Стен: {walls}.

## План (вид сверху)
{plan_items}

## Отделка
- Стены: {wall_finish}.
- Пол: {floor_finish}.
- Потолок: {ceiling_finish}.

## Палитра (HEX)
- Осн.: {hex_main} | Акцент: {hex_accent} | Текстиль: {hex_textile}

## Освещение
{lighting}

## Камера исходника
Высота ~{cam_height} м. Фокус ~{cam_focal} мм.
Точка: {cam_position}.
"""

FACADE_BRIEF_TEMPLATE = """<!--SCENE_BRIEF:exterior-->
# СЦЕНА: Фасад — {title}

## Объём
Тип: {building_type}. Этажности: {floors}.
Высота ~{height} м. Габариты в плане ~{footprint} м.
Форма: {shape}.

## Композиция фасада ({facade_name})
- Осей: {axes}. Шаг ~{axes_step} м.
- Окна по этажам: {window_grid} ({axes} осей × {floors} ярусов), {window_size}.
- Вход: {entrance}.
- Балконы: {balconies}.

## Материалы по зонам фасада
- Цоколь: {plinth}.
- Стены: {walls_mat}.
- Обрамления окон: {trim}.
- Крыша: {roof}.

## Элементы
{elements}

## Палитра (HEX)
- Осн.: {hex_main} | Цоколь: {hex_plinth} | Крыша: {hex_roof}

## Контекст
{context}

## Камера исходника
Высота ~{cam_height} м. Фокус ~{cam_focal} мм.
Точка: {cam_position}.
"""

MASTERPLAN_BRIEF_TEMPLATE = """<!--SCENE_BRIEF:masterplan-->
# СЦЕНА: Генплан — {title}

## Участок
Площадь ~{area} га. Габариты ~{dimensions} м.
Ориентация: верх кадра — СЕВЕР.

## Пятна застройки
{footprints}

## Дороги и пути
{roads}

## Озеленение и покрытия
{greenery}

## Функциональные зоны
{zones}

## Палитра (HEX)
- Застройка: {hex_build} | Озеленение: {hex_green} | Покрытия: {hex_pave}

## Камера исходника
Вид: орто/дрон, угол ~90° (надир).
Высота съёмки ~{cam_height} м. Центр кадра — {cam_center}.
"""

BRIEF_TEMPLATES: Dict[str, str] = {
    "interior": INTERIOR_BRIEF_TEMPLATE,
    "exterior": FACADE_BRIEF_TEMPLATE,
    "masterplan": MASTERPLAN_BRIEF_TEMPLATE,
}


# ==================== SCENE SYSTEM PROMPTS (spec раздел 7) ====================

INTERIOR_SYSTEM_PROMPT = (
    "Архитектурный интерьер. Сохраняй ТОЧНУЮ геометрию комнаты: стены, "
    "проёмы (двери/окна), их взаимное расположение. Мебель — тот же "
    "инвентарь в тех же позициях (учитывая новый ракурс). Отделка стен/пола/"
    "потолка, палитра HEX и источник света — неизменны во всех кадрах серии. "
    "Окна не добавлять и не убирать. Окно, помеченное [ЗАМОРОЗИТЬ], "
    "присутствует ВСЕГДА."
)

FACADE_SYSTEM_PROMPT = (
    "Архитектурный фасад. Сохраняй этажность, число и шаг осей, оконную "
    "сетку по ярусам (M осей × N этажей — как в паспорте). Входы, балконы, "
    "карнизы, цоколь — на тех же местах. Материалы по зонам фасада "
    "(цоколь/стены/обрамления/крыша) и их HEX — неизменны. Не добавлять "
    "окна там, где их нет. Камера — с земли, если не задан дрон."
)

MASTERPLAN_SYSTEM_PROMPT = (
    "Генплан, вид сверху. Сохраняй границы участка, ориентацию (СТРЕЛКА "
    "СЕВЕРА — неприкосновенна), пятна застройки в тех же позициях, "
    "дорожную сеть и озеленение. Функциональные зоны не меняют границ. "
    "Обход = поворот карты/высота дрона, а не перестановка объектов. "
    "Масштаб объектов постоянен."
)

SCENE_TYPE_SYSTEM_PROMPTS: Dict[str, str] = {
    "interior": INTERIOR_SYSTEM_PROMPT,
    "facade": FACADE_SYSTEM_PROMPT,
    "masterplan": MASTERPLAN_SYSTEM_PROMPT,
}

# Дефолты для Admin Valves SCENE_CAPTURE_SYSTEM_PROMPT / SCENE_EDIT_SYSTEM_PROMPT.
SCENE_CAPTURE_SYSTEM_PROMPT_DEFAULT = (
    "Ты — архитектурный анализатор сцены. По загруженному изображению построй "
    "паспорт сцены (SCENE_BRIEF), СТРОГО следуя шаблону для данного типа "
    "(interior/exterior/masterplan). Заполни ВСЕ поля шаблона по тому, что "
    "видишь: геометрия, материалы, палитра (HEX), освещение, камера исходника. "
    "Если что-то скрыто за кадром — не выдумывай, оставь разумное значение и "
    "пометь «(не видно)». Если на изображении есть пронумерованные метки "
    "(1, 2, 3 …), прочитай их и добавь раздел «## Правила разметки (ПРИОРИТЕТ)» "
    "с переводом каждой метки в правило по глаголу-маркеру из пояснений "
    "пользователя. ВСЕГДА начинай паспорт строкой маркера "
    "'<!--SCENE_BRIEF:<type>-->' где <type> = interior/exterior/masterplan. "
    "Отвечай ТОЛЬКО паспортом, без лишнего текста."
)

SCENE_EDIT_SYSTEM_PROMPT_DEFAULT = (
    "Ты — чтец архитектурной разметки. На изображении есть пронумерованные "
    "метки (1, 2, 3 …). В тексте пользователя — пояснения вида "
    "'<номер> = <действие> <что>'. Глаголы-маркеры и их смысл: "
    "заморозить = LOCK (защитить, не менять ни в одном ракурсе); "
    "изменить = EDIT (перерисовать зону); удалить = REMOVE; "
    "добавить = ADD; камера = CAMERA (точка/направление ракурса); "
    "фокус = FOCUS. Если глагола нет — трактовать как EDIT. "
    "Для каждой метки верни: номер, действие (LOCK/EDIT/REMOVE/ADD/CAMERA/FOCUS), "
    "описание зоны/объекта, и (для EDIT/ADD) целевое значение. "
    "Если метка внутри замкнутого контура — это точная маска, граница = контур. "
    "Если точка-пин — определи разумную зону (объект/стена/окно) в окрестности. "
    "Метку без пояснения — пропусти. Верни СТРОГИЙ JSON-список объектов "
    '[{"n": 1, "verb": "LOCK", "zone": "...", "target": "..."}].'
)


# ==================== VIEW PRESETS (spec раздел 6.3) ====================
#
# Пресеты ракурса. VIEW — единое поле UserValves для всех MODE.
# "none" = не добавлять пресет (полагаемся на текст/разметку).

INTERIOR_VIEW_PROMPTS: Dict[str, str] = {
    "none": "",
    "turn_left_90": "Обход камеры на 90° влево от исходной точки обзора.",
    "turn_right_90": "Обход камеры на 90° вправо от исходной точки обзора.",
    "opposite_wall": "Вид на противоположную стену из исходной точки съёмки.",
    "zoom_out": "Отдаление камеры: панорама комнаты с более широким охватом.",
    "zoom_in": "Приближение камеры: более крупный план той же сцены.",
    "eye_level": "Уровень глаз человека (~1.6 м), нейтральный объектив 35–50 мм.",
    "birds_eye": "Вид сверху под углом на ту же комнату (диорамный ракурс).",
}

FACADE_VIEW_PROMPTS: Dict[str, str] = {
    "none": "",
    "front": "Строго фронтальный ортогональный вид главного фасада.",
    "side_left": "Вид с левого торца здания под углом ~45°.",
    "side_right": "Вид с правого торца здания под углом ~45°.",
    "rear": "Вид заднего фасада с той же высоты съёмки.",
    "from_above": "Вид сверху под углом (дрон, ~30° от надира) на тот же объём.",
    "street_wide": "Широкоугольный вид с улицы, охватывающий фасад и контекст.",
}

MASTERPLAN_VIEW_PROMPTS: Dict[str, str] = {
    "none": "",
    "rotate_left": "Поворот вида генплана против часовой на ~45°.",
    "rotate_right": "Поворот вида генплана по часовой на ~45°.",
    "higher_drone": "Поднять камеру дрона: охват шире, масштаб мельче.",
    "lower_drone": "Опустить камеру дрона: охват уже, масштаб крупнее.",
    "nadir": "Строго надирный (90°) ортогональный вид участка.",
    "oblique_45": "Наклонный вид дрона под ~45° к надиру.",
}

# free — переиспользует существующие CAMERA_ANGLE_PROMPTS + auto/none.
FREE_VIEW_PROMPTS: Dict[str, str] = {
    "none": "",
    "auto": CAMERA_ANGLE_PROMPTS.get("auto", ""),
    "drone": CAMERA_ANGLE_PROMPTS.get("drone", ""),
    "eye_level": CAMERA_ANGLE_PROMPTS.get("eye_level", ""),
    "birds_eye": CAMERA_ANGLE_PROMPTS.get("birds_eye", ""),
    "worms_eye": CAMERA_ANGLE_PROMPTS.get("worms_eye", ""),
    "close_up": CAMERA_ANGLE_PROMPTS.get("close_up", ""),
    "wide_angle": CAMERA_ANGLE_PROMPTS.get("wide_angle", ""),
    "portrait": CAMERA_ANGLE_PROMPTS.get("portrait", ""),
    "landscape": CAMERA_ANGLE_PROMPTS.get("landscape", ""),
    "isometric": CAMERA_ANGLE_PROMPTS.get("isometric", ""),
}

VIEW_PROMPTS_BY_MODE: Dict[str, Dict[str, str]] = {
    "interior": INTERIOR_VIEW_PROMPTS,
    "facade": FACADE_VIEW_PROMPTS,
    "masterplan": MASTERPLAN_VIEW_PROMPTS,
    "free": FREE_VIEW_PROMPTS,
}

VIEW_VALUES_BY_MODE: Dict[str, Set[str]] = {
    mode: set(prompts.keys()) for mode, prompts in VIEW_PROMPTS_BY_MODE.items()
}

# Соответствие MODE -> тип маркера паспорта (exterior для facade!).
MODE_TO_BRIEF_TYPE: Dict[str, str] = {
    "interior": "interior",
    "facade": "exterior",
    "masterplan": "masterplan",
}


# ==================== UTILITIES ====================


class ImageProcessor:
    """Handles input image downloading, resizing and base64 encoding as a data URL."""

    def __init__(self, event_emitter=None):
        self.emitter = event_emitter

    async def _emit(self, msg: str):
        if self.emitter:
            await self.emitter(
                {"type": "status", "data": {"description": msg, "done": False}}
            )

    async def process(self, url: str) -> str:
        """Accepts http(s) URL or data URI, returns data URL."""
        try:
            if not url:
                raise ValidationError("Empty image URL")

            if url.startswith("data:image"):
                return url

            await self._emit("📥 Downloading input image...")

            if not url.startswith(("http://", "https://")):
                raise ValidationError(f"Invalid image URL: {url}")

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUTS) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()

                content_length = resp.headers.get("content-length")
                if content_length and int(content_length) > MAX_DOWNLOAD_SIZE:
                    raise ValidationError(
                        f"Image too large: {int(content_length)} bytes exceeds {MAX_DOWNLOAD_SIZE} bytes limit"
                    )

                data = resp.content
                if len(data) > MAX_DOWNLOAD_SIZE:
                    raise ValidationError(
                        f"Image too large: {len(data)} bytes exceeds {MAX_DOWNLOAD_SIZE} bytes limit"
                    )

            img = Image.open(io.BytesIO(data))
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            if max(img.size) > MAX_IMAGE_SIZE:
                img.thumbnail((MAX_IMAGE_SIZE, MAX_IMAGE_SIZE))

            # Encode as JPEG data URL to keep payload small and widely compatible.
            out = io.BytesIO()
            img_rgb = img.convert("RGB")
            img_rgb.save(out, format="JPEG", quality=85, optimize=True)
            b64 = base64.b64encode(out.getvalue()).decode("utf-8")
            return f"data:image/jpeg;base64,{b64}"

        except (ValidationError,):
            raise
        except Exception as e:
            logger.error(f"Image processing error: {e}")
            raise ImageProcessingError(f"Failed to process image: {str(e)}")


def _safe_join_sentences(parts: List[str]) -> str:
    cleaned = []
    for p in parts:
        if p and p.strip():
            cleaned.append(p.strip().rstrip("."))
    text = ". ".join(cleaned).strip()
    if text and not text.endswith("."):
        text += "."
    return text


# ----- Annotation parsing (spec раздел 5) -----


@dataclasses.dataclass
class Annotation:
    """Одна пронумерованная метка разметки (номера + текст)."""

    n: int
    verb: str  # LOCK | EDIT | REMOVE | ADD | CAMERA | FOCUS
    zone: str  # что/зона
    target: Optional[str] = None  # целевое значение (для EDIT/ADD)


# Глаголы-маркеры (RU + emoji) -> канонический verb.
VERB_ALIASES: Dict[str, str] = {
    "заморозить": "LOCK",
    "🔒": "LOCK",
    "lock": "LOCK",
    "защитить": "LOCK",
    "изменить": "EDIT",
    "✏️": "EDIT",
    "edit": "EDIT",
    "поменять": "EDIT",
    "покрасить": "EDIT",
    "заменить": "EDIT",
    "удалить": "REMOVE",
    "✖️": "REMOVE",
    "remove": "REMOVE",
    "убрать": "REMOVE",
    "добавить": "ADD",
    "➕": "ADD",
    "add": "ADD",
    "вставить": "ADD",
    "камера": "CAMERA",
    "🎥": "CAMERA",
    "camera": "CAMERA",
    "ракурс": "CAMERA",
    "фокус": "FOCUS",
    "🎯": "FOCUS",
    "focus": "FOCUS",
}

# Явные команды захвата сцены. ВАЖНО: голое слово "scene" НЕ является
# триггером — оно встречается в обычных английских промптах ("interior
# scene, wide angle") и ложно запускало захват при выключенном тумблере
# SCENE_AUTO_CAPTURE. Русские формы допускают склонения ("захвати сцену",
# "захвата сцены", "новую сцену").
_CAPTURE_TRIGGER_RE = re.compile(
    r"захват\w*\s+сцен\w+"  # захвати сцену / захват сцены / захвата сцены
    r"|нов\w+\s+сцен\w+"  # новая сцена / новую сцену
    r"|capture\s+(?:the\s+|a\s+|this\s+)?scene"  # capture (the) scene
    r"|scene\s+capture",  # scene capture
    re.IGNORECASE,
)


# Шаблон строки: "<номер> [emoji/markers] = <текст>"  или "<номер>.)"
# Номер может быть с точкой/скобкой: "1", "1.", "1)", "1 -".
_ANNOT_LINE_RE = re.compile(
    r"^\s*(?P<n>\d+)[\s)\.\-=:]*"  # номер + разделители
    r"(?P<rest>.+)$",  # остальное
    re.MULTILINE,  # ^…$ построчно → корректно для многострочного ввода
)


def _split_verb_and_rest(rest: str) -> Tuple[str, Optional[str], str]:
    """Вернуть (verb, target|None, zone_text). По первому найденному глаголу."""
    lower = rest.lower()
    for alias, verb in VERB_ALIASES.items():
        idx = lower.find(alias)
        if idx >= 0:
            after = rest[idx + len(alias) :].lstrip(" :=-→").strip()
            zone_text = rest[:idx].strip()
            target = None
            # EDIT/ADD могут содержать "старое -> новое" или ": новое".
            # Порядок сепараторов: ":" проверяется ПЕРВЫМ, т.к. в записи
            # "пол: паркет → плитка" зона (пол) отделена двоеточием, а стрелка
            # разделяет старое/новое значение внутри цели. Иначе зона ошибочно
            # поглотила бы "пол: паркет".
            for sep in (":", "→", "->", "—"):
                if sep in after:
                    parts = after.split(sep, 1)
                    zone_text = (
                        (zone_text + " " + parts[0]).strip()
                        if zone_text
                        else parts[0].strip()
                    )
                    target = parts[1].strip() or None
                    break
            if target is None and after:
                target = after
            # Если zone_text пуст, а after не разделён — значит всё это зона.
            if not zone_text and target:
                zone_text = target
                target = None
            return verb, target, zone_text
    # Глагол не найден → EDIT по умолчанию, всё — zone.
    return "EDIT", None, rest.strip()


def parse_annotation_text(text: str) -> List[Annotation]:
    """Разбор пояснений разметки вида 'N = действие что [: цель]'.

    Строки без префикса-номера игнорируются.
    """
    anns: List[Annotation] = []
    if not text:
        return anns
    for line in text.splitlines():
        m = _ANNOT_LINE_RE.match(line)
        if not m:
            continue
        try:
            n = int(m.group("n"))
        except ValueError:
            continue
        rest = (m.group("rest") or "").strip().strip("=:").strip()
        if not rest:
            continue
        verb, target, zone = _split_verb_and_rest(rest)
        anns.append(Annotation(n=n, verb=verb, zone=zone or rest, target=target))
    return anns


def truncate_annotations(
    anns: List[Annotation], limit: int = MAX_ANNOTATION_MARKS
) -> Tuple[List[Annotation], Optional[str]]:
    """Обрезка по лимиту. Возвращает (список, warning|None)."""
    if len(anns) <= limit:
        return anns, None
    warning = (
        f"⚠️ Меток больше лимита ({limit}): обработаны первые {limit}, "
        f"остальные {len(anns) - limit} отсечены."
    )
    return anns[:limit], warning


# ----- SCENE_BRIEF search / validate (spec раздел 4.4, 8) -----


def _flatten_content(content: Any) -> str:
    """Привести content сообщения (str | list[part]) к строке."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
        return "\n".join(parts)
    return ""


def find_latest_scene_brief(messages: List[Dict[str, Any]]) -> Optional[str]:
    """Найти ПОСЛЕДНИЙ SCENE_BRIEF в истории сообщений (включая маркер).

    Идём с конца; возвращаем весь блок от маркера до конца content.
    """
    if not messages:
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") not in ("assistant", "user"):
            continue
        text = _flatten_content(msg.get("content", ""))
        m = BRIEF_MARKER_RE.search(text)
        if m:
            # Паспорт = от маркера до конца content (паспорт — финальный блок ответа).
            return text[m.start() :].strip()
    return None


def extract_brief_type(brief: str) -> Optional[str]:
    """Тип паспорта из маркера (interior/exterior/masterplan)."""
    if not brief:
        return None
    m = BRIEF_MARKER_RE.search(brief)
    return m.group(1).lower() if m else None


def validate_brief_length(brief: str) -> None:
    """Raise ValidationError, если паспорт длиннее MAX_SCENE_BRIEF_CHARS."""
    if brief and len(brief) > MAX_SCENE_BRIEF_CHARS:
        raise ValidationError(
            f"SCENE_BRIEF too long: {len(brief)} > {MAX_SCENE_BRIEF_CHARS}"
        )


def ensure_brief_marker(brief: str, brief_type: str) -> str:
    """Если в brief нет маркера — добавить. Иначе вернуть как есть."""
    if not brief:
        return brief
    if BRIEF_MARKER_RE.search(brief):
        return brief
    marker = f"{BRIEF_MARKER_PREFIX}{brief_type}-->\n"
    return marker + brief.lstrip()


# ----- Prompt assembly (spec раздел 7) -----

_LOCK_HEADER = "СТРОГИЕ ПРАВИЛА [ЗАМОРОЗИТЬ] (нарушение = брак):"
_EDIT_HEADER = "ТОЧЕЧНЫЕ ПРАВКИ [ИЗМЕНИТЬ/ДОБАВИТЬ/УДАЛИТЬ]:"

_VERB_LABEL_RU = {
    "LOCK": "ЗАМОРОЗИТЬ",
    "EDIT": "ИЗМЕНИТЬ",
    "REMOVE": "УДАЛИТЬ",
    "ADD": "ДОБАВИТЬ",
    "CAMERA": "КАМЕРА",
    "FOCUS": "ФОКУС",
}


def resolve_camera_command(
    annotations: List[Annotation], user_text: str, mode: str, view: str
) -> str:
    """Цепочка приоритета: annotation[CAMERA] > user_text > view preset > ''."""
    # 1. Разметка CAMERA
    for a in annotations or []:
        if a.verb == "CAMERA" and (a.zone or a.target):
            return a.zone or a.target or ""
    # 2. Текст пользователя (если есть и не пуст)
    if user_text and user_text.strip():
        return user_text.strip()
    # 3. Пресет VIEW
    prompts = VIEW_PROMPTS_BY_MODE.get(mode, {})
    return prompts.get(view, "")


def format_annotation_rules(
    annotations: List[Annotation],
) -> Tuple[str, str]:
    """Вернуть (lock_block, edit_block) — текст правил для промпта."""
    lock_lines: List[str] = []
    edit_lines: List[str] = []
    for a in annotations or []:
        label = _VERB_LABEL_RU.get(a.verb, a.verb)
        if a.verb == "LOCK":
            lock_lines.append(
                f"{a.n}. [{label}] {a.zone} — НЕ ИЗМЕНЯТЬ ни в одном ракурсе."
            )
        elif a.verb in ("EDIT", "ADD", "REMOVE"):
            tgt = f" → {a.target}" if a.target else ""
            edit_lines.append(f"{a.n}. [{label}] {a.zone}{tgt}")
        # CAMERA и FOCUS идут в camera command / не в блоки правил
    lock_block = ""
    if lock_lines:
        lock_block = _LOCK_HEADER + "\n" + "\n".join(lock_lines)
    edit_block = ""
    if edit_lines:
        edit_block = _EDIT_HEADER + "\n" + "\n".join(edit_lines)
    return lock_block, edit_block


def assemble_scene_prompt(
    mode: str,
    brief: str,
    annotations: List[Annotation],
    user_text: str,
    view: str,
    style: str,
) -> str:
    """Собрать финальный промпт генерации в порядке приоритета (spec раздел 7).

    Порядок (LOCK-правила — самые сильные, идут ДО брифа):
      1. SCENE_TYPE_SYSTEM_PROMPT (по mode)
      2. LOCK правила            ← выше брифа: железобетонные ограничения
      3. SCENE_BRIEF (дословно)
      4. EDIT/ADD/REMOVE правила
      5. Команда ракурса (resolve_camera_command)
      6. STYLE

    Примечание: порядок «lock раньше brief» намеренно отклоняется от исходного
    черновика brief (где brief шёл перед lock) — этого требует тест
    test_assemble_order_lock_before_brief_before_edit (TDD: тест авторитетен),
    и это соответствует spec раздел 7: [ЗАМОРОЗИТЬ] — самые сильные правила.
    """
    parts: List[str] = []

    sys_prompt = SCENE_TYPE_SYSTEM_PROMPTS.get(mode, "")
    if sys_prompt:
        parts.append(sys_prompt)

    lock_block, edit_block = format_annotation_rules(annotations)
    if lock_block:
        parts.append(lock_block)

    if brief:
        parts.append("ПАСПОРТ СЦЕНЫ (канон — соблюдать дословно):\n" + brief)

    if edit_block:
        parts.append(edit_block)

    camera = resolve_camera_command(annotations, user_text, mode, view)
    if camera:
        parts.append("ЗАДАЧА РАКУРСА:\n" + camera)

    if style and style != "none" and style in STYLE_PROMPTS:
        parts.append("СТИЛЬ: " + STYLE_PROMPTS[style])

    return "\n\n".join(parts).strip()


# ImageRouter принимает size строкой "WxH" (или "auto"). Вентиль RESOLUTION
# (1K/2K/4K) задаёт длинную сторону; короткая — из ASPECT_RATIO. Реальная
# поддержка зависит от модели — локальный снап выбирает ближайший допустимый.
RESOLUTION_VALUES: List[str] = ["1K", "2K", "4K"]
DEFAULT_RESOLUTION = "1K"  # обратная совместимость (раньше было захардкожено в 1K)

_RESOLUTION_LONG_SIDE: Dict[str, int] = {"1K": 1024, "2K": 2048, "4K": 4096}


def _round_to_multiple(value: float, multiple: int = 16) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


def _compute_target_size(
    aspect_ratio: str, resolution: str
) -> Optional[Tuple[int, int]]:
    """'16:9'+'2K' → (2048, 1152). 'auto'/нечитаемый ratio → None."""
    try:
        rw, rh = aspect_ratio.split(":")
        rw, rh = float(rw), float(rh)
        if rw <= 0 or rh <= 0:
            return None
    except (ValueError, AttributeError):
        return None
    long_side = _RESOLUTION_LONG_SIDE.get(resolution, 1024)
    short = _round_to_multiple(long_side * min(rw, rh) / max(rw, rh))
    return (long_side, short) if rw >= rh else (short, long_side)


def _snap_size(target: Tuple[int, int], sizes: List[str]) -> Optional[Tuple[int, int]]:
    """Ближайший поддерживаемый размер: сначала ориентация (квадрат
    универсален), затем min max(|Δw|/w, |Δh|/h), тай-брейк — площадь."""
    parsed: List[Tuple[int, int]] = []
    for s in sizes or []:
        s = (s or "").strip().lower()
        if s in ("", "auto", "custom"):
            continue
        try:
            cw, ch = s.split("x")
            parsed.append((int(cw), int(ch)))
        except ValueError:
            continue
    if not parsed:
        return None
    tw, th = target

    def orient(cw: int, ch: int) -> str:
        return "landscape" if cw > ch else ("portrait" if cw < ch else "square")

    pool = [c for c in parsed if orient(*c) == orient(tw, th) or c[0] == c[1]]
    pool = pool or parsed

    def score(c: Tuple[int, int]) -> Tuple[float, int]:
        rel = max(abs(c[0] - tw) / tw, abs(c[1] - th) / th)
        area_diff = abs(c[0] * c[1] - tw * th)
        return (rel, area_diff)

    return min(pool, key=score)


def _resolve_image_size(aspect_ratio: str, resolution: str, model_cfg: Dict) -> str:
    """Финальный `size` для ImageRouter: auto | вычисленный WxH | снап."""
    target = _compute_target_size(aspect_ratio, resolution)
    if target is None:
        return "auto"
    sizes = (model_cfg or {}).get("sizes")
    if not sizes or sizes == ["custom"]:
        return f"{target[0]}x{target[1]}"
    snapped = _snap_size(target, sizes)
    return f"{snapped[0]}x{snapped[1]}" if snapped else f"{target[0]}x{target[1]}"


def _short_error(e: Exception, limit: int = 240) -> str:
    s = str(e) if e is not None else ""
    s = " ".join(s.split())
    return (s[:limit] + "…") if len(s) > limit else s


def _extract_text_from_content(content: Any) -> str:
    """ImageRouter message.content can be a string or a list of parts.

    Returns a best-effort plain text string.
    """

    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if isinstance(p, dict) and (p.get("type") == "text"):
                t = (p.get("text") or "").strip()
                if t:
                    parts.append(t)
        return "\n".join(parts).strip()

    # Fallback: keep it readable.
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _get_user_email(__user__: Any) -> Optional[str]:
    """Best-effort email extraction from OpenWebUI `__user__` payload (fail-closed)."""

    if not isinstance(__user__, dict):
        return None

    candidates = [
        __user__.get("email"),
        (__user__.get("user") or {}).get("email"),
        (__user__.get("profile") or {}).get("email"),
    ]

    for c in candidates:
        if isinstance(c, str) and "@" in c:
            return c.strip().lower()

    return None


def _parse_data_url(data_url: str) -> Tuple[str, bytes]:
    """Return (mime, raw_bytes)."""
    if not data_url.startswith("data:"):
        raise ValidationError("Expected data URL")

    header, b64 = data_url.split(",", 1)
    # header: data:image/png;base64
    if ";base64" not in header:
        raise ValidationError("Data URL is not base64 encoded")
    mime = header[5:].split(";", 1)[0] or "application/octet-stream"
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        raise ValidationError(f"Invalid base64 in data URL: {e}")
    return mime, raw


def _sniff_image_mime(raw: bytes) -> str:
    """Магические байты: png/jpeg/webp/gif (дефолт png)."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def _make_data_url(fmt: str, raw: bytes) -> str:
    fmt_l = fmt.lower().strip()
    if fmt_l in ("jpg", "jpeg"):
        mime = "image/jpeg"
    elif fmt_l == "png":
        mime = "image/png"
    elif fmt_l == "webp":
        mime = "image/webp"
    else:
        mime = "application/octet-stream"
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _convert_data_url_format(data_url: str, target_format: str) -> str:
    """Convert base64 data URL image to jpeg/png/webp if needed."""
    target = (target_format or "jpeg").lower().strip()
    if target == "jpg":
        target = "jpeg"
    if target not in ("jpeg", "png", "webp"):
        raise ValidationError(f"Unsupported OUTPUT_FORMAT: {target_format}")

    mime, raw = _parse_data_url(data_url)
    target_mime = {
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }[target]
    if mime == target_mime:
        return data_url

    img = Image.open(io.BytesIO(raw))
    out = io.BytesIO()
    if target == "jpeg":
        img = img.convert("RGB")
        img.save(out, format="JPEG", quality=90, optimize=True)
        return _make_data_url("jpeg", out.getvalue())

    if target == "webp":
        # WebP поддерживает RGBA; для гарантии приводим к RGB (фон белый),
        # т.к. некоторые провайдеры отдают PNG с прозрачностью.
        if img.mode == "RGBA":
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[3])
            img = background
        elif img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        img.save(out, format="WEBP", quality=90, method=4)
        return _make_data_url("webp", out.getvalue())

    # png
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    img.save(out, format="PNG", optimize=True)
    return _make_data_url("png", out.getvalue())


# ==================== PROVIDER API ====================


class ImageRouterProvider:
    """Handles communication with ImageRouter Chat Completions API for Gemini image generation."""

    def __init__(
        self,
        api_key: str,
        timeout: httpx.Timeout,
        model_configs: Dict[str, Dict[str, Any]],
        emitter=None,
        base_url: str = "",
    ):
        self.api_key = api_key
        self.timeout = timeout
        self.emitter = emitter
        self.processor = ImageProcessor(emitter)
        self.model_configs = model_configs
        # Базовый URL от Admin Valves (IMAGEROUTER_API_BASE_URL); при пустом —
        # дефолтные константы. От базы строятся оба эндпоинта.
        base = (base_url or "").strip().rstrip("/") or IMAGEROUTER_DEFAULT_BASE_URL
        self.images_url = f"{base}/images/generations"
        self.chat_url = f"{base}/chat/completions"

    async def _emit(self, msg: str, done: bool = False):
        if self.emitter:
            await self.emitter(
                {"type": "status", "data": {"description": msg, "done": done}}
            )

    def _get_model_config(self, model_id: str) -> Dict[str, Any]:
        """Resolve model config. Свободный текст: если model_id не в MODEL_CONFIGS,
        строим конфиг из строки (api_name = model_id)."""
        if not model_id:
            model_id = "google/nano-banana-pro"
        else:
            model_id = model_id.strip()
        low = model_id.lower()
        if low in self.model_configs:
            return self.model_configs[low]
        for cfg in self.model_configs.values():
            if cfg.get("api_name", "").lower() == low:
                return cfg
        # Свободный текст: временный конфиг.
        return {"api_name": model_id, "display_name": model_id}

    def _prepare_prompt(
        self, base_prompt: str, admin_sys: str, style: str, angle: str
    ) -> str:
        parts: List[str] = []
        if admin_sys and admin_sys.strip():
            parts.append(admin_sys.strip())

        # NOTE: "none" means: do not add any decorator prompt for this valve.
        if style and style != "none" and style in STYLE_PROMPTS:
            parts.append(STYLE_PROMPTS[style])

        # NOTE: "auto" preserves camera; "none" means: do not add any camera-angle decorator.
        if angle and angle not in ("auto", "none") and angle in CAMERA_ANGLE_PROMPTS:
            parts.append(CAMERA_ANGLE_PROMPTS[angle])

        if base_prompt and base_prompt.strip():
            parts.append(base_prompt.strip())
        return _safe_join_sentences(parts)

    def _build_vl_messages(
        self, system_prompt: str, prompt: str, images: List[str]
    ) -> List[Dict[str, Any]]:
        """Build messages for VL models (system + user with optional images)."""

        messages: List[Dict[str, Any]] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})

        if not images:
            messages.append({"role": "user", "content": prompt})
            return messages

        content_parts: List[Dict[str, Any]] = []
        content_parts.append({"type": "text", "text": prompt})
        for data_url in images:
            content_parts.append({"type": "image_url", "image_url": {"url": data_url}})
        messages.append({"role": "user", "content": content_parts})
        return messages

    def _headers(self) -> Dict[str, str]:
        # Be explicit about JSON to reduce chances of getting a non-JSON 200 response
        # (e.g., edge proxies / mis-negotiated content types).
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _parse_json(self, resp: httpx.Response) -> Dict[str, Any]:
        """Parse ImageRouter response as JSON with multiple fallbacks.

        Почему это нужно:
        - иногда при `200 OK` приходит невалидный JSON (или JSON в неожиданной кодировке)
        - иногда приходит `text/event-stream` (SSE)
        - иногда `resp.text` оказывается пустым, хотя `resp.content` содержит байты

        Возвращает dict (или {"_raw": <...>} для не-dict JSON).
        Бинарные image/* ответы обрабатываются в ImageRouterProvider._generate_via_images().
        """

        raw = resp.content or b""
        ct = (resp.headers.get("content-type") or "").lower()

        def _wrap(obj: Any) -> Dict[str, Any]:
            return obj if isinstance(obj, dict) else {"_raw": obj}

        # 1) Быстрый путь через httpx
        try:
            return _wrap(resp.json())
        except Exception:
            pass

        # 2) SSE: lines like "data: {...}".
        # Иногда у ImageRouter случаются смешанные/ошибочные заголовки, поэтому проверяем и по content-type,
        # и по префиксу тела.
        text_for_sse = resp.text or ""
        if "text/event-stream" in ct or text_for_sse.lstrip().startswith("data:"):
            last_obj: Optional[Dict[str, Any]] = None
            for line in text_for_sse.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    obj = json.loads(payload)
                    if isinstance(obj, dict):
                        last_obj = obj
                except Exception:
                    continue
            if last_obj is not None:
                return last_obj

        # 3) Попытка распарсить JSON из разных текстовых представлений
        candidates: List[str] = []
        if text_for_sse.strip():
            candidates.append(text_for_sse)

        # Декодирование байтов: ImageRouter должен отдавать UTF-8, но на практике иногда встречаются BOM/UTF-16.
        if raw:
            encodings: List[str] = []
            if raw.startswith(b"\xef\xbb\xbf"):
                encodings.append("utf-8-sig")
            if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
                encodings.append("utf-16")

            # Heuristic: много нулевых байтов → вероятно UTF-16.
            sample = raw[:2000]
            null_ratio = sample.count(0) / max(1, len(sample))
            if null_ratio > 0.10 and "utf-16" not in encodings:
                encodings.append("utf-16")

            # Fallback encodings
            encodings.extend(
                ["utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "latin-1"]
            )

            for enc in encodings:
                try:
                    s = raw.decode(enc)
                except Exception:
                    continue
                if s and s.strip():
                    candidates.append(s)
                    break

        for s in candidates:
            # Убираем BOM/нулевые символы (часто появляются при неверной детекции кодировки).
            cleaned = s.replace("\ufeff", "").replace("\x00", "").strip()
            if not cleaned:
                continue
            try:
                return _wrap(json.loads(cleaned))
            except Exception:
                continue

        # 4) Диагностика: если текст не читается, отдадим hex-дамп первых байт.
        snippet = ""
        try:
            snippet = (raw[:2000].decode("utf-8", errors="replace") or "").strip()
        except Exception:
            snippet = ""

        if not snippet and raw:
            head = raw[:200]
            hex_head = " ".join(f"{b:02x}" for b in head)
            snippet = f"<non-textual body; first_bytes_hex={hex_head}>"

        content_len = len(raw)
        raise APIError(
            resp.status_code,
            f"Non-JSON response (content-type={ct or 'unknown'}, bytes={content_len}): {snippet[:2000]}",
        )

    async def generate(
        self,
        params: Dict[str, Any],
        admin_sys_prompt: str,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Generate images via ImageRouter.

        Единственный путь: POST /v1/openai/images/generations
        (JSON без референсов; multipart с image[] при image-to-image).

        Размер результата: вентили ASPECT_RATIO + RESOLUTION (1K/2K/4K)
        маппятся локально в size WxH со снапом к поддерживаемым размерам модели.
        """
        # --- подготовка параметров ---
        model_id = params.get("MODEL", "google/nano-banana-pro")
        model_cfg = self._get_model_config(model_id)

        aspect_ratio = (params.get("ASPECT_RATIO", "1:1") or "1:1").strip()
        if aspect_ratio not in KNOWN_VALUES["ASPECT_RATIO"]:
            raise ValidationError(f"Unsupported ASPECT_RATIO: {aspect_ratio}")

        style_raw = (
            (params.get("STYLE", "photorealistic") or "photorealistic").lower().strip()
        )
        if style_raw not in KNOWN_VALUES["STYLE"]:
            raise ValidationError(f"Unsupported STYLE: {style_raw}")
        style = _normalize_style_value(style_raw)

        angle_raw = (params.get("CAMERA_ANGLE", "auto") or "auto").lower().strip()
        if angle_raw not in KNOWN_VALUES["CAMERA_ANGLE"]:
            raise ValidationError(f"Unsupported CAMERA_ANGLE: {angle_raw}")
        angle = _normalize_camera_angle_value(angle_raw)

        output_format = (params.get("OUTPUT_FORMAT", "jpeg") or "jpeg").lower().strip()
        if output_format == "jpg":
            output_format = "jpeg"
        if output_format not in ("jpeg", "png", "webp"):
            raise ValidationError(f"Unsupported OUTPUT_FORMAT: {output_format}")

        resolution = (
            (params.get("RESOLUTION", DEFAULT_RESOLUTION) or DEFAULT_RESOLUTION)
            .upper()
            .strip()
        )
        if resolution not in KNOWN_VALUES["RESOLUTION"]:
            raise ValidationError(f"Unsupported RESOLUTION: {resolution}")

        image_urls = params.get("images", []) or []

        # Process input images into data URLs (optional).
        processed_images: List[str] = []
        if image_urls:
            await self._emit("🖼️ Processing input images...")
            processed_images = await asyncio.gather(
                *[self.processor.process(u) for u in image_urls]
            )

        full_prompt = self._prepare_prompt(
            base_prompt=params.get("prompt", ""),
            admin_sys=admin_sys_prompt,
            style=style,
            angle=angle,
        )

        size = _resolve_image_size(aspect_ratio, resolution, model_cfg)

        logger.info(
            f"Generating with model '{model_id}' ({model_cfg['api_name']}) -> "
            f"Ratio: {aspect_ratio} | Size: {size} | Style: {style} | Angle: {angle}"
        )

        await self._emit(
            f"🚀 ImageRouter: {model_cfg['display_name']} ({aspect_ratio}, {size})..."
        )

        return await self._generate_via_images(
            model_cfg=model_cfg,
            full_prompt=full_prompt,
            processed_images=processed_images,
            size=size,
            output_format=output_format,
            resolution=resolution,
        )

    # ==================== IMAGE API PATHS ====================

    @staticmethod
    def _is_response_format_error(text: str) -> bool:
        """400-я ошибка про неподдерживаемый response_format/b64_ephemeral."""
        low = (text or "").lower()
        return "response_format" in low or "b64_ephemeral" in low

    def _base_meta(
        self, used_api: str, resolution: str = DEFAULT_RESOLUTION
    ) -> Dict[str, Any]:
        return {
            "used_api": used_api,
            "used_resolution": resolution.lower(),
            "used_image_size": resolution.upper(),
            "read_timeout_seconds": float(
                getattr(self.timeout, "read", 120.0) or 120.0
            ),
        }

    async def _generate_via_images(
        self,
        model_cfg: Dict[str, Any],
        full_prompt: str,
        processed_images: List[str],
        size: str,
        output_format: str,
        resolution: str = DEFAULT_RESOLUTION,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """ImageRouter: POST /v1/openai/images/generations.

        JSON без референсов; multipart c image[] (декодированные data-URL) при
        image-to-image. response_format=b64_ephemeral (fallback b64_json при 400).
        Response: {created, data:[{b64_json|url}], latency, cost}.
        """
        meta = self._base_meta("images", resolution)

        fields: Dict[str, Any] = {
            "model": model_cfg["api_name"],
            "prompt": full_prompt,
            "size": size,
            "output_format": output_format,
            "response_format": "b64_ephemeral",
        }
        files: List[tuple] = []
        for i, data_url in enumerate(processed_images):
            mime, raw = _parse_data_url(data_url)
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(
                mime, "png"
            )
            files.append(("image[]", (f"image_{i}.{ext}", raw, mime)))

        logger.info(
            f"DEBUG: /images/generations request: model={fields['model']} "
            f"size={size} refs={len(files)} fmt={output_format}"
        )
        # Служебные данные запроса (для debug-блока в чате).
        meta["request_model"] = fields["model"]
        meta["request_size"] = size
        meta["request_refs"] = len(files)
        meta["request_url"] = self.images_url

        resp = await self._post_generation(fields, files)
        if resp.status_code == 400 and self._is_response_format_error(resp.text):
            logger.info(
                "response_format=b64_ephemeral rejected, retrying with b64_json"
            )
            fields = {**fields, "response_format": "b64_json"}
            resp = await self._post_generation(fields, files)

        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        # Бинарный image/* ответ — теоретически возможный edge; обрабатываем.
        ct = (resp.headers.get("content-type") or "").lower()
        if ct.startswith("image/") and resp.content:
            mime = ct.split(";", 1)[0].strip()
            b64 = base64.b64encode(resp.content).decode("utf-8")
            images_out = [f"data:{mime};base64,{b64}"]
        else:
            try:
                data = resp.json()
            except Exception:
                data = self._parse_json(resp)
            # Служебные данные ответа (для debug-блока в чате).
            meta["provider_cost_usd"] = data.get("cost")
            meta["latency"] = data.get("latency")
            items = data.get("data") or []
            if not items:
                raise GenerationError(
                    f"No images in /images/generations response: {str(data)[:500]}"
                )
            images_out: List[str] = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                b64 = (it.get("b64_json") or "").strip()
                if b64:
                    try:
                        mime = _sniff_image_mime(base64.b64decode(b64))
                    except Exception:
                        mime = f"image/{output_format}"
                    images_out.append(f"data:{mime};base64,{b64}")
                    continue
                url = (it.get("url") or "").strip()
                if url.startswith("data:"):
                    images_out.append(url)
                elif url:
                    images_out.append(await self._download_to_data_url(url))

        if not images_out:
            raise GenerationError(
                "No b64_json/url in /images/generations response data[]"
            )

        if output_format in ("jpeg", "png", "webp"):
            try:
                images_out = [
                    _convert_data_url_format(u, output_format) for u in images_out
                ]
            except Exception as e:
                logger.warning(f"Failed to convert output format: {e}")

        return images_out, meta

    async def _post_generation(
        self, fields: Dict[str, Any], files: List[tuple]
    ) -> httpx.Response:
        """POST /images/generations: JSON или multipart (httpx сам ставит boundary)."""
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                if files:
                    return await client.post(
                        self.images_url,
                        headers=headers,
                        data=fields,
                        files=files,
                    )
                return await client.post(
                    self.images_url,
                    headers={**headers, "Content-Type": "application/json"},
                    json=fields,
                )
            except httpx.TimeoutException:
                raise TimeoutError(
                    "ImageRouter /images/generations request timed out "
                    f"(read={float(getattr(self.timeout, 'read', 120.0) or 120.0)}s)"
                )
            except httpx.HTTPError as e:
                raise APIError(0, str(e))

    async def _download_to_data_url(self, url: str) -> str:
        """Скачать hosting-URL картинки и обернуть в data URL (лимит 10MB)."""
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True
        ) as client:
            try:
                resp = await client.get(url)
            except httpx.HTTPError as e:
                raise APIError(0, f"Failed to download image {url}: {e}")
        if resp.status_code >= 400:
            raise APIError(resp.status_code, f"Failed to download image: {url}")
        if len(resp.content) > MAX_DOWNLOAD_SIZE:
            raise ImageProcessingError(
                f"Image too large: {len(resp.content)} bytes exceeds {MAX_DOWNLOAD_SIZE} bytes limit"
            )
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/"):
            mime = _sniff_image_mime(resp.content)
        return f"data:{mime};base64,{base64.b64encode(resp.content).decode('utf-8')}"

    def _get_vl_model_config(self, vl_model: str) -> Dict[str, Any]:
        """Resolve VL model config by key or api_name."""

        if not vl_model:
            vl_model = DEFAULT_VL_MODEL
        vl_model = vl_model.lower().strip()

        if vl_model in VL_MODEL_CONFIGS:
            return VL_MODEL_CONFIGS[vl_model]

        for cfg in VL_MODEL_CONFIGS.values():
            if cfg.get("api_name") == vl_model:
                return cfg

        logger.warning(f"VL model '{vl_model}' not found, using free-text config")
        # Свободный текст: временный конфиг (как _get_model_config для image-моделей).
        return {"api_name": vl_model, "display_name": vl_model}

    async def assist_with_image(
        self,
        user_prompt: str,
        images: List[str],
        vl_model: str,
        system_prompt: str,
        max_tokens: int = 900,
    ) -> str:
        """Run a VL model to ask clarifying questions / help craft prompts from an image."""

        model_cfg = self._get_vl_model_config(vl_model)

        image_urls = images or []
        processed_images: List[str] = []
        if image_urls:
            await self._emit("🖼️ Processing input images for VL assist...")
            processed_images = await asyncio.gather(
                *[self.processor.process(u) for u in image_urls]
            )

        prompt_text = (user_prompt or "").strip()
        if not prompt_text:
            prompt_text = (
                "Посмотри на изображение и сделай следующее:\n"
                "1) Составь список уточняющих вопросов (5–12), которые нужно задать пользователю, чтобы точно сформировать промпт.\n"
                "2) Дай 3 варианта готового промпта для генерации/редизайна изображения (короткий / средний / подробный).\n"
                "3) Добавь блок 'Негативный промпт' (если уместно) и список параметров (aspect_ratio, style, camera_angle), если это помогает.\n"
                "Ответ дай на русском в Markdown."
            )

        messages = self._build_vl_messages(
            system_prompt=system_prompt, prompt=prompt_text, images=processed_images
        )

        payload: Dict[str, Any] = {
            "model": model_cfg["api_name"],
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
        }

        await self._emit(
            f"🧠 Sending VL request to ImageRouter: {model_cfg['display_name']}..."
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    self.chat_url, headers=self._headers(), json=payload
                )
            except httpx.TimeoutException:
                raise TimeoutError("ImageRouter VL request timed out")
            except httpx.HTTPError as e:
                raise APIError(0, str(e))

        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        data = self._parse_json(resp)

        choices = data.get("choices") or []
        if not choices:
            raise GenerationError("No choices in VL response")

        msg = (choices[0] or {}).get("message") or {}
        text = _extract_text_from_content(msg.get("content"))
        text = (text or "").strip()
        if not text:
            raise GenerationError("VL model returned empty content")

        return text

    async def assist_with_history(
        self,
        history_messages: List[Dict[str, Any]],
        vl_model: str,
        system_prompt: str,
        max_tokens: int = 900,
    ) -> str:
        """VL assistant that sees the full chat context.

        - Accepts full OpenWebUI-like message history (user+assistant).
        - Converts any image_url parts into data URLs (best-effort) so VL models can "see" them.
        - Works even if the *latest* message has no image (as long as an earlier one did).
        """

        model_cfg = self._get_vl_model_config(vl_model)

        out_messages: List[Dict[str, Any]] = []
        if system_prompt and system_prompt.strip():
            out_messages.append({"role": "system", "content": system_prompt.strip()})

        for m in history_messages or []:
            if not isinstance(m, dict):
                continue

            role = (m.get("role") or "user").strip().lower()
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"

            content = m.get("content", "")

            if isinstance(content, str):
                if content.strip():
                    out_messages.append({"role": role, "content": content})
                continue

            if isinstance(content, list):
                parts: List[Dict[str, Any]] = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = (part.get("type") or "").strip()
                    if ptype == "text":
                        t = (part.get("text") or "").strip()
                        if t:
                            parts.append({"type": "text", "text": t})
                    elif ptype == "image_url":
                        url = (((part.get("image_url") or {}).get("url")) or "").strip()
                        if not url:
                            continue
                        try:
                            data_url = await self.processor.process(url)
                            parts.append(
                                {"type": "image_url", "image_url": {"url": data_url}}
                            )
                        except Exception as e:
                            # Best-effort: if conversion fails, include original url so the model still sees something.
                            logger.warning(
                                f"VL image processing failed, passing original url. Error: {e}"
                            )
                            parts.append(
                                {"type": "image_url", "image_url": {"url": url}}
                            )

                if parts:
                    out_messages.append({"role": role, "content": parts})

        if not out_messages:
            raise ValidationError("Empty history_messages for VL assist")

        payload: Dict[str, Any] = {
            "model": model_cfg["api_name"],
            "messages": out_messages,
            "stream": False,
            "max_tokens": max_tokens,
        }

        await self._emit(
            f"🧠 Sending VL (history) request to ImageRouter: {model_cfg['display_name']}..."
        )

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                resp = await client.post(
                    self.chat_url, headers=self._headers(), json=payload
                )
            except httpx.TimeoutException:
                raise TimeoutError("ImageRouter VL request timed out")
            except httpx.HTTPError as e:
                raise APIError(0, str(e))

        if resp.status_code >= 400:
            raise APIError(resp.status_code, resp.text)

        data = self._parse_json(resp)

        choices = data.get("choices") or []
        if not choices:
            raise GenerationError("No choices in VL response")

        msg = (choices[0] or {}).get("message") or {}
        text = _extract_text_from_content(msg.get("content"))
        text = (text or "").strip()
        if not text:
            raise GenerationError("VL model returned empty content")

        return text

    # ----- Scene consistency mode (spec раздел 3, 5) -----

    async def _vl_call(
        self,
        system_prompt: str,
        user_prompt: str,
        images: List[str],
        vl_model: str,
        max_tokens: int = 1500,
        timeout_s: int = VL_CAPTURE_TIMEOUT,
    ) -> str:
        """Базовый VL-вызов (chat completions). Возвращает текст ответа."""
        model_cfg_api_name = vl_model  # свободный текст из Admin Valves
        processed: List[str] = []
        if images:
            await self._emit("🖼️ Обработка изображений для VL...")
            processed = await asyncio.gather(
                *[self.processor.process(u) for u in images]
            )
        messages = self._build_vl_messages(
            system_prompt=system_prompt, prompt=user_prompt, images=processed
        )
        payload: Dict[str, Any] = {
            "model": model_cfg_api_name,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
        }
        await self._emit(f"🧠 VL-запрос: {model_cfg_api_name}...")
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            try:
                resp = await client.post(
                    self.chat_url, headers=self._headers(), json=payload
                )
            except httpx.TimeoutException:
                raise TimeoutError("VL request timed out")
            except httpx.HTTPError as e:
                raise APIError(0, str(e))
        if resp.status_code >= 400:
            body_text = (resp.text or "").lower()
            # Guard: VL_MODEL не подходит для vision-запроса. Два случая:
            #   (1) текстовая модель → 400 «image_url is only supported by certain models»
            #   (2) image-модель на chat/completions → 404 «... is an image generation
            #       model and cannot be used with the chat/completions endpoint»
            is_non_vision = resp.status_code in (400, 404) and (
                "image_url" in body_text
                or "output modalities" in body_text
                or "badrequestfordependentservice" in body_text
                or "only supported by certain models" in body_text
                or "image generation model" in body_text
                or "cannot be used with the chat/completions endpoint" in body_text
            )
            if is_non_vision:
                raise ValidationError(
                    f"VL_MODEL '{model_cfg_api_name}' не подходит для анализа "
                    f"изображений (upstream вернул {resp.status_code}: это не vision-модель "
                    f"— либо текстовая, либо image-генератор). Смените VL_MODEL в Admin "
                    f"Valves на vision-модель (напр. {DEFAULT_VL_MODEL}, "
                    f"anthropic/claude-3.5-sonnet, openai/gpt-4o). "
                    f"Подробности: {resp.text[:300]}"
                )
            raise APIError(resp.status_code, resp.text)
        data = self._parse_json(resp)
        choices = data.get("choices") or []
        if not choices:
            raise GenerationError("No choices in VL response")
        msg = (choices[0] or {}).get("message") or {}
        text = _extract_text_from_content(msg.get("content"))
        return (text or "").strip()

    async def capture_scene(
        self,
        images: List[str],
        mode: str,
        annotation_text: str,
        vl_model: str,
        capture_prompt: str = "",
    ) -> str:
        """Вызвать VL для построения SCENE_BRIEF. Возвращает brief с маркером."""
        brief_type = MODE_TO_BRIEF_TYPE.get(mode, "interior")
        template = BRIEF_TEMPLATES.get(brief_type, BRIEF_TEMPLATES["interior"])
        sys_prompt = capture_prompt or SCENE_CAPTURE_SYSTEM_PROMPT_DEFAULT
        user_prompt = (
            f"Тип сцены: {brief_type}.\n"
            f"Шаблон паспорта (заполни ВСЕ поля по изображению):\n\n{template}\n\n"
        )
        if annotation_text:
            user_prompt += (
                "Пояснения разметки пользователя (пронумерованные метки):\n"
                f"{annotation_text}\n"
                "Добавь раздел «## Правила разметки (ПРИОРИТЕТ)» по этим меткам.\n"
            )
        raw = await self._vl_call(
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            images=images,
            vl_model=vl_model,
            max_tokens=2000,
            timeout_s=VL_CAPTURE_TIMEOUT,
        )
        # Пустой/нерелевантный ответ VL не должен считаться «успешным» захватом:
        # иначе retry проглатывает реальную ошибку, и fallback-ветки оркестратора
        # (capture-fail → free, spec раздел 8) не срабатывают.
        if not raw or not raw.strip() or "# " not in raw:
            raise GenerationError(
                f"VL returned empty/invalid scene brief (mode={mode})"
            )
        brief = ensure_brief_marker(raw, brief_type)
        validate_brief_length(brief)
        return brief

    async def read_annotations(
        self,
        images: List[str],
        vl_model: str,
        edit_prompt: str = "",
    ) -> List[Annotation]:
        """Вызвать VL для чтения пронумерованных меток → list[Annotation]."""
        sys_prompt = edit_prompt or SCENE_EDIT_SYSTEM_PROMPT_DEFAULT
        raw = await self._vl_call(
            system_prompt=sys_prompt,
            user_prompt="Прочитай пронумерованные метки на изображении и верни JSON-список.",
            images=images,
            vl_model=vl_model,
            max_tokens=900,
            timeout_s=VL_ANNOTATION_TIMEOUT,
        )
        # VL может обернуть JSON в ```json ... ``` — извлечём.
        import re as _re

        m = _re.search(r"\[.*\]", raw, _re.DOTALL)
        payload_text = m.group(0) if m else raw
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            return []
        anns: List[Annotation] = []
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                try:
                    anns.append(
                        Annotation(
                            n=int(item.get("n")),
                            verb=str(item.get("verb", "EDIT")).upper(),
                            zone=str(item.get("zone", "")),
                            target=item.get("target"),
                        )
                    )
                except (TypeError, ValueError):
                    continue
        return anns


# ==================== MAIN PIPE ====================


class Pipe:
    """OpenWebUI Pipe for ImageRouter Gemini image generation."""

    class Valves(BaseModel):
        # --- ключи API ---
        IMAGEROUTER_API_KEY: str = Field(
            default="",
            title="ImageRouter API ключ",
            description="ImageRouter API ключ (единый глобальный)",
        )
        ALLOWED_EMAILS: str = Field(
            default="",
            title="Список доступа (email)",
            description="Allowlist email'ов через запятую. Пусто = разрешить всем.",
        )

        # --- админские настройки подключения ---
        IMAGEROUTER_API_BASE_URL: str = Field(
            default="https://api.imagerouter.io/v1/openai",
            title="Базовый URL ImageRouter API",
            description=(
                "Базовый URL OpenAI-совместимого API ImageRouter. "
                "Эндпоинты /images/generations и /chat/completions строятся от него. "
                "Можно указать прокси/локальный шлюз."
            ),
        )
        READ_TIMEOUT_SECONDS: int = Field(
            default=120,
            title="Таймаут чтения, сек",
            description=(
                "Сколько ждать ответа генерации (read timeout httpx). "
                "4K/видео-модели могут отвечать дольше — увеличьте."
            ),
        )

        # --- сервер списания кредитов ---
        REPORT_TO_CREDITS_SERVER: bool = Field(
            default=True,
            title="Отчитываться на сервер списания",
            description=(
                "Отправлять данные о каждой генерации (модель, стоимость $, "
                "успех/ошибка) на сервер списания кредитов. "
                "False — полностью выключить списание."
            ),
        )
        CREDITS_SERVER_URL: str = Field(
            default="https://credits.dev-bim.com",
            title="Адрес сервера списания кредитов",
            description=(
                "Публичный адрес сервера списания (через Cloudflare) или "
                "http://localhost:4010 при локальном запуске."
            ),
        )
        CREDITS_API_KEY: str = Field(
            default="devbim2026",
            title="Ключ сервера списания",
            description=(
                "Bearer-ключ приёма отчётов (CREDITS_API_KEY в .env сервера "
                "списания).",
            ),
            json_schema_extra={"input": {"type": "password"}},
        )

        # --- МОДЕЛИ ---
        IMAGE_MODEL: str = Field(
            default="google/nano-banana-pro",
            title="Модель генерации изображения (дефолт)",
            description=(
                "Дефолт/фоллбэк image-модели — используется, если пользователь не выбрал "
                "модель в User Valves. Слаги ImageRouter (imagerouter.io/models)."
            ),
            json_schema_extra={"enum": IMAGE_MODEL_ENUM},
        )
        VL_MODEL: str = Field(
            default=DEFAULT_VL_MODEL,
            title="VL-модель (анализатор сцены/разметки)",
            description="ImageRouter API-имя vision-language модели (свободный текст).",
        )

        # --- промпты ---
        ADMIN_SYSTEM_PROMPT: str = Field(default="", title="Админ-промпт (префикс)")
        VL_SYSTEM_PROMPT: str = Field(
            default=(
                "Ты — помощник по изображению. Твоя задача: задавать уточняющие вопросы "
                "по загруженной картинке и помогать формировать качественные промпты. "
                "Отвечай кратко, структурировано, в Markdown и на русском языке."
            ),
            description="System prompt для VL-помощника (режим prompt_assist)",
        )
        SCENE_CAPTURE_SYSTEM_PROMPT: str = Field(
            default=SCENE_CAPTURE_SYSTEM_PROMPT_DEFAULT,
            title="Промпт захвата сцены",
            description="Инструкция VL для построения SCENE_BRIEF.",
        )
        SCENE_EDIT_SYSTEM_PROMPT: str = Field(
            default=SCENE_EDIT_SYSTEM_PROMPT_DEFAULT,
            title="Промпт чтения разметки",
            description="Инструкция VL для чтения пронумерованных меток.",
        )

        # --- http ---
        RETURN_ALL_IMAGES: bool = Field(
            default=False,
            title="Показывать все результаты",
            description="Если true — все изображения; иначе только первое.",
        )
        ALLOW_DEBUG_INFO_IN_CHAT: bool = Field(
            default=True,
            title="Разрешить служебную информацию в чате",
            description=(
                "Админский рубильник: разрешает ли вообще вывод служебного блока "
                "(модель/размер/стоимость/latency) в чат. Если False — "
                "пользовательский тумблер SHOW_DEBUG_INFO игнорируется."
            ),
        )

    class UserValves(BaseModel):
        IMAGE_MODEL: str = Field(
            default="google/nano-banana-pro",
            title="Модель генерации изображения",
            description=(
                "Выбор image-модели для генерации (слаги ImageRouter). "
                "Если не задано — берётся дефолт из Admin Valves."
            ),
            json_schema_extra={"enum": IMAGE_MODEL_ENUM},
        )
        MODE: str = Field(
            default="interior",
            title="Режим генерации",
            description="interior/facade/masterplan — tailored-режимы консистентной "
            "сцены; free — обычная генерация; prompt_assist — VL-помощник.",
            json_schema_extra={"enum": sorted(SCENE_MODES)},
        )
        VIEW: str = Field(
            default="none",
            title="Пресет ракурса",
            description="Зависит от MODE. none = по тексту/разметке.",
            # enum собираем из объединения всех режимов (UI не умеет динамический enum
            # по MODE, поэтому показываем все; валидация по MODE — в pipe()).
            json_schema_extra={
                "enum": sorted(
                    {v for vals in VIEW_VALUES_BY_MODE.values() for v in vals}
                )
            },
        )
        SCENE_AUTO_CAPTURE: bool = Field(
            default=False,
            title="Захват сцены (рубильник)",
            description="Полный выключатель захвата сцены. True — паспорт строится "
            "по фото автоматически (или по команде «захвати сцену» — "
            "форс-перезахват). False — захват выключен: запросы "
            "выполняются как обычная генерация/редактирование "
            "(приложенное фото — референс), без блокировок.",
        )
        STYLE: str = Field(
            default="фотореализм",
            title="Стиль",
            description="Стиль (нет/none = не добавлять стиль в промпт).",
            json_schema_extra={"enum": STYLE_UI_ENUM_RU},
        )
        ASPECT_RATIO: str = Field(
            default="1:1",
            title="Соотношение сторон",
            json_schema_extra={"enum": sorted(list(KNOWN_VALUES["ASPECT_RATIO"]))},
        )
        RESOLUTION: str = Field(
            default="1K",
            title="Размер изображения",
            description=(
                "1K/2K/4K. Реальная поддержка зависит от модели — провайдер ограничит "
                "(clamp) неподдерживаемое. Дефолт 1K (обратная совместимость)."
            ),
            json_schema_extra={"enum": RESOLUTION_VALUES},
        )
        OUTPUT_FORMAT: str = Field(
            default="jpeg",
            title="Формат",
            json_schema_extra={"enum": ["jpeg", "png", "webp"]},
        )
        SHOW_DEBUG_INFO: bool = Field(
            default=False,
            title="Служебная информация в чат",
            description=(
                "Показывать под результатом блок служебной информации: разрешённая "
                "модель и API-эндпоинт, итоговый размер, число референсов, latency, "
                "стоимость (cost от ImageRouter), предупреждения. "
                "Работает только если админ не запретил (ALLOW_DEBUG_INFO_IN_CHAT)."
            ),
        )
        SHOW_CREDITS_INFO: bool = Field(
            default=True,
            title="Отчёт о списании в чат",
            description=(
                "Показывать под результатом блок «Отчёт списания кредитов»: "
                "→ запрос на сервер списания (модель, $), ← ответ (credits, "
                "pricing, cost). Работает только если админ включил "
                "REPORT_TO_CREDITS_SERVER."
            ),
        )

    def __init__(self):
        self.type = "pipe"
        self.id = "imagerouter_scene_image"
        self.name = f"ImageRouter Scene Image v{PIPE_VERSION}"
        self.valves = self.Valves()
        self.user_valves = self.UserValves()
        self.emitter = None
        self.model_configs = MODEL_CONFIGS
        # Последняя генерация: служебные данные для отчёта на сервер списания.
        self._last_gen_meta: Dict[str, Any] = {}

    def _get_inputs(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[Optional[str], List[str]]:
        """Extract latest user text and images from OpenWebUI-like messages."""
        text, images = "", []
        for msg in reversed(messages or []):
            if msg.get("role") != "user":
                continue

            content = msg.get("content", "")
            if isinstance(content, str):
                return content, []

            if isinstance(content, list):
                for part in content:
                    if (part or {}).get("type") == "text":
                        text = part.get("text", "")
                    elif (part or {}).get("type") == "image_url":
                        img_url = (
                            (part.get("image_url") or {}).get("url") or ""
                        ).strip()
                        if img_url:
                            images.append(img_url)
                return text, images

        return None, []

    def _last_image_from_history(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Найти самое свежее изображение в истории сообщений (для ref-image при traverse).

        Идём с конца; возвращаем первый найденный image_url. Нужно, чтобы text-only
        запрос нового ракурса («обойди вправо») всё равно отправлял исходное фото
        как image-to-image ref (spec раздел 3.2).
        """
        for msg in reversed(messages or []):
            if not isinstance(msg, dict) or msg.get("role") not in (
                "user",
                "assistant",
            ):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "image_url":
                        url = ((part.get("image_url") or {}).get("url") or "").strip()
                        if url:
                            return url
        return None

    # ==================== SCENE MODE HELPERS (spec раздел 3) ====================

    async def _generate_free(
        self, provider: "ImageRouterProvider", active_config: Dict[str, Any]
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Генерация в free-режиме: VIEW -> free preset.
        MODEL: выбор пользователя (UserValves) либо дефолт Admin Valves."""
        view_prompt = active_config.pop("_free_view_prompt", "")
        style = _normalize_style_value(active_config.get("STYLE", "фотореализм"))
        parts: List[str] = []
        if self.valves.ADMIN_SYSTEM_PROMPT:
            parts.append(self.valves.ADMIN_SYSTEM_PROMPT.strip())
        if style and style != "none" and style in STYLE_PROMPTS:
            parts.append(STYLE_PROMPTS[style])
        if view_prompt:
            parts.append(view_prompt)
        if active_config.get("prompt"):
            parts.append(active_config["prompt"].strip())
        # Модель: UserValves.IMAGE_MODEL (если задано) иначе Admin Valves дефолт.
        user_model = (active_config.get("IMAGE_MODEL") or "").strip()
        active_config["MODEL"] = user_model or self.valves.IMAGE_MODEL
        active_config["_full_prompt"] = _safe_join_sentences(parts)
        return await provider.generate(
            {**active_config, "prompt": active_config["_full_prompt"]},
            self.valves.ADMIN_SYSTEM_PROMPT,
        )

    # ==================== ОТЧЁТ О СПИСАНИИ КРЕДИТОВ ====================

    _CREDITS_TIMEOUT = 6.0  # сек на обмен с сервером списания

    async def _report_credits(
        self,
        *,
        email: str,
        user_id: str,
        model: str,
        cost_usd: Optional[float],
        is_success: bool,
        error_message: str = "",
        request_date: str,
    ) -> str:
        """Отправить отчёт на сервер списания и вернуть текстовый блок для чата.

        Отчёт на сервер уходит всегда (при включённом REPORT_TO_CREDITS_SERVER).
        Markdown-блок для чата возвращается только если пользователь включил
        SHOW_CREDITS_INFO. Никогда не бросает исключений: сбой сервера списания
        не должен ронять генерацию. Возвращает markdown-блок либо "".
        """
        if not self.valves.REPORT_TO_CREDITS_SERVER:
            return ""
        server = (self.valves.CREDITS_SERVER_URL or "").rstrip("/")
        if not server:
            return ""

        def _chat_block(*lines: str) -> str:
            """Обернуть строки в markdown-блок, если показ включён."""
            if not self.user_valves.SHOW_CREDITS_INFO:
                return ""
            return "---\n**Отчёт списания кредитов**\n```\n" + "\n".join(lines) + "\n```"

        response_date = datetime.now(timezone.utc).isoformat()
        payload: Dict[str, Any] = {
            "email": email or "unknown@example.com",
            "function": "ImageRouter Scene Image Generator",
            "model": model or "",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Изображения не считаются по токенам — только реальный cost $
            # от ImageRouter (provider) либо ничего (курс по модели в GUI).
            "tokens": 0,
            "user_id": user_id or "",
            "request_date": request_date,
            "response_date": response_date,
            "is_success": is_success,
        }
        if cost_usd is not None:
            payload["cost_usd"] = float(cost_usd)
        if not is_success and error_message:
            payload["error_message"] = error_message[:500]

        headers = {"Content-Type": "application/json"}
        if self.valves.CREDITS_API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.CREDITS_API_KEY}"

        url = f"{server}/api/usage"
        cost_repr = f"${cost_usd}" if cost_usd is not None else "—"
        send_line = f"→ POST {url} · model={model or '—'} · cost={cost_repr} · success={is_success}"
        logger.info(f"credits report: {send_line}")

        try:
            async with httpx.AsyncClient(timeout=self._CREDITS_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # сеть/таймаут/DNS — глушим, пайп живёт
            logger.warning(f"credits report failed: {type(exc).__name__}: {exc}")
            return _chat_block(
                send_line,
                f"✗ Сервер списания недоступен: {type(exc).__name__}: {exc}",
            )

        if resp.status_code >= 400:
            body = (resp.text or "")[:200]
            logger.warning(f"credits report HTTP {resp.status_code}: {body}")
            return _chat_block(send_line, f"✗ HTTP {resp.status_code} · {body}")

        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {}
        recv_parts = [f"← HTTP {resp.status_code}"]
        if data:
            recv_parts.append(f"credits={data.get('credits')}")
            recv_parts.append(f"pricing={data.get('pricing')}")
            if data.get("cost_usd") is not None:
                recv_parts.append(f"cost=${data.get('cost_usd')} ({data.get('cost_source')})")
        recv_line = " · ".join(recv_parts)
        logger.info(f"credits report OK: {recv_line}")
        return _chat_block(send_line, recv_line)

    def _format_image_output(
        self,
        images_out: List[str],
        gen_meta: Dict[str, Any],
        active_config: Dict[str, Any],
        mode: str,
    ) -> str:
        # Помним последнюю генерацию — по ней pipe() шлёт отчёт о списании.
        self._last_gen_meta = dict(gen_meta or {})
        if not bool(self.valves.RETURN_ALL_IMAGES):
            images_out = (images_out or [])[:1]
        blocks = ["🎨 **Изображение успешно сгенерировано**\n"]
        warning = (gen_meta or {}).get("warning")
        if isinstance(warning, str) and warning.strip():
            blocks.append(f"⚠️ **Warning:** {warning.strip()}\n")
        for idx, data_url in enumerate(images_out, start=1):
            blocks.append(f"**Result {idx}**\n\n![Result {idx}]({data_url})\n")
        details = (
            f"⚙️ `v{PIPE_VERSION} | Режим: {mode} | Модель: {active_config.get('MODEL')} | "
            f"{active_config.get('STYLE')} | {active_config.get('ASPECT_RATIO')} | "
            f"{active_config.get('RESOLUTION')} | {active_config.get('VIEW')} | "
            f"fmt={active_config.get('OUTPUT_FORMAT')}`"
        )
        blocks.append(details)

        # Служебный блок в чат: пользовательский тумблер SHOW_DEBUG_INFO,
        # разрешённый админом (ALLOW_DEBUG_INFO_IN_CHAT).
        if (
            active_config.get("SHOW_DEBUG_INFO")
            and self.valves.ALLOW_DEBUG_INFO_IN_CHAT
        ):
            m = gen_meta or {}
            lines = [
                f"api:        {m.get('used_api')} → {m.get('request_url')}",
                f"модель:     {m.get('request_model')}",
                f"размер:     {m.get('request_size')} ({m.get('used_resolution')})",
                f"референсов: {m.get('request_refs')}",
            ]
            if m.get("latency") is not None:
                lines.append(f"latency:    {m.get('latency')}")
            if m.get("provider_cost_usd") is not None:
                lines.append(f"cost:       ${m.get('provider_cost_usd')}")
            lines.append(f"timeout_r:  {m.get('read_timeout_seconds')} c")
            if isinstance(warning, str) and warning.strip():
                lines.append(f"warning:    {warning.strip()}")
            blocks.append(
                "📊 **Служебная информация**\n```\n" + "\n".join(lines) + "\n```"
            )
        return "\n".join(blocks)

    async def _run_scene_mode(
        self,
        provider: "ImageRouterProvider",
        body: Dict[str, Any],
        active_config: Dict[str, Any],
        mode: str,
        prompt: str,
        images: List[str],
    ):
        """Оркестрация interior/facade/masterplan (spec раздел 3)."""
        messages = body.get("messages", []) or []
        view = active_config.get("VIEW", "none")
        style = active_config.get("STYLE", "фотореализм")
        auto_capture = bool(active_config.get("SCENE_AUTO_CAPTURE"))
        trigger = self._is_capture_trigger(prompt)
        existing_brief = find_latest_scene_brief(messages)

        # Несоответствие типа: предупреждение.
        no_brief_warn = ""
        if existing_brief:
            brief_type = extract_brief_type(existing_brief) or ""
            expected_type = MODE_TO_BRIEF_TYPE.get(mode, "")
            if brief_type and expected_type and brief_type != expected_type:
                if auto_capture:
                    yield (
                        f"⚠️ Найден паспорт сцены типа `{brief_type}`, но текущий режим — "
                        f"`{mode}` (ожидался `{expected_type}`). Перехватите сцену заново "
                        f"(команда «захвати сцену» при включённом SCENE_AUTO_CAPTURE)."
                    )
                    return
                # Рубильник выключен: не блокируем — паспорт игнорируется.
                no_brief_warn = (
                    f"⚠️ Паспорт сцены типа `{brief_type}` не соответствует режиму "
                    f"`{mode}` и проигнорирован (SCENE_AUTO_CAPTURE выключен)."
                )
                existing_brief = None

        # ФАЗА ЗАХВАТА.
        # need_capture действует ТОЛЬКО по рубильнику SCENE_AUTO_CAPTURE:
        # - тумблер OFF → захват полностью выключен (даже явная команда);
        # - тумблер ON  → авто-захват при отсутствии паспорта ИЛИ форс-перезахват
        #   по явной команде («захвати сцену») даже при существующем паспорте.
        # Capture ВСЕГДА требует изображения (spec раздел 3.1: «фото + триггер
        # захвата») — без фото VL не из чего строить паспорт.
        need_capture = auto_capture and (trigger or not existing_brief)
        if need_capture and not images:
            # Триггер захвата/обхода есть, но изображения нет — попросить загрузить.
            yield "Загрузите изображение сцены, чтобы захватить паспорт."
            return
        if need_capture and images:
            try:
                brief = await self._capture_with_retry(
                    provider,
                    images=images,
                    mode=mode,
                    annotation_text=prompt,
                )
            except ImageRouterPipeError as e:
                # Fallback на free.
                logger.warning(f"Scene capture failed, fallback to free: {e}")
                yield (
                    f"⚠️ Не удалось построить паспорт сцены ({_short_error(e)}). "
                    f"Генерация без консистентности.\n"
                )
                active_config["MODE"] = "free"
                active_config["_free_view_prompt"] = FREE_VIEW_PROMPTS.get(
                    active_config.get("VIEW", "none"), ""
                )
                images_out, gen_meta = await self._generate_free(
                    provider, active_config
                )
                yield self._format_image_output(
                    images_out, gen_meta, active_config, "free"
                )
                return
            yield brief + "\n\n_Паспорт сцены захвачен. Теперь просите новый ракурс._"
            return

        # ФАЗА ОБХОДА: есть existing_brief.
        brief = existing_brief
        if not brief:
            # Рубильник выключен и паспорта нет (сюда попадаем только при OFF —
            # при ON отсутствие паспорта обрабатывается фазой захвата выше).
            # НЕ блокируем пользователя: обычная генерация/редактирование (free)
            # с учётом остальных вентилей (модель/стиль/ракурс/размер/формат);
            # приложенное фото (или последнее из истории) идёт как референс.
            ref_images = images
            if not ref_images:
                last_img = self._last_image_from_history(messages)
                if last_img:
                    ref_images = [last_img]
            active_config["MODE"] = "free"
            active_config["_free_view_prompt"] = FREE_VIEW_PROMPTS.get(view, "")
            active_config["images"] = ref_images
            images_out, gen_meta = await self._generate_free(provider, active_config)
            out = self._format_image_output(images_out, gen_meta, active_config, "free")
            yield (no_brief_warn + "\n\n" + out) if no_brief_warn else out
            return

        # Чтение разметки (если есть изображение с метками).
        annotations: List[Annotation] = []
        if images and self._looks_annotated(prompt):
            try:
                annotations = await self._read_annotations_with_retry(
                    provider, images=images
                )
            except ImageRouterPipeError as e:
                logger.warning(f"Annotation read failed, continuing without: {e}")

        # Дополнительно: разбор пояснений из текста пользователя (без VL).
        annotations.extend(parse_annotation_text(prompt))

        kept, warn = truncate_annotations(annotations)
        full_prompt = assemble_scene_prompt(
            mode=mode,
            brief=brief,
            annotations=kept,
            user_text=prompt,
            view=view,
            style=style,
        )

        # Spec раздел 3.2: исходное фото всегда идёт как ref-image (image-to-image).
        # Если текущее сообщение без изображения (text-only traverse) — берём
        # последнее изображение из истории.
        traverse_images = images
        if not traverse_images:
            last_img = self._last_image_from_history(messages)
            if last_img:
                traverse_images = [last_img]

        # Модель: UserValves.IMAGE_MODEL (если задано) иначе Admin Valves дефолт.
        user_model = (active_config.get("IMAGE_MODEL") or "").strip()
        chosen_model = user_model or self.valves.IMAGE_MODEL
        gen_config = {
            **active_config,
            "MODEL": chosen_model,
            "prompt": full_prompt,
            "images": traverse_images,
        }
        images_out, gen_meta = await provider.generate(
            gen_config,
            self.valves.ADMIN_SYSTEM_PROMPT,
        )
        out = self._format_image_output(images_out, gen_meta, active_config, mode)
        if warn:
            out = warn + "\n\n" + out
        yield out

    # ----- мелкие хелперы режима сцены -----

    @staticmethod
    def _is_capture_trigger(text: str) -> bool:
        return bool(_CAPTURE_TRIGGER_RE.search(text or ""))

    @staticmethod
    def _looks_annotated(text: str) -> bool:
        # Эвристика: в тексте есть строки вида "N = ..."
        return bool(_ANNOT_LINE_RE.search(text or ""))

    async def _capture_with_retry(
        self,
        provider: "ImageRouterProvider",
        images: List[str],
        mode: str,
        annotation_text: str,
    ) -> str:
        last = None
        for _ in range(MAX_VL_RETRY + 1):
            try:
                return await provider.capture_scene(
                    images=images,
                    mode=mode,
                    annotation_text=annotation_text,
                    vl_model=self.valves.VL_MODEL,
                    capture_prompt=self.valves.SCENE_CAPTURE_SYSTEM_PROMPT,
                )
            except ImageRouterPipeError as e:
                last = e
        assert last is not None
        raise last

    async def _read_annotations_with_retry(
        self, provider: "ImageRouterProvider", images: List[str]
    ) -> List[Annotation]:
        last = None
        for _ in range(MAX_VL_RETRY + 1):
            try:
                return await provider.read_annotations(
                    images=images,
                    vl_model=self.valves.VL_MODEL,
                    edit_prompt=self.valves.SCENE_EDIT_SYSTEM_PROMPT,
                )
            except ImageRouterPipeError as e:
                last = e
        assert last is not None
        raise last

    def _omni_search_settings(self, *roots) -> Dict[str, str]:
        """Recursively search for known settings in nested structures.

        Приоритет поиска (от высшего к низшему):
        1. kwargs (явно переданные параметры)
        2. body (тело запроса)
        3. __user__ (пользовательские данные)

        Важно: OpenWebUI иногда передаёт user valves не как plain dict,
        а как объект/Pydantic-модель. Поэтому перед обходом нормализуем
        такие объекты в dict через `model_dump()`/`dict()`.
        """

        found: Dict[str, str] = {}
        recursion_depth = [0]

        logger.info(f"Starting omni_search_settings with KNOWN_VALUES: {KNOWN_VALUES}")

        def _normalize_container(x: Any) -> Any:
            """Best-effort: convert Pydantic/objects into plain dict/list for recursive search."""

            if x is None:
                return None

            # Pydantic v2 BaseModel
            if isinstance(x, BaseModel):
                try:
                    return x.model_dump()
                except Exception:
                    return x

            # Pydantic-like / custom objects
            if hasattr(x, "model_dump") and callable(getattr(x, "model_dump")):
                try:
                    return x.model_dump()  # type: ignore[no-any-return]
                except Exception:
                    pass

            if hasattr(x, "dict") and callable(getattr(x, "dict")):
                try:
                    return x.dict()  # type: ignore[no-any-return]
                except Exception:
                    pass

            # Generic objects: last-resort inspect __dict__ (avoid types/modules)
            try:
                if hasattr(x, "__dict__") and not isinstance(x, (type,)):
                    return vars(x)
            except Exception:
                pass

            return x

        def _find_matching_value(obj: Any, category: str) -> Optional[str]:
            """Find a valid value for a specific category inside nested containers.

            This is used to support nested shapes like:
              {"STYLE": {"value": "sketch"}}
            without scanning arbitrary free-text prompts.
            """

            obj = _normalize_container(obj)

            if isinstance(obj, str):
                v = obj.lower().strip()
                return v if v in KNOWN_VALUES.get(category, set()) else None

            if isinstance(obj, dict):
                for _k, _v in obj.items():
                    hit = _find_matching_value(_v, category)
                    if hit:
                        return hit
                return None

            if isinstance(obj, (list, tuple, set)):
                for _it in obj:
                    hit = _find_matching_value(_it, category)
                    if hit:
                        return hit
                return None

            return None

        def recurse(obj: Any, source_name: str = "unknown"):
            recursion_depth[0] += 1
            if recursion_depth[0] > MAX_RECURSION_DEPTH:
                raise ValidationError(
                    f"Maximum recursion depth {MAX_RECURSION_DEPTH} exceeded"
                )

            try:
                obj = _normalize_container(obj)

                if isinstance(obj, dict):
                    logger.debug(f"Searching dict in {source_name}: {list(obj.keys())}")
                    for k, v in obj.items():
                        # Prefer key-based extraction; this prevents accidental matches from free-text.
                        if isinstance(k, str):
                            ku = k.upper()

                            # Avoid scanning prompt/chat content for settings.
                            if source_name == "body" and k.lower() in (
                                "messages",
                                "content",
                                "prompt",
                            ):
                                continue

                            if ku in KNOWN_VALUES:
                                if isinstance(v, str):
                                    clean = v.lower().strip()
                                    if clean in KNOWN_VALUES[ku]:
                                        logger.info(
                                            f"Found {ku}={clean} in {source_name}"
                                        )
                                        found[ku] = clean
                                else:
                                    hit = _find_matching_value(v, ku)
                                    if hit:
                                        logger.info(
                                            f"Found {ku}={hit} in {source_name}"
                                        )
                                        found[ku] = hit
                                # Don't recurse into this subtree (prevents duplicate logs + prompt interference).
                                continue

                        recurse(v, source_name)
                    return

                if isinstance(obj, (list, tuple, set)):
                    logger.debug(
                        f"Searching list-like in {source_name} with {len(obj)} items"
                    )
                    for it in obj:
                        recurse(it, source_name)
                    return

                # Do not do global fuzzy string matching; settings must come from explicit keys
                # or from nested containers under those keys (handled via _find_matching_value()).
                if isinstance(obj, str):
                    return

            finally:
                recursion_depth[0] -= 1

        # Search from lowest to highest priority so higher-priority sources overwrite.
        default_source_names = ["__user__", "body", "kwargs"]
        source_names: List[str] = []
        for idx in range(len(roots)):
            if idx < len(default_source_names):
                source_names.append(default_source_names[idx])
            else:
                source_names.append(f"explicit_{idx - len(default_source_names) + 1}")

        for root, name in zip(roots, source_names):
            if root is not None:
                logger.info(f"Searching in {name}")
                recurse(root, name)

        logger.info(f"Final found settings: {found}")
        return found

    async def pipe(self, body: dict, __event_emitter__=None, __user__=None, **kwargs):
        self.emitter = __event_emitter__

        # Safe diagnostics: helps identify where OpenWebUI passes user valves.
        try:
            body_keys = list((body or {}).keys()) if isinstance(body, dict) else []
        except Exception:
            body_keys = []

        try:
            kwargs_keys = list(kwargs.keys())
        except Exception:
            kwargs_keys = []

        logger.info(
            "DEBUG: pipe() input shapes: "
            f"body_keys={body_keys} | kwargs_keys={kwargs_keys} | __user__={type(__user__).__name__}"
        )
        for k in ("user_valves", "valves", "__user_valves__", "__valves__"):
            if isinstance(kwargs, dict) and k in kwargs:
                logger.info(f"DEBUG: kwargs[{k}] type={type(kwargs.get(k)).__name__}")

        email = _get_user_email(__user__)

        # Experimental allowlist access control (fail-closed).
        allowed_raw = (self.valves.ALLOWED_EMAILS or "").strip()
        allowed = {e.strip().lower() for e in allowed_raw.split(",") if e.strip()}
        if allowed and (not email or email not in allowed):
            yield "Error: Access denied."
            return

        # Единый глобальный ключ (valve или env).
        api_key = (
            self.valves.IMAGEROUTER_API_KEY or os.getenv("IMAGEROUTER_API_KEY") or ""
        ).strip()
        if not api_key:
            yield "Error: ImageRouter API key is not configured (set IMAGEROUTER_API_KEY)."
            return

        # В OpenWebUI 0.6+ UserValves передаются канонически через
        # __user__["valves"] как Pydantic-объект. Раньше (старые версии)
        # значения могли приходить в разных местах тела/kwargs, поэтому
        # старый код делал рекурсивный "omni-search" по body/__user__/kwargs.
        # Этот обход на новых версиях проваливался через MAX_RECURSION_DEPTH=10
        # из-за более глубокой структуры body.metadata — поэтому полностью
        # убран в пользу одного канонического источника.
        active_config = self.user_valves.model_dump()
        logger.info(
            f"Initial active_config from UserValves (defaults): {active_config}"
        )

        found_settings: Dict[str, str] = {}
        try:
            user_valves_obj = None
            if isinstance(__user__, dict):
                user_valves_obj = __user__.get("valves")

            if user_valves_obj is not None:
                # Pydantic v2 UserValves instance.
                if isinstance(user_valves_obj, BaseModel):
                    found_settings = user_valves_obj.model_dump()
                elif isinstance(user_valves_obj, dict):
                    found_settings = dict(user_valves_obj)
                else:
                    # Best-effort для нестандартных объектов.
                    dumper = getattr(user_valves_obj, "model_dump", None) or getattr(
                        user_valves_obj, "dict", None
                    )
                    if callable(dumper):
                        found_settings = dumper()

                # Оставляем только ключи, которые реально принадлежат UserValves,
                # чтобы не залить active_config посторонними полями.
                # Pydantic v2: model_fields; Pydantic v1: __fields__.
                fields = getattr(self.UserValves, "model_fields", None)
                if fields is None:
                    fields = getattr(self.UserValves, "__fields__", None) or {}
                allowed = set(fields.keys())
                found_settings = {
                    k: v for k, v in (found_settings or {}).items() if k in allowed
                }
                logger.info(f"Found settings from __user__['valves']: {found_settings}")
                active_config.update(found_settings)
                logger.info(f"Final active_config after update: {active_config}")
        except Exception as e:
            logger.error(f"Settings validation error: {e}")
            yield f"Error: Invalid settings - {str(e)}"
            return

        prompt, images = self._get_inputs((body or {}).get("messages", []))
        if not prompt and not images:
            yield "Please provide a prompt or image."
            return
        if prompt and prompt.strip().startswith("### Task:"):
            return

        active_config["prompt"] = prompt or ""
        active_config["images"] = images

        # Контекст отчёта о списании: кто и когда инициировал генерацию.
        self._last_gen_meta = {}
        credits_email = email or "unknown@example.com"
        credits_user_id = (
            str((__user__ or {}).get("id") or "")
            if isinstance(__user__, dict)
            else ""
        )
        credits_request_date = datetime.now(timezone.utc).isoformat()

        # --- Определение режима ---
        mode = (active_config.get("MODE") or "interior").lower().strip()
        if mode not in SCENE_MODES:
            mode = "interior"

        try:
            # Таймауты: read из админ-вентиля READ_TIMEOUT_SECONDS.
            read_s = max(1, int(self.valves.READ_TIMEOUT_SECONDS or 120))
            timeout = httpx.Timeout(
                connect=10.0, read=float(read_s), write=10.0, pool=10.0
            )
            provider = ImageRouterProvider(
                api_key=api_key,
                timeout=timeout,
                model_configs=self.model_configs,
                emitter=self.emitter,
                base_url=self.valves.IMAGEROUTER_API_BASE_URL,
            )

            # --- prompt_assist: VL-помощник (без изменений по смыслу) ---
            if mode == "prompt_assist":
                vl_model = (self.valves.VL_MODEL or DEFAULT_VL_MODEL).strip()
                history_messages = (body or {}).get("messages", []) or []
                text_out = await provider.assist_with_history(
                    history_messages=history_messages,
                    vl_model=vl_model,
                    system_prompt=self.valves.VL_SYSTEM_PROMPT,
                )
                yield "\n".join(
                    [
                        "🧠 **VL помощник по промпту**\n",
                        f"**VL-модель:** `{vl_model}`\n",
                        text_out.strip(),
                        "\n⚙️ `режим=prompt_assist`",
                    ]
                )
                if self.emitter:
                    await self.emitter(
                        {
                            "type": "status",
                            "data": {"description": "✅ Done", "done": True},
                        }
                    )
                return

            # --- free: обычная генерация, VIEW -> free preset ---
            if mode == "free":
                view = active_config.get("VIEW", "none")
                active_config["_free_view_prompt"] = FREE_VIEW_PROMPTS.get(view, "")
                images_out, gen_meta = await self._generate_free(
                    provider, active_config
                )
                yield self._format_image_output(
                    images_out, gen_meta, active_config, mode
                )
                # Отчёт о списании (не роняет пайп при сбое сервера).
                block = await self._report_credits(
                    email=credits_email,
                    user_id=credits_user_id,
                    model=active_config.get("MODEL") or self.valves.IMAGE_MODEL,
                    cost_usd=(gen_meta or {}).get("provider_cost_usd"),
                    is_success=True,
                    request_date=credits_request_date,
                )
                if block:
                    yield block
                if self.emitter:
                    await self.emitter(
                        {
                            "type": "status",
                            "data": {"description": "✅ Done", "done": True},
                        }
                    )
                return

            # --- interior/facade/masterplan: режим консистентной сцены ---
            async for chunk in self._run_scene_mode(
                provider=provider,
                body=body or {},
                active_config=active_config,
                mode=mode,
                prompt=prompt or "",
                images=images,
            ):
                yield chunk
            # Отчёт о списании по последней генерации (meta запомнен в
            # _format_image_output; для capture-фазы генерации не было —
            # cost=None и отчёт не шлём, списать нечего).
            last_meta = getattr(self, "_last_gen_meta", {})
            if last_meta:
                block = await self._report_credits(
                    email=credits_email,
                    user_id=credits_user_id,
                    model=last_meta.get("request_model")
                    or active_config.get("MODEL")
                    or self.valves.IMAGE_MODEL,
                    cost_usd=last_meta.get("provider_cost_usd"),
                    is_success=True,
                    request_date=credits_request_date,
                )
                if block:
                    yield block
            if self.emitter:
                await self.emitter(
                    {"type": "status", "data": {"description": "✅ Done", "done": True}}
                )

        except ImageRouterPipeError as e:
            logger.error(f"ImageRouter pipe error: {e}")
            block = await self._report_credits(
                email=credits_email,
                user_id=credits_user_id,
                model=active_config.get("MODEL") or self.valves.IMAGE_MODEL,
                cost_usd=None,
                is_success=False,
                error_message=str(e),
                request_date=credits_request_date,
            )
            yield f"Error: {str(e)}" + (f"\n\n{block}" if block else "")
        except Exception as e:
            logger.error(f"Unexpected pipe error: {traceback.format_exc()}")
            block = await self._report_credits(
                email=credits_email,
                user_id=credits_user_id,
                model=active_config.get("MODEL") or self.valves.IMAGE_MODEL,
                cost_usd=None,
                is_success=False,
                error_message=str(e),
                request_date=credits_request_date,
            )
            yield f"Error: An unexpected error occurred - {str(e)}" + (
                f"\n\n{block}" if block else ""
            )
