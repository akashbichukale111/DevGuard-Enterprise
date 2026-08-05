"""
sentinel.py — the Sentinel, on the catalog-text side.

The agent design gives Sentinel two jobs; this module is the first one: *"Fence and screen
**all** catalog-sourced text for injection."* (Scanning the proposed fix is the
Surgeon-side job and lands with the Surgeon.)

the untrusted-content boundary is the requirement being met: *"all catalog free-text is UNTRUSTED_TEXT,
fenced in prompts, never instruction. **No tool call may be selected on the
basis of catalog free-text.**"*

Design note worth stating plainly, because overclaiming here would be its own
the no-fabrication rule problem: **detection is a signal, not a defence.** The pattern list below
will miss novel phrasings, and it is not what makes DevGuard safe. What makes
DevGuard safe is architectural — the agent that reads this text
(Diagnostician) holds zero tools, so text that successfully manipulates its
reasoning still cannot cause an action. The screen exists to *surface* attempts
as evidence (the injection demonstration's demo beat), and to refuse to let untrusted text stand alone
as justification. `backend/core/ai_agent.py` already ships the fencing primitive
used here; it is reused rather than reimplemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

# Imported from the leaf module, not from ai_agent: the definitions are shared
# (never duplicated), but fencing a catalog string must not require the
# OpenTelemetry and LLM stack that ai_agent pulls in.
from backend.core.untrusted import UNTRUSTED_CONTENT_RULE, fence_untrusted

__all__ = [
    "InjectionRisk", "InjectionFinding", "ScreenedText", "Sentinel",
    "PatchRisk", "PatchFinding", "PatchScan",
    "UNTRUSTED_CONTENT_RULE", "fence_untrusted",
]


class InjectionRisk(str, Enum):
    NONE = "NONE"
    SUSPICIOUS = "SUSPICIOUS"
    """Contains instruction-shaped language. Worth surfacing; not proof."""
    LIKELY = "LIKELY"
    """Contains a recognised injection pattern aimed at an agent."""


#: (name, pattern, risk). Ordered most-specific first.
#:
#: Every pattern here is aimed at *instruction-shaped text in a metadata field*.
#: A dataset description has no legitimate reason to address the reader as an
#: agent, which is what makes these worth flagging at all.
_PATTERNS: tuple[tuple[str, re.Pattern, InjectionRisk], ...] = (
    ("override-previous",
     re.compile(r"(?i)\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b"
                r"(previous|prior|above|earlier|all)\b[^.\n]{0,20}\b"
                r"(instruction|prompt|rule|direction|context)s?\b"),
     InjectionRisk.LIKELY),
    ("role-reassignment",
     re.compile(r"(?i)\b(you are now|act as|from now on,? you|new (system )?prompt|"
                r"system\s*:\s*)"),
     InjectionRisk.LIKELY),
    ("action-directive",
     re.compile(r"(?i)\b(mark|set|tag|classify|approve|certify|flag)\b[^.\n]{0,30}\b"
                r"(this|the)\b[^.\n]{0,30}\b"
                r"(dataset|table|column|asset|model|change|fix)\b"),
     InjectionRisk.LIKELY),
    ("suppress-findings",
     re.compile(r"(?i)\b(do not|don't|never)\b[^.\n]{0,30}\b"
                r"(report|raise|flag|alert|escalate|log)\b"),
     InjectionRisk.LIKELY),
    ("exfiltration",
     re.compile(r"(?i)\b(reveal|print|output|send|post|leak|exfiltrate)\b[^.\n]{0,30}\b"
                r"(token|secret|credential|api[_ ]?key|password|environment)"),
     InjectionRisk.LIKELY),
    ("tool-naming",
     re.compile(r"(?i)\b(call|invoke|run|use)\b[^.\n]{0,20}\b"
                r"(add_tags|update_description|save_document|add_owners|"
                r"add_structured_properties|raiseIncident)\b"),
     InjectionRisk.LIKELY),
    ("imperative-to-agent",
     re.compile(r"(?i)\b(assistant|agent|ai|llm|model|claude|gpt)\b[^.\n]{0,20}[:,]\s*\w"),
     InjectionRisk.SUSPICIOUS),
    ("fence-breakout",
     re.compile(r"<<<\s*(END\s+)?UNTRUSTED|```\s*system|\[/?INST\]|<\|im_(start|end)\|>"),
     InjectionRisk.SUSPICIOUS),
)


@dataclass(frozen=True)
class InjectionFinding:
    pattern: str
    risk: InjectionRisk
    excerpt: str
    field: str


@dataclass(frozen=True)
class ScreenedText:
    """Catalog text, after screening, in the only form agents may consume."""

    field: str
    original: str
    findings: tuple[InjectionFinding, ...]

    @property
    def risk(self) -> InjectionRisk:
        if any(f.risk is InjectionRisk.LIKELY for f in self.findings):
            return InjectionRisk.LIKELY
        if self.findings:
            return InjectionRisk.SUSPICIOUS
        return InjectionRisk.NONE

    @property
    def fenced(self) -> str:
        """The ONLY representation that may enter a prompt.

        There is no accessor that returns the raw string for prompt use. Making
        the unsafe path unavailable is more reliable than documenting that it
        should not be taken.
        """
        return fence_untrusted(self.field, self.original)


class PatchRisk(str, Enum):
    """The autonomy policy's risk classes, as they apply to a proposed change."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class PatchFinding:
    rule: str
    risk: PatchRisk
    detail: str


@dataclass(frozen=True)
class PatchScan:
    """The verdict on a proposed diff (step 12)."""

    files_touched: tuple[str, ...]
    added_lines: int
    removed_lines: int
    findings: tuple[PatchFinding, ...]

    @property
    def risk(self) -> PatchRisk:
        order = [PatchRisk.LOW, PatchRisk.MEDIUM, PatchRisk.HIGH, PatchRisk.CRITICAL]
        return max((f.risk for f in self.findings), key=order.index, default=PatchRisk.LOW)

    @property
    def blocked(self) -> bool:
        """CRITICAL never proceeds, regardless of who would approve it."""
        return self.risk is PatchRisk.CRITICAL


#: Patterns that make a *proposed change* dangerous, as opposed to text that
#: makes a *description* suspicious. Destructive DDL in a fix is the case worth
#: catching: an agent that can propose `drop table` and get it rubber-stamped is
#: a liability no amount of prompt discipline fixes.
_PATCH_RULES: tuple[tuple[str, re.Pattern, PatchRisk, str], ...] = (
    ("destructive-ddl",
     re.compile(r"(?i)^\+.*\b(drop|truncate)\s+(table|schema|database|view)\b", re.M),
     PatchRisk.CRITICAL, "adds destructive DDL"),
    ("data-mutation",
     re.compile(r"(?i)^\+.*\b(delete\s+from|update\s+\w+\s+set|insert\s+into)\b", re.M),
     PatchRisk.CRITICAL, "adds a data mutation"),
    ("grant",
     re.compile(r"(?i)^\+.*\b(grant|revoke|alter\s+role|create\s+user)\b", re.M),
     PatchRisk.CRITICAL, "changes database permissions"),
    ("filter-removed",
     re.compile(r"(?i)^-.*\bwhere\b", re.M),
     PatchRisk.HIGH, "removes a WHERE clause — row counts may change silently"),
    ("secret-literal",
     re.compile(r"(?i)^\+.*\b(password|secret|token|api[_ ]?key)\s*=\s*['\"]\S+", re.M),
     PatchRisk.CRITICAL, "hard-codes a credential"),
    ("config-change",
     re.compile(r"(?i)^\+\+\+ b/.*\.(yml|yaml|toml|cfg|ini|env)$", re.M),
     PatchRisk.MEDIUM, "touches configuration, not just SQL"),
    ("wide-change",
     re.compile(r"^\+\+\+ b/", re.M),
     PatchRisk.LOW, "file touched"),
)


class Sentinel:
    """Screens catalog free-text AND proposed changes. Holds no DataHub tools (the agent design)."""

    NAME = "sentinel"

    def screen(self, field: str, text: str | None) -> ScreenedText:
        text = text or ""
        findings: list[InjectionFinding] = []
        for name, pattern, risk in _PATTERNS:
            for match in pattern.finditer(text):
                start = max(0, match.start() - 30)
                findings.append(InjectionFinding(
                    pattern=name, risk=risk, field=field,
                    excerpt=text[start:match.end() + 30].replace("\n", " ").strip(),
                ))
        return ScreenedText(field=field, original=text, findings=tuple(findings))

    def screen_all(self, fields: Iterable[tuple[str, str | None]]) -> tuple[ScreenedText, ...]:
        return tuple(self.screen(name, value) for name, value in fields)

    def scan_patch(self, diff: str) -> PatchScan:
        """step 12: scan the proposed change and classify its risk.

        Note what is NOT here: any notion of "the agent said it was safe". The
        classification is a property of the diff text, computed the same way
        every time, so a persuasive rationale cannot lower a risk class.
        """
        files = tuple(sorted({line[6:].strip()
                              for line in diff.splitlines()
                              if line.startswith("+++ b/")}))
        added = sum(1 for l in diff.splitlines()
                    if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff.splitlines()
                      if l.startswith("-") and not l.startswith("---"))

        findings: list[PatchFinding] = []
        for name, pattern, risk, detail in _PATCH_RULES:
            if name == "wide-change":
                # One file is routine; several in one automated fix is not.
                if len(files) > 1:
                    findings.append(PatchFinding(
                        rule=name, risk=PatchRisk.MEDIUM,
                        detail=f"{len(files)} files in one proposed change"))
                continue
            for match in pattern.finditer(diff):
                findings.append(PatchFinding(
                    rule=name, risk=risk,
                    detail=f"{detail}: {match.group(0).strip()[:100]}"))

        return PatchScan(files_touched=files, added_lines=added,
                         removed_lines=removed, findings=tuple(findings))

    @staticmethod
    def build_prompt_context(screened: Iterable[ScreenedText]) -> str:
        """Assemble fenced catalog text for a prompt, rule first.

        The security rule is prepended rather than appended because it must be
        read before the untrusted content, not after it.
        """
        blocks = [s.fenced for s in screened]
        return UNTRUSTED_CONTENT_RULE + "\n\n" + "\n\n".join(blocks)
