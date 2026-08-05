"""Category iconography and colour tones.

The visual system is categorical, not photographic. Fifteen icons and fifteen
tones cover all 4,810 schemes, which is the only approach that scales: stock
photography at this volume is noise a citizen has to look past, and myScheme
itself carries no scheme-specific imagery to reuse.

Tones are referenced by name and resolved to CSS custom properties in
style.css, so light and dark themes swap in one place. Every tone is chosen to
hold a >= 4.5:1 contrast ratio against its own tinted background in both
themes — the icon chip is decorative, but the label sitting next to it is not.
"""
from __future__ import annotations

# category label -> sprite symbol id
CATEGORY_ICON: dict[str, str] = {
    "Social welfare & Empowerment": "hands",
    "Education & Learning": "book",
    "Agriculture, Rural & Environment": "sprout",
    "Business & Entrepreneurship": "briefcase",
    "Women and Child": "family",
    "Skills & Employment": "tools",
    "Banking, Financial Services and Insurance": "rupee",
    "Health & Wellness": "heart",
    "Sports & Culture": "trophy",
    "Housing & Shelter": "home",
    "Science, IT & Communications": "chip",
    "Transport & Infrastructure": "bus",
    "Travel & Tourism": "compass",
    "Utility & Sanitation": "droplet",
    "Public Safety, Law & Justice": "scales",
}

# category label -> tone token (see --tone-* in style.css)
CATEGORY_TONE: dict[str, str] = {
    "Social welfare & Empowerment": "violet",
    "Education & Learning": "indigo",
    "Agriculture, Rural & Environment": "green",
    "Business & Entrepreneurship": "amber",
    "Women and Child": "rose",
    "Skills & Employment": "orange",
    "Banking, Financial Services and Insurance": "teal",
    "Health & Wellness": "red",
    "Sports & Culture": "fuchsia",
    "Housing & Shelter": "brown",
    "Science, IT & Communications": "cyan",
    "Transport & Infrastructure": "blue",
    "Travel & Tourism": "sky",
    "Utility & Sanitation": "lime",
    "Public Safety, Law & Justice": "slate",
}
