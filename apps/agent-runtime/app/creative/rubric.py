"""What a video ad is scored against, and why each thing matters here.

The rubric is data, versioned, so a rating is comparable to the rubric it was
scored against and not silently to a newer one. Every criterion carries the
reason it is on the list, and the reason is specific to the market this product
serves - Indian D2C, sold largely on cash-on-delivery, with the return-to-origin
rate as the number that decides whether an account makes money. A rubric that
scored "production quality" would be a rubric for a different business.

Two kinds of criterion, and the model is only asked about one of them:

  * MEASURED - duration, aspect ratio, whether the first frame carries text -
    are computed from the file. Asking a model to estimate what ffmpeg can
    read is a way to get a confident wrong number.

  * JUDGED - whether the hook lands, whether the offer is clear - are what the
    `creative_analysis` tier is for, and each is scored with a REASON the owner
    can disagree with. A score with nothing behind it is not feedback.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

RUBRIC_VERSION = "2026-09-14.1"


class Kind(str, Enum):
    MEASURED = "measured"
    JUDGED = "judged"


@dataclass(frozen=True, slots=True)
class Criterion:
    key: str
    label: str
    kind: Kind
    # Weight in the overall score. Weights sum to 100 across the JUDGED
    # criteria; measured ones gate rather than score (below).
    weight: int
    why: str
    # What the model is asked to look for. Empty for measured criteria.
    ask: str = ""


CRITERIA: tuple[Criterion, ...] = (
    # -- measured ----------------------------------------------------------
    Criterion(
        key="vertical",
        label="Vertical (9:16)",
        kind=Kind.MEASURED,
        weight=0,
        why=(
            "Reels and Stories are where Indian D2C spend actually delivers, and "
            "they are full-screen vertical. A landscape video is letterboxed into a "
            "third of the screen and reads as an ad from somewhere else."
        ),
    ),
    Criterion(
        key="length",
        label="Length fits the objective",
        kind=Kind.MEASURED,
        weight=0,
        why=(
            "Under 15 seconds for awareness, 15 to 30 for a considered COD "
            "purchase where the buyer needs the reason and the price. Past 45 the "
            "completion rate falls off a cliff on mobile data."
        ),
    ),
    # -- judged ------------------------------------------------------------
    Criterion(
        key="hook",
        label="Hook in the first 3 seconds",
        kind=Kind.JUDGED,
        weight=25,
        why=(
            "The scroll decision is made in under three seconds. Nothing after the "
            "hook is seen by anyone the hook lost, so every other criterion is "
            "conditional on this one."
        ),
        ask=(
            "Looking only at the first three seconds of frames: is there a "
            "pattern interrupt - a face, a problem shown, a bold claim, movement, "
            "on-screen text - that would stop a thumb? Or does it open on a logo, "
            "a product on a plain background, or a slow establishing shot?"
        ),
    ),
    Criterion(
        key="problem_first",
        label="Problem before product",
        kind=Kind.JUDGED,
        weight=15,
        why=(
            "A COD buyer is buying relief from something, not a bottle. Copy that "
            "opens on the product asks the viewer to already care; copy that opens "
            "on the problem earns the care. This is also where Schedule J and the "
            "DMR Act bite hardest, so the problem has to be shown without a "
            "prohibited claim."
        ),
        ask=(
            "Does the ad name or show the problem the viewer has before it shows "
            "the product? Or is it product-first?"
        ),
    ),
    Criterion(
        key="sound_off",
        label="Works with the sound off",
        kind=Kind.JUDGED,
        weight=15,
        why=(
            "Most feed video is watched muted, in public, on a phone. If the "
            "message lives only in the voiceover, most viewers never get it. "
            "Captions or on-screen text carrying the key claim and the offer are "
            "not a nicety."
        ),
        ask=(
            "From the frames alone, with no audio: can you tell what is being "
            "sold, what it does, and what to do next? Is there readable on-screen "
            "text or captioning?"
        ),
    ),
    Criterion(
        key="offer_clarity",
        label="The offer is unmistakable",
        kind=Kind.JUDGED,
        weight=15,
        why=(
            "Price, what you get, and the deal - shown, not implied. Ambiguity at "
            "this point is what produces the order that gets cancelled on the "
            "confirmation call or refused at the door. RTO is a creative problem "
            "before it is a logistics one."
        ),
        ask=(
            "Is the price or offer visible? Is it clear what the buyer receives? "
            "Would a first-time viewer know what they were ordering?"
        ),
    ),
    Criterion(
        key="trust",
        label="Trust signals for a first order",
        kind=Kind.JUDGED,
        weight=10,
        why=(
            "The COD buyer is trusting a brand they have never touched. Real "
            "people, real usage, a visible brand name, an AYUSH licence number "
            "where it applies, a WhatsApp number - each one lowers the odds the "
            "parcel is refused. Stock footage and a faceless voiceover raise them."
        ),
        ask=(
            "Are there trust signals: a real person using the product, a visible "
            "brand or licence, a phone number or WhatsApp, reviews, packaging? Or "
            "does it read as stock footage?"
        ),
    ),
    Criterion(
        key="cta",
        label="One clear call to action",
        kind=Kind.JUDGED,
        weight=10,
        why=(
            "The destination decides the whole funnel - WhatsApp, a call, a lead "
            "form, a landing page - and the ad has to say which. A video that ends "
            "without telling the viewer what to do has spent its budget on "
            "awareness the account did not choose to buy."
        ),
        ask=(
            "Does the ad end with one clear instruction - order now, message on "
            "WhatsApp, call, fill the form? Is the CTA on screen, not only spoken?"
        ),
    ),
    Criterion(
        key="native_feel",
        label="Looks like content, not an ad",
        kind=Kind.JUDGED,
        weight=10,
        why=(
            "Polished, studio-lit, colour-graded video is skipped as an ad. "
            "Phone-shot, slightly rough, a person talking to camera is watched as "
            "content. This is the single most reliable pattern in Indian D2C "
            "creative and it runs against most brands' instincts."
        ),
        ask=(
            "Does this look like something a person posted, or like a commercial? "
            "Consider lighting, framing, whether a real person addresses the "
            "camera, and whether the brand is pushed or discovered."
        ),
    ),
)

JUDGED = tuple(c for c in CRITERIA if c.kind is Kind.JUDGED)
MEASURED = tuple(c for c in CRITERIA if c.kind is Kind.MEASURED)

assert sum(c.weight for c in JUDGED) == 100, "judged weights must sum to 100"


# ---------------------------------------------------------------------------
# The measured gates
# ---------------------------------------------------------------------------

# Anything wider than this is treated as square-or-landscape.
VERTICAL_MAX_RATIO = 0.60          # width / height; 9:16 is 0.5625
# Length bands, in seconds. Outside the band is a flag, not a fail - a 50-second
# testimonial can work; it should just know it is fighting the format.
LENGTH_BANDS = {
    "awareness": (4.0, 15.0),
    "consideration": (12.0, 30.0),
    "conversion": (10.0, 45.0),
}


def measure(*, width: int | None, height: int | None, duration_s: float | None,
            objective: str = "conversion") -> list[dict]:
    """The measured criteria, from file metadata. No model."""
    out: list[dict] = []

    if width and height:
        ratio = width / height
        vertical = ratio <= VERTICAL_MAX_RATIO
        out.append({
            "key": "vertical",
            "passed": vertical,
            "observed": f"{width}x{height} ({ratio:.2f})",
            "note": None if vertical else (
                "not vertical; it will be letterboxed in Reels and Stories, which is "
                "where the spend delivers"
            ),
        })
    else:
        out.append({"key": "vertical", "passed": None, "observed": None,
                    "note": "dimensions could not be read from the file"})

    if duration_s is not None:
        low, high = LENGTH_BANDS.get(objective, LENGTH_BANDS["conversion"])
        in_band = low <= duration_s <= high
        out.append({
            "key": "length",
            "passed": in_band,
            "observed": f"{duration_s:.1f}s",
            "note": None if in_band else (
                f"{duration_s:.0f}s is outside the {low:.0f}-{high:.0f}s band for a "
                f"{objective} objective"
            ),
        })
    else:
        out.append({"key": "length", "passed": None, "observed": None,
                    "note": "duration could not be read from the file"})

    return out
