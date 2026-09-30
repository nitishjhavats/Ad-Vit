"""Product and company identity, in one place.

Every surface that shows a name - the API metadata, report headers, PDF
footers, the web apps when they exist - reads from here. Branding scattered
across templates is branding that drifts, and a report footer showing a stale
company name is the kind of detail a client notices in a meeting.

The lock-up is deliberate:

    ad-vit
    by Broadmate Global          <- smaller, secondary
    broadmate.org                <- linked wherever the medium allows

The product name leads because that is what the user is looking at. The company
sits underneath it, quieter, because attribution is not the message.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Brand:
    product_name: str = "ad-vit"
    company_name: str = "Broadmate Global"
    website_url: str = "https://broadmate.org"
    website_label: str = "broadmate.org"
    tagline: str = "Agentic Meta advertising, closed on business truth."

    @property
    def byline(self) -> str:
        """The secondary line, rendered smaller than the product name."""
        return f"by {self.company_name}"

    @property
    def full_name(self) -> str:
        return f"{self.product_name} by {self.company_name}"

    def html(self, *, heading_level: int = 1) -> str:
        """Lock-up for an HTML surface, with the site as a real link."""
        h = max(1, min(6, heading_level))
        return (
            f'<div class="brand-lockup">'
            f'<h{h} class="brand-product">{self.product_name}</h{h}>'
            f'<p class="brand-byline">by {self.company_name}</p>'
            f'<p class="brand-site">'
            f'<a href="{self.website_url}" target="_blank" rel="noopener noreferrer">'
            f"{self.website_label}</a></p>"
            f"</div>"
        )

    def markdown(self) -> str:
        """Lock-up for a Markdown surface - report headers, exports."""
        return (
            f"# {self.product_name}\n"
            f"<sub>by {self.company_name} · "
            f"[{self.website_label}]({self.website_url})</sub>\n"
        )

    def plain(self) -> str:
        """Lock-up for a terminal or plain-text surface."""
        return (
            f"{self.product_name}\n"
            f"  by {self.company_name}\n"
            f"  {self.website_url}"
        )

    def footer(self) -> str:
        """One-line footer for a PDF or an email."""
        return f"{self.product_name} · by {self.company_name} · {self.website_url}"

    def api_description(self) -> str:
        return (
            f"{self.tagline}\n\n"
            f"by {self.company_name} — {self.website_url}\n\n"
            "Decision, execution, learning and compliance layer above Meta. "
            "Not a competing console."
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "product_name": self.product_name,
            "company_name": self.company_name,
            "byline": self.byline,
            "website_url": self.website_url,
            "website_label": self.website_label,
            "tagline": self.tagline,
        }


BRAND = Brand()

# CSS for the lock-up. Kept beside the markup it styles so the two cannot drift
# apart, and sized so the by-line reads as secondary without becoming
# illegible on a mid-range Android screen (PRD 16.5 targets WCAG AA).
BRAND_CSS = """
.brand-lockup { line-height: 1.25; }
.brand-lockup .brand-product {
  margin: 0;
  font-size: 1.5rem;
  font-weight: 650;
  letter-spacing: -0.01em;
}
.brand-lockup .brand-byline {
  margin: 0.15rem 0 0;
  font-size: 0.8125rem;
  font-weight: 450;
  opacity: 0.72;
}
.brand-lockup .brand-site { margin: 0.1rem 0 0; font-size: 0.8125rem; }
.brand-lockup .brand-site a { color: inherit; text-decoration: underline; }
.brand-lockup .brand-site a:hover { text-decoration-thickness: 2px; }
""".strip()
