"""A robots.txt matcher that actually honours wildcards.

Python's `urllib.robotparser` compares paths with `startswith`, so a rule
written with a wildcard matches nothing at all:

    Disallow: /vyhledat*      ->  /vyhledat?offerType=PRODEJ  ALLOWED
    Disallow: /moje-bezrealitky/*  ->  /moje-bezrealitky/x     ALLOWED

Nothing real begins with the literal string "/vyhledat*", so the rule is a
no-op and everything it was meant to forbid sails through. That is most of
the rules these portals actually write: bezrealitky states all four of its
prohibitions with wildcards, and Reality.iDNES.cz states nearly all of its
that way too.

In practice this project was not fetching any of the forbidden paths - it
walks a sitemap and reads listing pages - so nothing improper happened. But
a project that spends a whole document (docs/podminky.md) arguing about what
it is entitled to fetch cannot have a compliance check that quietly ignores
most of the rules it is checking. The check has to mean what it says, or the
argument is worthless.

This implements the matching rules of RFC 9309, which is what the portals
are writing against and what every major crawler implements:

  - `*` matches any sequence of characters, `$` anchors the end of the path;
  - the most specific rule wins, measured by the length of the pattern, and
    Allow beats Disallow when the two are equally specific;
  - a group naming our user-agent wins over the `*` group entirely, rather
    than adding to it;
  - an empty `Disallow:` means "nothing is disallowed".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str
    regex: re.Pattern
    length: int


def _compile(pattern: str) -> re.Pattern:
    """A robots.txt path pattern as a regex anchored at the start.

    Everything is escaped except the two characters robots.txt gives meaning
    to, so a path containing regex metacharacters - "/hledani/*%2C" is a real
    rule on sreality - cannot turn into a pattern that matches the wrong
    thing or fails to compile.
    """
    anchored_end = pattern.endswith("$")
    body = pattern[:-1] if anchored_end else pattern
    regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
    return re.compile("^" + regex + ("$" if anchored_end else ""))


class Rules:
    """The rules that apply to one user-agent on one host."""

    def __init__(self, rules: Optional[list] = None, has_group: bool = False):
        self._rules = rules or []
        self.has_group = has_group

    def allows(self, path: str) -> bool:
        """RFC 9309 precedence: longest matching pattern wins, Allow on ties.

        No matching rule means allowed - robots.txt is a list of
        prohibitions, and silence is permission.
        """
        best: Optional[Rule] = None
        for rule in self._rules:
            if not rule.regex.match(path):
                continue
            if best is None or rule.length > best.length or (
                rule.length == best.length and rule.allow and not best.allow
            ):
                best = rule
        return True if best is None else best.allow


def parse(text: str, user_agent: str) -> Rules:
    """The rules for `user_agent`, preferring a group that names it.

    Agent matching is the substring rule every crawler uses: a group for
    "Googlebot" applies to "Googlebot/2.1 (+http://...)". Ours is matched on
    the product token before the slash, so the descriptive User-Agent this
    project sends still finds a group written for its name.
    """
    token = user_agent.split("/")[0].strip().lower()

    groups: dict[str, list] = {}
    current: list = []
    starting_group = True

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()

        if field == "user-agent":
            if not starting_group:
                current = []
                starting_group = True
            groups.setdefault(value.lower(), current)
            # Several User-agent lines in a row share one set of rules.
            groups[value.lower()] = current
            continue

        if field in ("allow", "disallow"):
            starting_group = False
            if field == "disallow" and value == "":
                # "Disallow:" with nothing after it disallows nothing.
                continue
            if not value:
                continue
            current.append(
                Rule(allow=(field == "allow"), pattern=value,
                     regex=_compile(value), length=len(value))
            )

    # A group naming us replaces the wildcard group rather than adding to it.
    for name, rules in groups.items():
        if name and name != "*" and (name in token or token in name):
            return Rules(rules, has_group=True)
    if "*" in groups:
        return Rules(groups["*"], has_group=True)
    return Rules([], has_group=False)


def can_fetch(text: str, user_agent: str, url: str) -> bool:
    """Whether `url` may be fetched under the given robots.txt text."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parse(text, user_agent).allows(path)
