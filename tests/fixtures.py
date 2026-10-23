"""Feed fixtures shared by the tests and scripts/demo_offline.py.

Generates RSS 2.0, RSS 1.0/RDF and Atom files with fresh dates, including the
FCA's non-standard date format, plus a feeds.yaml pointing at them.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

NOW = datetime.now(timezone.utc)


def rfc822(hours_ago: float) -> str:
    return format_datetime(NOW - timedelta(hours=hours_ago))


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_fixtures(d: Path) -> dict[str, Path]:
    files = {}
    files["fed"] = d / "fed.xml"
    files["fed"].write_text(f"""﻿<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><title>FRB: Press Release - All Releases</title><link>https://www.federalreserve.gov</link><description>x</description>
<item><title>Federal Reserve issues FOMC statement holding the federal funds rate target range</title>
<link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20261014a.htm?utm_source=rss</link>
<description>The Federal Open Market Committee decided to maintain the target range for the federal funds rate at 3.75 to 4 percent. Inflation remains somewhat elevated at 2.9% and the Committee will continue to assess incoming data.</description>
<pubDate>{rfc822(5)}</pubDate></item>
<item><title>Minutes of the Board's discount rate meetings</title><link>https://www.federalreserve.gov/minutes1.htm</link>
<description>Minutes of discount rate meetings.</description><pubDate>{rfc822(30)}</pubDate></item>
</channel></rss>""", encoding="utf-8")

    files["cfpb"] = d / "cfpb.xml"
    files["cfpb"].write_text(f"""<?xml version="1.0"?><rss version="2.0"><channel><title>CFPB</title>
<item><title>Fed holds federal funds rate target range steady as inflation stays elevated</title>
<link>https://example.gov/cfpb/fed-holds-rate</link>
<description>Consumers with variable-rate credit card balances should not expect relief. The average credit card APR is 21.5%.</description>
<pubDate>{rfc822(3)}</pubDate></item>
</channel></rss>""", encoding="utf-8")

    files["nerdwallet"] = d / "nerdwallet.xml"
    files["nerdwallet"].write_text(f"""<?xml version="1.0"?><rss version="2.0"><channel><title>NerdWallet</title>
<item><title>Select Extended Car Warranty 2026 Review: No Exclusionary Coverage</title><link>https://www.nerdwallet.com/a/review1</link>
<description>Our review.</description><pubDate>{rfc822(2)}</pubDate></item>
<item><title>Delta One Upgrades Its Food and Amenities</title><link>https://www.nerdwallet.com/a/delta</link>
<description>Airline perks and lounge upgrade news for cardholders.</description><pubDate>{rfc822(4)}</pubDate></item>
</channel></rss>""", encoding="utf-8")

    files["boc"] = d / "boc.xml"
    files["boc"].write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="https://www.bankofcanada.ca"><title>Press Releases - Bank of Canada</title></channel>
<item rdf:about="https://www.bankofcanada.ca/2026/10/policy-rate/"><title>Bank of Canada maintains the policy rate at 2¼%</title>
<link>https://www.bankofcanada.ca/2026/10/policy-rate/</link>
<description>The Bank of Canada today held its target for the overnight rate at 2¼%, with the Bank Rate at 2½% and the deposit rate at 2.20%.</description>
<dc:date>{iso(8)}</dc:date></item>
<item rdf:about="https://www.bankofcanada.ca/2026/09/new-20-note/"><title>Bank of Canada unveils new vertical $20 bank note</title>
<link>https://www.bankofcanada.ca/2026/09/new-20-note/</link><description>New note.</description><dc:date>{iso(10)}</dc:date></item>
</rdf:RDF>""", encoding="utf-8")

    files["hmrc"] = d / "hmrc.atom"
    files["hmrc"].write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>HM Revenue &amp; Customs - Activity on GOV.UK</title><id>tag:gov.uk</id><updated>{iso(1)}</updated>
<entry><id>tag:gov.uk,2005:1</id><updated>{iso(6)}</updated>
<link rel="alternate" type="text/html" href="https://www.gov.uk/government/news/self-assessment-penalty"/>
<title>Self Assessment customers reminded of 31 January deadline as late filing penalty stays at £100</title>
<summary>HMRC reminded Self Assessment customers that the online filing deadline is 31 January and that a £100 late filing penalty applies.</summary></entry>
</feed>""", encoding="utf-8")

    files["fca"] = d / "fca.xml"
    fca_date = (NOW - timedelta(hours=7)).strftime("%A, %B %d, %Y - %H:%M")
    files["fca"].write_text(f"""<?xml version="1.0"?><rss version="2.0"><channel><title>Financial Conduct Authority (FCA)</title>
<item><title>Motor finance legal challenge to be heard in February 2027</title><link>https://www.fca.org.uk/news/motor-finance</link>
<description>The FCA said a legal challenge to its motor finance redress scheme will be heard in February 2027, which may delay compensation payments to car finance customers.</description>
<pubDate>{fca_date}</pubDate></item>
</channel></rss>""", encoding="utf-8")
    return files


def feeds_yaml(files: dict[str, Path]) -> str:
    rows = [
        ("us-fed-press", "Federal Reserve Board", "US", "fed", "official", "public_domain"),
        ("us-cfpb-newsroom", "CFPB", "US", "cfpb", "official", "public_domain"),
        ("us-nerdwallet", "NerdWallet", "US", "nerdwallet", "publisher", "link_only"),
        ("ca-boc-press", "Bank of Canada", "CA", "boc", "official", "crown_copyright_ca"),
        ("uk-hmrc", "HMRC", "UK", "hmrc", "official", "ogl"),
        ("uk-fca-news", "Financial Conduct Authority", "UK", "fca", "official", "ogl"),
    ]
    out = ["sources:"]
    for sid, name, c, key, kind, reuse in rows:
        out += [f"  - id: {sid}", f"    name: {name}", f"    country: {c}", f"    url: file://{files[key]}",
                f"    kind: {kind}", f"    reuse: {reuse}", "    full_text: false"]
    return "\n".join(out) + "\n"

