"""CV auto-summary with Claude: score + short summary against the role's requirements."""
import json
import logging
import re

import requests
from flask import current_app

log = logging.getLogger(__name__)

PROMPT = """You are screening a job application for {company}.

ROLE: {title}
WHAT WE ARE LOOKING FOR (recruiter's knowledge base for this role):
{requirements}

CANDIDATE
Name: {name}
Location: {location}
Years of experience in this field (self-declared): {years}
LinkedIn: {linkedin}
Cover note: {cover_note}

CV TEXT (extracted, may be imperfect):
\"\"\"
{cv_text}
\"\"\"

Write a screening summary for the hiring team. Be concrete and factual; only use what is in the CV.
Return ONLY a JSON object with these keys:
- "score": integer 0-100, fit against the requirements above (100 = perfect match)
- "recommendation": one of "strong yes", "yes", "maybe", "no"
- "headline": one line, e.g. "Head of Growth, 8y DTC, Meta/Google at scale — Paris"
- "summary": 4-7 bullet points as a single string, each bullet on its own line starting with "- "
- "matches": list of the must-have / nice-to-have requirements the candidate clearly meets
- "gaps": list of requirements not met or not evidenced
- "flags": list of anything odd (gaps in timeline, inconsistency with declared experience, missing basics), or []
"""


def summarize(applicant):
    """Returns dict(score, recommendation, headline, summary, matches, gaps, flags) or None."""
    cfg = current_app.config
    if not cfg["SUMMARY_ENABLED"] or not cfg["ANTHROPIC_API_KEY"]:
        return None
    role = applicant.role
    prompt = PROMPT.format(
        company=cfg["COMPANY_NAME"],
        title=role.title,
        requirements=role.requirements or "(no requirements written yet — judge general quality and seniority)",
        name=applicant.full_name,
        location=applicant.location or "-",
        years=applicant.years_experience if applicant.years_experience is not None else "-",
        linkedin=applicant.linkedin_url or "-",
        cover_note=applicant.cover_note or "-",
        cv_text=(applicant.cv_text or "(no text could be extracted from the CV)")[:40_000],
    )
    # Do not send temperature/top_p/top_k: Claude 4.7+ (including opus-5) returns 400 if they are set.
    r = requests.post(
        f"{cfg['ANTHROPIC_API_BASE']}/v1/messages",
        headers={"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["SUMMARY_MODEL"], "max_tokens": 4096,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:500]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") != "thinking")
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    data["score"] = max(0, min(100, int(data.get("score", 0))))
    return data


def format_for_task(applicant, data):
    """Plain-text block used both as ai_summary and as the ClickUp comment body."""
    lines = [
        f"CV auto-summary — {applicant.full_name} · {applicant.role.title}",
        f"Score {data['score']}/100 · Recommendation: {data.get('recommendation', '-')}",
        data.get("headline", ""),
        "",
        data.get("summary", "").strip(),
    ]
    if data.get("matches"):
        lines += ["", "Meets: " + "; ".join(data["matches"])]
    if data.get("gaps"):
        lines += ["Gaps: " + "; ".join(data["gaps"])]
    if data.get("flags"):
        lines += ["Flags: " + "; ".join(data["flags"])]
    lines += ["", f"Declared experience: {applicant.years_experience} years"
              if applicant.years_experience is not None else ""]
    return "\n".join(l for l in lines if l is not None).strip()


EVAL_PROMPT = """You are grading a candidate's written technical test for {company}.

ROLE: {title}

TEST QUESTIONS (as sent to the candidate):
\"\"\"
{questions}
\"\"\"

ANSWER KEY (private — what a strong answer contains; award partial credit for partially correct answers):
\"\"\"
{answer_key}
\"\"\"

CANDIDATE'S ANSWERS:
\"\"\"
{answers}
\"\"\"

Grade strictly against the answer key. Judge substance, not formatting or language (English/French both fine).
Return ONLY a JSON object with:
- "score": integer 0-100 overall
- "verdict": one of "excellent", "good", "borderline", "insufficient"
- "per_question": single string, one line per question, format "- Qn: x/y — one-line justification"
- "strengths": list of strings
- "weaknesses": list of strings
- "flags": list (e.g. likely copy-paste/AI answer with no specifics, contradiction with CV), or []
"""


def evaluate_test(applicant):
    """Grade the candidate's submitted test against the role's private answer key."""
    cfg = current_app.config
    if not cfg["ANTHROPIC_API_KEY"]:
        return None
    role = applicant.role
    prompt = EVAL_PROMPT.format(
        company=cfg["COMPANY_NAME"], title=role.title,
        questions=(role.test_questions or "")[:20_000],
        answer_key=(role.test_answer_key or "(no key provided — grade on correctness and depth)")[:20_000],
        answers=(applicant.test_answers or "")[:40_000],
    )
    r = requests.post(
        f"{cfg['ANTHROPIC_API_BASE']}/v1/messages",
        headers={"x-api-key": cfg["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": cfg["SUMMARY_MODEL"], "max_tokens": 4096,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:500]}")
    text = "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") != "thinking")
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0) if m else text)
    data["score"] = max(0, min(100, int(data.get("score", 0))))
    return data


def format_test_result(applicant, data):
    lines = [
        f"Test result — {applicant.full_name} · {applicant.role.title}",
        f"Test score {data['score']}/100 · {data.get('verdict', '-')}",
        "",
        data.get("per_question", "").strip(),
    ]
    if data.get("strengths"):
        lines += ["", "Strengths: " + "; ".join(data["strengths"])]
    if data.get("weaknesses"):
        lines += ["Weaknesses: " + "; ".join(data["weaknesses"])]
    if data.get("flags"):
        lines += ["Flags: " + "; ".join(data["flags"])]
    if applicant.score is not None:
        lines += ["", f"(CV screening score was {applicant.score}/100)"]
    return "\n".join(l for l in lines if l is not None).strip()
