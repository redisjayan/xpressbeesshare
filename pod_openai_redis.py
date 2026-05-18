#!/usr/bin/env python3
"""
Analyze POD receipt images with OpenAI (vision + text embeddings) and store results in Redis.

Requires Redis Stack (RedisJSON + RediSearch). Install: pip install openai redis pydantic

Environment:
  OPENAI_API_KEY  (required)

Edit REDIS_URL below to point at your Redis server.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Optional

import redis
import redis.exceptions
from openai import OpenAI
from pydantic import BaseModel, Field, ConfigDict
from redis.commands.search.field import NumericField, TagField, TextField, VectorField
from redis.commands.search.index_definition import IndexDefinition, IndexType
import config

# --- configure Redis ---
#REDIS_URL = "redis://localhost:6379/0"
REDIS_URL = config.REDIS_URL
os.environ['OPENAI_API_KEY'] = config.OPENAI_API_KEY

# --- OpenAI models ---
VISION_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")
EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
# Default output size for text-embedding-3-small
EMBEDDING_DIM = int(os.environ.get("OPENAI_EMBEDDING_DIM", "1536"))

DEFAULT_POD_DIR = Path("pod")
DEFAULT_INDEX_NAME = "idx:pods"
SUCCESS_POD_STREAM = "POD_Image_Stream:demo:stp_pod_stream"
FAILURE_POD_STREAM = "POD_Image_Stream:demo:error_pod_stream"

r = redis.Redis.from_url(REDIS_URL, decode_responses=True)

class AwbCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    printed_awb: Optional[str] = Field(
        default=None,
        description="AWB / barcode / tracking number visibly printed on the receipt, if any.",
    )
    matches_shipping_id: bool = Field(
        default=False,
        description="True only if the printed AWB clearly matches the expected shipping id.",
    )


class BlurCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_blur: bool = Field(description="Whether the image appears blurred / unreadable.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence between 0 and 1.")


class TornCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_torn: bool = Field(description="Whether the paper appears torn or missing pieces.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence between 0 and 1.")

class IsDamaged(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_damaged: bool = Field(description="Whether there is handwritten remarks containing damage word.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence between 0 and 1.")

class PresenceCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    present: bool = Field(description="Whether the feature appears present on the receipt.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence between 0 and 1.")


class PodVisionResult(BaseModel):
    """Structured output from the vision model."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="Short neutral summary of the receipt image content.")
    awb_check: AwbCheck
    blur_check: BlurCheck
    torn_check: TornCheck
    consignee_stamp: PresenceCheck
    receiver_sign: PresenceCheck
    damage_check: IsDamaged
    delivery_date: Optional[str] = Field(
        default=None,
        description='Printed "Delivery date" text exactly as visible, else null.',
    )
    receiver_name: Optional[str] = Field(
        default=None,
        description="Printed or clearly labeled receiver name, else null.",
    )
    deps_shortage: Optional[str] = Field(
        default=None,
        description='Handwritten remark about "Shortage" including count if visible, else null.',
    )
    deps_damage: Optional[str] = Field(
        default=None,
        description='Handwritten remark about "Damage" including count if visible, else null.',
    )
    weight_dimensions: Optional[str] = Field(
        default=None,
        description='Printed "Weight" and/or "Dimensions" lines as visible, else null.',
    )
    handwritten_text: Optional[str]= Field(
        default=None,
        description='Handwritten text as read by the model.',
    )


def _tag_bool(value: bool) -> str:
    """RediSearch TAG fields work best with string categorical tokens."""
    return "true" if value else "false"


def _image_data_url(path: Path) -> tuple[str, str]:
    mime, _ = mimetypes.guess_type(path.name)
    if not mime:
        mime = "image/jpeg"
    b64 = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return mime, f"data:{mime};base64,{b64}"


def _analyze_image(client: OpenAI, image_path: Path, shipping_id: str) -> PodVisionResult:
    _, url = _image_data_url(image_path)
    instructions = (
        "You inspect logistics Proof-of-Delivery (POD) receipt photos. "
        f"The expected shipping id for this file is: {shipping_id!r}. "
        "Compare any printed AWB / tracking number to this shipping id. "
        "If unsure, lower confidence and prefer false for booleans. "
        "The consignee stamps may be of different shapes and sizes like round, rectangle, without border, etc."
        "Transcribe visible printed fields; for handwriting, transcribe only what you can read. Highlight when there is a missing, short or dammage message written in hadwriting"
        "Ensure to highlight if you determine any of the following 'short', 'shortage' 'missing' or 'damage' words in handwritten remarks in the summary."
    )
    completion = client.chat.completions.parse(
        model=VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instructions},
                    {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                ],
            }
        ],
        response_format=PodVisionResult,
    )
    parsed = completion.choices[0].message.parsed
    if parsed is None:
        raise RuntimeError("OpenAI returned no parsed structured output.")
    return parsed


def _embedding_text(shipping_id: str, vision: PodVisionResult) -> str:
    """Text used for semantic vectorization (OpenAI embeddings are text-only)."""
    parts = [
        f"shipping_id:{shipping_id}",
        f"summary:{vision.summary}",
        f"printed_awb:{vision.awb_check.printed_awb}",
        f"awb_matches:{vision.awb_check.matches_shipping_id}",
        f"blur:{vision.blur_check.is_blur} conf:{vision.blur_check.confidence}",
        f"torn:{vision.torn_check.is_torn} conf:{vision.torn_check.confidence}",
        f"consignee_stamp:{vision.consignee_stamp.present} conf:{vision.consignee_stamp.confidence}",
        f"receiver_sign:{vision.receiver_sign.present} conf:{vision.receiver_sign.confidence}",
        f"delivery_date:{vision.delivery_date}",
        f"receiver_name:{vision.receiver_name}",
        f"shortage:{vision.deps_shortage}",
        f"damage:{vision.deps_damage}",
        f"weight_dimensions:{vision.weight_dimensions}",
        f"handwritten_text:{vision.handwritten_text}",
        f"damaged:{vision.damage_check.is_damaged}",

    ]
    return "\n".join(parts)


def _embed(client: OpenAI, text: str) -> list[float]:
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    vec = resp.data[0].embedding
    if len(vec) != EMBEDDING_DIM:
        raise RuntimeError(
            f"Embedding length {len(vec)} does not match EMBEDDING_DIM={EMBEDDING_DIM}. "
            "Adjust OPENAI_EMBEDDING_DIM or the embedding model settings."
        )
    return vec

def _push_to_redis_stream(data, streamname) :
    
    try :
        messageid = r.xadd(streamname, {'data': json.dumps(data)})
        print(f"Message inserted to stream '{streamname}' with message id : {messageid}")
        return messageid
    except Exception as e:
        print (f"Error occured : {e}")
        return None
    
def _vision_to_redis_doc(shipping_id: str, vision: PodVisionResult, embedding: list[float]) -> dict[str, Any]:
    """Document stored at JSON root for key Pod:{shipping_id}."""
    data =  {
        "shipping_id": shipping_id,
        "summary": vision.summary,
        "embedding": embedding,
        "awb_check": {
            "printed_awb": vision.awb_check.printed_awb,
            "matches_shipping_id": _tag_bool(vision.awb_check.matches_shipping_id),
        },
        "blur_check": {
            "is_blur": _tag_bool(vision.blur_check.is_blur),
            "confidence": vision.blur_check.confidence,
        },
        "torn_check": {
            "is_torn": _tag_bool(vision.torn_check.is_torn),
            "confidence": vision.torn_check.confidence,
        },
        "consignee_stamp": {
            "present": _tag_bool(vision.consignee_stamp.present),
            "confidence": vision.consignee_stamp.confidence,
        },
        "receiver_sign": {
            "present": _tag_bool(vision.receiver_sign.present),
            "confidence": vision.receiver_sign.confidence,
        },
        "delivery_date": vision.delivery_date,
        "receiver_name": vision.receiver_name,
        "deps_shortage": vision.deps_shortage,
        "deps_damage": vision.deps_damage,
        "weight_dimensions": vision.weight_dimensions,
        "handwritten_text" : vision.handwritten_text,
        "damaged":vision.damage_check.is_damaged,
    }

    if(vision.awb_check.matches_shipping_id and (not vision.blur_check.is_blur) and (not vision.torn_check.is_torn )
       and vision.consignee_stamp.present and vision.receiver_sign.present) :
        ''' Stream the data for further processing '''
        _push_to_redis_stream( data, SUCCESS_POD_STREAM)
    else :
        _push_to_redis_stream( data, FAILURE_POD_STREAM)

    return data

def ensure_search_index(r: redis.Redis, index_name: str, *, recreate: bool) -> None:
    """Create a RediSearch JSON index over Pod:* documents."""
    ft = r.ft(index_name)
    exists = False
    try:
        ft.info()
        exists = True
    except redis.exceptions.ResponseError as e:
        exists = False
        #if "Unknown index name" not in str(e):
        #    raise

    if exists and recreate:
        ft.dropindex(delete_documents=True)
        exists = False

    if exists:
        return

    schema = (
        TextField("$.summary", as_name="summary"),
        TextField("$.delivery_date", as_name="delivery_date"),
        TextField("$.receiver_name", as_name="receiver_name"),
        TextField("$.deps_shortage", as_name="deps_shortage"),
        TextField("$.deps_damage", as_name="deps_damage"),
        TextField("$.weight_dimensions", as_name="weight_dimensions"),
        TextField("$.awb_check.printed_awb", as_name="awb_printed"),
        TagField("$.awb_check.matches_shipping_id", as_name="awb_match"),
        TagField("$.blur_check.is_blur", as_name="blur"),
        TagField("$.damaged", as_name="damaged"),
        NumericField("$.blur_check.confidence", as_name="blur_confidence"),
        TagField("$.torn_check.is_torn", as_name="torn"),
        NumericField("$.torn_check.confidence", as_name="torn_confidence"),
        TagField("$.consignee_stamp.present", as_name="consignee_stamp"),
        NumericField("$.consignee_stamp.confidence", as_name="consignee_stamp_confidence"),
        TagField("$.receiver_sign.present", as_name="receiver_sign"),
        NumericField("$.receiver_sign.confidence", as_name="receiver_sign_confidence"),
        VectorField(
            "$.embedding",
            "HNSW",
            {
                "TYPE": "FLOAT32",
                "DIM": EMBEDDING_DIM,
                "DISTANCE_METRIC": "COSINE",
            },
            as_name="embedding_vector",
        ),
    )

    definition = IndexDefinition(prefix=["Pod:"], index_type=IndexType.JSON)
    ft.create_index(schema, definition=definition)


def iter_pod_images(pod_dir: Path) -> list[tuple[str, Path]]:
    """Return (shipping_id, path) pairs for supported image extensions."""
    exts = {".jpeg", ".jpg", ".jpe", ".png", ".webp"}
    out: list[tuple[str, Path]] = []
    if not pod_dir.is_dir():
        return out
    for p in sorted(pod_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in exts:
            continue
        shipping_id = p.stem
        # strip optional _N suffix from duplicate downloads? User asked filename = shippingid;
        # duplicates were saved as id_1.jpeg — keep full stem as key to match file naming.
        out.append((shipping_id, p))
    return out


def process_one(
    *,
    client: OpenAI,
    r: redis.Redis,
    shipping_id: str,
    image_path: Path,
    dry_run: bool,
) -> str:
    vision = _analyze_image(client, image_path, shipping_id)
    embed_text = _embedding_text(shipping_id, vision)
    vector = _embed(client, embed_text)
    doc = _vision_to_redis_doc(shipping_id, vision, vector)
    key = f"Pod:{shipping_id}"

    if dry_run:
        preview = {k: v for k, v in doc.items() if k != "embedding"}
        preview["embedding_len"] = len(vector)
        print(f"[dry-run] {key} -> {json.dumps(preview, ensure_ascii=False)[:2000]}")
        return key

    r.json().set(key, "$", doc)
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description="POD image analysis -> Redis JSON + search index")
    parser.add_argument("--pod-dir", type=Path, default=DEFAULT_POD_DIR)
    parser.add_argument("--index-name", type=str, default=DEFAULT_INDEX_NAME)
    parser.add_argument("--recreate-index", action="store_true", help="Drop and recreate the search index")
    parser.add_argument("--skip-index", action="store_true", help="Do not create/update the RediSearch index")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="Process at most N images (0 = all)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("Error: set OPENAI_API_KEY in your environment.", file=sys.stderr)
        return 1

    pod_dir: Path = args.pod_dir
    pairs = iter_pod_images(pod_dir)
    if args.limit and args.limit > 0:
        pairs = pairs[: args.limit]

    if not pairs:
        print(f"No images found under {pod_dir.resolve()}", file=sys.stderr)
        return 1

    client = OpenAI(api_key=api_key)
    

    try:
        r.ping()
    except redis.exceptions.RedisError as e:
        print(f"Error: cannot connect to Redis ({REDIS_URL}): {e}", file=sys.stderr)
        return 1

    if not args.skip_index and not args.dry_run:
        try:
            ensure_search_index(r, args.index_name, recreate=True)
        except redis.exceptions.ResponseError as e:
            print (e)
            print("\n")
            print(
                "Error: failed to create search index. "
                "You need Redis Stack with RedisJSON + RediSearch modules enabled.\n"
                f"Details: {e}",
                file=sys.stderr,
            )
            return 1

    ok = 0
    failed = 0
    for shipping_id, path in pairs:
        try:
            key = process_one(
                client=client,
                r=r,
                shipping_id=shipping_id,
                image_path=path,
                dry_run=args.dry_run,
            )
            print(f"OK {path.name} -> {key}")
            ok += 1
        except Exception as e:  # noqa: BLE001 - CLI tool: show all failures
            print(f"FAIL {path.name}: {e}", file=sys.stderr)
            failed += 1

    print(f"Done. success={ok} failed={failed}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())


#pip install openai redis pydantic
# python3 pod_openai_redis.py
# @consignee_stamp:{true}
#@summary:*short*
#@summary:*damage*
# to seek to PoD
#Pod:91696002253
#@damaged:{false}
