import re

from pydantic import BaseModel, Field

MAX_MESSAGE_CHARS = 2000

INJECTION_PATTERNS = [
    r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions?|rules?|polic(y|ies)|prompts?)\b",
    r"\bsystem prompt\b",
    r"\b(developer|admin|god) mode\b",
    r"\bjailbreak",
    r"\byou are now\b",
    r"\b(act|pretend) (as|to be)\b",
    r"</?(system|assistant|instructions?)>",
]


def check_input(message: str) -> str | None:
    if len(message) > MAX_MESSAGE_CHARS:
        return f"Message is longer than {MAX_MESSAGE_CHARS} characters"
    for pattern in INJECTION_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
            return "Possible prompt injection"
    return None


PII_PATTERNS = {
    "[card number]": r"\b(?:\d[ -]?){13,16}\b",
    "[phone number]": r"(?<!\d)(?:\+91[ -]?)?[6-9]\d{9}(?!\d)",
    "[email]": r"\b[\w.+-]+@[\w-]+\.[\w.]+\b",
}


def redact_pii(text: str, allowed_email: str = "") -> str:
    for label, pattern in PII_PATTERNS.items():

        def mask(match: re.Match[str], label: str = label) -> str:
            return match[0] if match[0] == allowed_email else label

        text = re.sub(pattern, mask, text)
    return text


class ReplyCheck(BaseModel):
    """Structured output of the reply check."""

    grounded: bool = Field(description="True if every action the reply claims really happened")
    problem: str = Field(description="If not grounded: which claim is unsupported, else empty")
    corrected_reply: str = Field(
        description="If not grounded: the reply rewritten without the unsupported claims"
    )


REPLY_CHECK_PROMPT = """You check a customer-support reply before it is sent.
You get the ticket context, the tool calls the team actually made (with their results), and the
draft reply. The reply is NOT grounded if it claims an action or outcome that the tool calls
don't show, e.g. "your refund is processed" without a successful issue_refund, or "I've flagged
it as a priority" when nothing did that. Facts the customer already knew from the conversation
are fine. If it is not grounded, rewrite it: keep the tone and everything true, drop or correct
the unsupported claims."""
