#!/usr/bin/env python3
"""Génère une grille SVG animée à partir des contributions publiques GitHub."""

from __future__ import annotations

import argparse
import calendar
import html
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path


PALETTE = ("#161b22", "#0e4429", "#006d32", "#26a641", "#39d353")
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
MAX_RESPONSE_BYTES = 1_000_000


@dataclass
class Contribution:
    day: date
    level: int
    count: int = -1


class ContributionParser(HTMLParser):
    """Extrait les cellules et leurs info-bulles du calendrier GitHub."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.contributions_by_id: dict[str, Contribution] = {}
        self._tooltip_target: str | None = None
        self._tooltip_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)

        if tag == "td":
            classes = (attributes.get("class") or "").split()
            cell_id = attributes.get("id")
            raw_day = attributes.get("data-date")

            if "ContributionCalendar-day" in classes and cell_id and raw_day:
                raw_level = attributes.get("data-level") or "0"
                if cell_id in self.contributions_by_id:
                    raise ValueError(f"Identifiant de cellule dupliqué : {cell_id}")
                try:
                    level = int(raw_level)
                except ValueError as error:
                    raise ValueError(
                        f"Niveau de contribution invalide : {raw_level!r}"
                    ) from error
                if level not in range(5):
                    raise ValueError(
                        f"Niveau de contribution hors limites : {level}"
                    )
                self.contributions_by_id[cell_id] = Contribution(
                    day=date.fromisoformat(raw_day),
                    level=level,
                )

        if tag == "tool-tip":
            target = attributes.get("for")
            if target in self.contributions_by_id:
                self._tooltip_target = target
                self._tooltip_parts = []

    def handle_data(self, data: str) -> None:
        if self._tooltip_target is not None:
            self._tooltip_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "tool-tip" or self._tooltip_target is None:
            return

        tooltip = " ".join(self._tooltip_parts).strip()
        match = re.search(r"([\d,]+)\s+contribution", tooltip, re.IGNORECASE)
        if match:
            count = int(match.group(1).replace(",", ""))
        elif re.search(r"\bno contributions?\b", tooltip, re.IGNORECASE):
            count = 0
        else:
            raise ValueError(
                f"Info-bulle GitHub non reconnue : {tooltip!r}"
            )
        self.contributions_by_id[self._tooltip_target].count = count
        self._tooltip_target = None
        self._tooltip_parts = []


def validate_username(username: str) -> str:
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(f"Nom d'utilisateur GitHub invalide : {username!r}")
    return username


def fetch_contributions(username: str, attempts: int = 3) -> str:
    encoded_username = urllib.parse.quote(username, safe="")
    url = f"https://github.com/users/{encoded_username}/contributions"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": "rfielbal-profile-readme/1.0",
        },
    )

    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.headers.get_content_type() != "text/html":
                    raise RuntimeError(
                        "Réponse GitHub inattendue : le contenu n'est pas du HTML"
                    )

                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        declared_length = int(raw_length)
                    except ValueError as error:
                        raise RuntimeError(
                            "Réponse GitHub invalide : taille illisible"
                        ) from error
                    if declared_length > MAX_RESPONSE_BYTES:
                        raise RuntimeError(
                            "Réponse GitHub anormalement volumineuse"
                        )

                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise RuntimeError(
                        "Réponse GitHub anormalement volumineuse"
                    )

                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset)
                except (LookupError, UnicodeDecodeError) as error:
                    raise RuntimeError(
                        "Réponse GitHub impossible à décoder"
                    ) from error
        except (TimeoutError, urllib.error.URLError) as error:
            if attempt == attempts:
                raise RuntimeError(
                    f"Impossible de récupérer les contributions après {attempts} essais"
                ) from error
            time.sleep(2 ** (attempt - 1))

    raise RuntimeError("Échec inattendu lors de la récupération des contributions")


def parse_contributions(source: str) -> list[Contribution]:
    parser = ContributionParser()
    parser.feed(source)
    contributions = sorted(
        parser.contributions_by_id.values(),
        key=lambda contribution: contribution.day,
    )

    if not 365 <= len(contributions) <= 371:
        raise RuntimeError(
            "Calendrier GitHub incomplet : "
            f"{len(contributions)} cellules trouvées, entre 365 et 371 attendues"
        )

    days = [contribution.day for contribution in contributions]
    if len(set(days)) != len(days):
        raise RuntimeError("Le calendrier GitHub contient des dates dupliquées")

    for previous_day, current_day in zip(days, days[1:]):
        if current_day - previous_day != timedelta(days=1):
            raise RuntimeError(
                "Le calendrier GitHub contient une rupture entre "
                f"{previous_day.isoformat()} et {current_day.isoformat()}"
            )

    for contribution in contributions:
        if contribution.count < 0:
            raise RuntimeError(
                "Une cellule GitHub ne possède aucune info-bulle exploitable : "
                f"{contribution.day.isoformat()}"
            )
        if (contribution.level == 0) != (contribution.count == 0):
            raise RuntimeError(
                "Niveau et nombre de contributions incohérents pour "
                f"{contribution.day.isoformat()}"
            )

    return contributions


def month_labels(
    placed: list[tuple[Contribution, int, int]],
) -> list[tuple[str, int]]:
    labels: list[tuple[str, int]] = []
    seen_months: set[tuple[int, int]] = set()

    for contribution, column, row in placed:
        month_key = (contribution.day.year, contribution.day.month)
        if month_key in seen_months:
            continue
        if labels and row != 0:
            continue

        seen_months.add(month_key)
        labels.append((calendar.month_abbr[contribution.day.month], column))

    return labels


def render_svg(contributions: list[Contribution]) -> str:
    first_day = contributions[0].day
    first_sunday = first_day - timedelta(days=(first_day.weekday() + 1) % 7)
    placed: list[tuple[Contribution, int, int]] = []

    for contribution in contributions:
        days_from_start = (contribution.day - first_sunday).days
        column = days_from_start // 7
        row = (contribution.day.weekday() + 1) % 7
        placed.append((contribution, column, row))

    max_column = max(column for _, column, _ in placed)
    labels = "".join(
        f'<text class="lbl" x="{34 + column * 16}" y="16">'
        f"{html.escape(label)}</text>"
        for label, column in month_labels(placed)
    )

    cells: list[str] = []
    for contribution, column, row in placed:
        x = 34 + column * 16
        y = 24 + row * 16
        delay = column * (3.385 / max(max_column, 1)) + row * (0.215 / 6)
        activity_class = "g" if contribution.level > 0 else "e"
        contribution_word = (
            "contribution" if contribution.count == 1 else "contributions"
        )
        title = html.escape(
            f"{contribution.day.isoformat()}: "
            f"{contribution.count} {contribution_word}"
        )
        cells.append(
            f'<rect class="c {activity_class}" x="{x}" y="{y}" '
            f'width="13" height="13" rx="2.5" '
            f'fill="{PALETTE[contribution.level]}" '
            f'style="animation-delay:{delay:.3f}s">'
            f"<title>{title}</title></rect>"
        )

    total = sum(contribution.count for contribution in contributions)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="888" height="158" viewBox="0 0 888 158" font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">
<title>Grille animée des contributions GitHub de Raphaël Coursier</title>
<style>
  text.lbl {{ fill:#7d8590; font-size:13px; font-weight:600; }}
  text.total {{ fill:#e6edf3; font-size:15px; font-weight:700; }}
  .c {{ transform-box:fill-box; transform-origin:center; opacity:0; animation:pop 0.55s ease-out both; }}
  .g {{ animation:pop 0.55s ease-out both, flash 0.70s ease-out both; }}
  @keyframes pop {{
    0% {{ opacity:0; transform:scale(.2); }}
    60% {{ opacity:1; transform:scale(1.1); }}
    100% {{ opacity:1; transform:scale(1); }}
  }}
  @keyframes flash {{
    0%, 45% {{ filter:brightness(2.4); }}
    100% {{ filter:brightness(1); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .c, .g {{ opacity:1 !important; animation:none !important; }}
  }}
</style>
<rect width="888" height="158" rx="12" fill="#0d1117"/>
<rect x="0.5" y="0.5" width="887" height="157" rx="11.5" fill="none" stroke="#30363d"/>
{labels}
<text class="lbl" x="2" y="51">Mon</text>
<text class="lbl" x="2" y="83">Wed</text>
<text class="lbl" x="2" y="115">Fri</text>
{"".join(cells)}
<text class="total" x="34" y="152">{total:,} contributions in the last year</text>
</svg>
"""


def write_if_changed(output: Path, content: str) -> bool:
    if output.exists() and output.read_text(encoding="utf-8") == content:
        return False

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(output)
    return True


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default="rfielbal")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("assets/contrib-heatmap.svg"),
    )
    parser.add_argument(
        "--input-html",
        type=Path,
        help="Utilise une réponse GitHub locale au lieu du réseau.",
    )
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()

    try:
        username = validate_username(arguments.username)
        if arguments.input_html:
            source = arguments.input_html.read_text(encoding="utf-8")
        else:
            source = fetch_contributions(username)
        contributions = parse_contributions(source)
        svg = render_svg(contributions)
        changed = write_if_changed(arguments.output, svg)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1

    total = sum(contribution.count for contribution in contributions)
    status = "actualisé" if changed else "déjà à jour"
    print(
        f"{arguments.output} : {status} · "
        f"{len(contributions)} jours · {total} contributions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
