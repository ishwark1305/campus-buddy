# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import os
import re
import json
from typing import Any, Optional, List, Dict
from pydantic import BaseModel, Field

from google.adk.workflow import Workflow, node, START, Edge
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App
from google import genai
from google.genai import types

from mcp import StdioServerParameters, ClientSession
from mcp.client.stdio import stdio_client
from google.adk.tools import McpToolset

from .config import MODEL_NAME, WEAK_TOPIC_THRESHOLD

mcp_server_params = StdioServerParameters(
    command="uv",
    args=["run", "python", "-m", "app.mcp_server"],
)
mcp_toolset = McpToolset(connection_params=mcp_server_params)


async def call_mcp_tool(name: str, arguments: dict) -> Any:
    """Helper to call an MCP tool using standard stdio client transport."""
    async with stdio_client(mcp_server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
            if hasattr(result, "content") and result.content:
                return result.content[0].text
            return str(result)

# Configure environment: fallback to local dev key if GCP ADC is not available
if not os.environ.get("GEMINI_API_KEY"):
    try:
        import google.auth

        _, project_id = google.auth.default()
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
        os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
        os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
    except Exception:
        pass


import time


def generate_content_with_retry(
    client, model, contents, config=None, max_retries=8, initial_delay=2.0
):
    delay = initial_delay
    fallback_models = [
        "gemini-3-flash-preview",
        "gemini-3.5-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash",
    ]
    current_model = model
    fallback_index = 0
    for attempt in range(max_retries):
        try:
            return client.models.generate_content(
                model=current_model,
                contents=contents,
                config=config,
            )
        except Exception as e:
            err_str = str(e).lower()
            if (
                "429" in err_str
                or "resource_exhausted" in err_str
                or "503" in err_str
                or "unavailable" in err_str
            ):
                # If daily request limit for current model is reached, fall back to the next available model
                is_daily_quota = "daily" in err_str or "perday" in err_str
                if is_daily_quota and fallback_index < len(fallback_models):
                    next_model = fallback_models[fallback_index]
                    fallback_index += 1
                    print(
                        f"⚠️ Daily quota exceeded for {current_model}. Falling back to {next_model}."
                    )
                    current_model = next_model
                    continue

                if attempt == max_retries - 1:
                    raise

                # Parse exact retry delay if specified by Gemini API
                sleep_delay = delay
                try:
                    import re

                    match_delay = re.search(
                        r"['\"]retryDelay['\"]\s*:\s*['\"]([\d\.]+)s['\"]", str(e)
                    )
                    match_retry_in = re.search(r"[Pp]lease retry in ([\d\.]+)s", str(e))
                    if match_delay:
                        sleep_delay = float(match_delay.group(1)) + 2.0
                        print(
                            f"ℹ️ Gemini API requested wait: {match_delay.group(1)}s (applying {sleep_delay}s sleep)"
                        )
                    elif match_retry_in:
                        sleep_delay = float(match_retry_in.group(1)) + 2.0
                        print(
                            f"ℹ️ Gemini API requested wait: {match_retry_in.group(1)}s (applying {sleep_delay}s sleep)"
                        )
                except Exception:
                    pass

                print(
                    f"Temporary API error (429/503) in agent, retrying in {sleep_delay}s..."
                )
                time.sleep(sleep_delay)
                # Keep updating backoff delay for cases where parsing failed
                delay *= 2.0
            else:
                raise


def clean_schema(schema: Any) -> Any:
    """Recursively removes keys that are not supported by the Gemini Developer API from a schema dict."""
    if isinstance(schema, dict):
        forbidden_keys = ["additionalProperties", "$schema", "unevaluatedProperties"]
        cleaned = {}
        for k, v in schema.items():
            if k in forbidden_keys:
                continue
            cleaned[k] = clean_schema(v)
        return cleaned
    elif isinstance(schema, list):
        return [clean_schema(item) for item in schema]
    else:
        return schema


def get_clean_response_schema(model_class: Any) -> dict:
    """Generates the JSON schema for a Pydantic model and strips out Developer-API-forbidden keys."""
    raw_schema = model_class.model_json_schema()
    return clean_schema(raw_schema)


# =====================================================================
# Pydantic Schemas for Session State & Structured Output
# =====================================================================


class SubjectDetail(BaseModel):
    topics: List[str]
    input_type: str  # "topics_only" or "detailed_notes"
    raw_content: str


class QAPair(BaseModel):
    question: str
    answer: str


class MCQOptions(BaseModel):
    A: str
    B: str
    C: str
    D: str


class MCQ(BaseModel):
    question: str
    options: MCQOptions
    correct_option: str  # "A", "B", "C", "D"
    explanation: str


class TopicQuestionBank(BaseModel):
    explanation: str
    mcqs: List[MCQ] = Field(default_factory=list)
    qa_pairs: List[QAPair] = Field(default_factory=list)


class TopicStat(BaseModel):
    topic: str
    attempted: int = 0
    correct: int = 0
    accuracy: float = 0.0
    flagged_ai: bool = False


class CampusBuddyState(BaseModel):
    subjects: Dict[str, SubjectDetail] = Field(
        default_factory=dict
    )  # subject -> SubjectDetail
    question_banks: Dict[str, Dict[str, TopicQuestionBank]] = Field(
        default_factory=dict
    )  # subject -> topic -> TopicQuestionBank
    topic_stats: Dict[str, TopicStat] = Field(default_factory=dict)  # topic -> stats
    weak_topics: List[str] = Field(default_factory=list)
    current_subject: Optional[str] = None
    current_question_index: int = 0
    exam_date: Optional[str] = None
    active_quiz_questions: List[Dict[str, Any]] = Field(default_factory=list)
    qb_confirmed: bool = False
    minutes_per_day: Optional[int] = None
    selected_subjects: List[str] = Field(default_factory=list)
    pending_interrupt: Optional[str] = None
    ai_flagged_count: int = 0
    quiz_active: bool = False
    quiz_history: List[Dict[str, Any]] = Field(default_factory=list)
    audit_log: List[Dict[str, Any]] = Field(default_factory=list)
    security_block_reason: Optional[str] = None
    pending_topic_adjustment: bool = False
    study_timer_active: bool = False
    study_session_topic: Optional[str] = None
    study_session_start_time: Optional[float] = None
    pomodoro_checkpoint_questions: List[Dict[str, Any]] = Field(default_factory=list)
    pomodoro_checkpoint_index: int = 0


# Schemas for structured LLM responses
class IngestOutput(BaseModel):
    subject: str
    topics: List[str]
    input_type: str  # "topics_only" or "detailed_notes"


class TopicBankWithTopic(BaseModel):
    topic: str
    explanation: str
    mcqs: List[MCQ]
    qa_pairs: List[QAPair]


class SubjectQuestionBankOutput(BaseModel):
    topic_banks: List[TopicBankWithTopic]


class GradingResult(BaseModel):
    correct: bool
    explanation: str


class FeedbackClassification(BaseModel):
    confirmed: bool
    revisions_requested: str


class SubjectSelectionOutput(BaseModel):
    selected: List[str]


class QuizMCQ(BaseModel):
    question: str
    options: MCQOptions
    correct_option: str  # "A", "B", "C", "D"
    explanation: str


class QuizQAPair(BaseModel):
    question: str
    answer: str


class TopicQuizQuestions(BaseModel):
    topic: str
    mcqs: List[QuizMCQ]
    qa_pairs: List[QuizQAPair]


class SubjectQuizQuestionsOutput(BaseModel):
    topic_quiz_questions: List[TopicQuizQuestions]


def scrub_pii(text: str) -> tuple[str, bool, list[str]]:
    detected = False
    types_found = []

    # Email addresses
    email_regex = r"\b[\w.-]+@[\w.-]+\.\w{2,}\b"
    if re.search(email_regex, text):
        text = re.sub(email_regex, "[REDACTED]", text)
        detected = True
        types_found.append("email")

    # Phone numbers (Indian mobile)
    phone_regex = r"\b[6-9]\d{9}\b"
    if re.search(phone_regex, text):
        text = re.sub(phone_regex, "[REDACTED]", text)
        detected = True
        types_found.append("phone")

    # Aadhaar-style numbers (12-digit sequences)
    aadhaar_regex = r"\b\d{12}\b"
    if re.search(aadhaar_regex, text):
        text = re.sub(aadhaar_regex, "[REDACTED]", text)
        detected = True
        types_found.append("aadhaar")

    # Student ID patterns (e.g. AIML + digits)
    student_id_regex = r"\bAIML\d+\b"
    if re.search(student_id_regex, text, re.IGNORECASE):
        text = re.sub(student_id_regex, "[REDACTED]", text, flags=re.IGNORECASE)
        detected = True
        types_found.append("student_id")

    return text, detected, types_found


@node
def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    user_text = ""
    if hasattr(node_input, "parts") and node_input.parts:
        user_text = "".join(
            [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
        )
    elif isinstance(node_input, str):
        user_text = node_input

    # 1. PII Scrubbing
    scrubbed_text, pii_detected, pii_types = scrub_pii(user_text)
    if pii_detected:
        print(f"⚠️ WARNING: PII detected and scrubbed: {pii_types}")

    # 2. Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions",
        "ignore all instructions",
        "system prompt",
        "developer mode",
        "jailbreak",
        "act as",
        "you are now",
        "system override",
        "dan mode",
        "forget your",
        "new instructions",
    ]
    injection_detected = False
    triggered_keyword = None
    user_text_lower = user_text.lower()
    for kw in injection_keywords:
        if kw in user_text_lower:
            injection_detected = True
            triggered_keyword = kw
            break

    # 3. Input Length Check
    length_exceeded = len(user_text) > 8000

    # 4. Audit Log
    import datetime

    timestamp = datetime.datetime.utcnow().isoformat() + "Z"

    if injection_detected or length_exceeded:
        action = "BLOCKED"
        severity = "CRITICAL"
    elif pii_detected:
        action = "SCRUBBED"
        severity = "WARNING"
    else:
        action = "PASS"
        severity = "INFO"

    audit_entry = {
        "timestamp": timestamp,
        "input_length": len(user_text),
        "pii_detected": pii_detected,
        "pii_types": pii_types,
        "injection_detected": injection_detected,
        "injection_keyword": triggered_keyword,
        "action": action,
        "severity": severity,
    }

    audit_log = list(ctx.state.get("audit_log", []))
    audit_log.append(audit_entry)
    ctx.state["audit_log"] = audit_log

    # Route decision
    if length_exceeded:
        ctx.state["security_block_reason"] = (
            "Input too long — please paste shorter notes or split into multiple messages."
        )
        return Event(output=user_text, actions=EventActions(route="SECURITY_EVENT"))

    if injection_detected:
        ctx.state["security_block_reason"] = (
            "🔒 That message was blocked by CampusBuddy's security checkpoint. "
            "Please rephrase and avoid including system instructions or "
            "personal identity information in your study notes."
        )
        return Event(output=user_text, actions=EventActions(route="SECURITY_EVENT"))

    # Route to pass or scrubbed, outputs the scrubbed text
    route = "scrubbed" if pii_detected else "pass"
    return Event(output=scrubbed_text, actions=EventActions(route=route))


@node
def security_blocked_node(ctx: Context, node_input: Any) -> Event:
    reason = ctx.state.get("security_block_reason")
    if not reason:
        reason = (
            "🔒 That message was blocked by CampusBuddy's security checkpoint. "
            "Please rephrase and avoid including system instructions or "
            "personal identity information in your study notes."
        )

    # Clean up state
    ctx.state["security_block_reason"] = None

    content = types.Content(role="model", parts=[types.Part.from_text(text=reason)])
    return Event(output=reason, actions=EventActions(), content=content)


@node
def router_node(ctx: Context, node_input: Any) -> Event:
    """Classifies user intent and routes to the appropriate node in the graph."""
    user_text = ""
    if hasattr(node_input, "parts") and node_input.parts:
        user_text = "".join(
            [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
        )
    elif isinstance(node_input, str):
        user_text = node_input

    user_text_lower = user_text.lower().strip()

    # Direct routing for options (a), (b), (c)
    if user_text_lower in ["a", "(a)", "a)"]:
        return Event(output=user_text, actions=EventActions(route="generate"))
    elif user_text_lower in ["b", "(b)", "b)"]:
        return Event(output=user_text, actions=EventActions(route="ingest"))
    elif user_text_lower in ["c", "(c)", "c)"]:
        return Event(output=user_text, actions=EventActions(route="ingest"))

    # Check if study timer is active first
    study_timer_active = ctx.state.get("study_timer_active", False)
    if study_timer_active:
        return Event(output=user_text, actions=EventActions(route="pomodoro"))

    # Bug 2 Fix: Check state.get("quiz_active", False) FIRST before LLM classification
    quiz_active = ctx.state.get("quiz_active", False)
    if quiz_active:
        user_text_stripped = user_text.strip()
        words = user_text_stripped.split()

        is_quiz_command = False

        # 1. Stop/exit/enough/next keywords
        if any(
            k in user_text_lower
            for k in [
                "stop",
                "quit",
                "exit",
                "end",
                "done",
                "finish",
                "enough",
                "next",
            ]
        ):
            is_quiz_command = True

        # 2. Single letters A/B/C/D (MCQ answers)
        if not is_quiz_command:
            is_quiz_command = len(
                user_text_stripped
            ) == 1 and user_text_stripped.upper() in ["A", "B", "C", "D"]

        # 3. Short free-text answer under 30 words that doesn't start with "syllabus", "notes", or a subject name
        if not is_quiz_command and len(words) < 30:
            starts_with_special = False
            first_word_lower = words[0].lower() if words else ""
            if first_word_lower in ["syllabus", "notes"]:
                starts_with_special = True
            else:
                saved_subjects = list(ctx.state.get("subjects", {}).keys())
                for s in saved_subjects:
                    if user_text_lower.startswith(s.lower()):
                        starts_with_special = True
                        break
            if not starts_with_special:
                is_quiz_command = True

        if is_quiz_command:
            return Event(output=user_text, actions=EventActions(route="quiz"))

    has_exit_keyword = any(
        k in user_text_lower
        for k in [
            "new subject",
            "syllabus",
            "notes",
            "plan",
            "schedule",
            "stop",
            "exit",
            "quit",
        ]
    )

    # If there is a pending interrupt in state, route directly to the node that requested it
    pending = ctx.state.get("pending_interrupt")
    if pending and not has_exit_keyword:
        return Event(output=user_text, actions=EventActions(route=pending))

    # Classify intent / answer general queries using LLM
    client = genai.Client()

    # Serialize session state to pass to LLM
    subjects_raw = ctx.state.get("subjects", {})
    subjects_serialized = {}
    for k, v in subjects_raw.items():
        if hasattr(v, "model_dump"):
            subjects_serialized[k] = v.model_dump()
        elif isinstance(v, dict):
            subjects_serialized[k] = v
        else:
            subjects_serialized[k] = str(v)

    state_desc = f"""
    Current Saved Subjects and Topics: {json.dumps(subjects_serialized)}
    Current Subject: {ctx.state.get("current_subject")}
    Question Banks Generated for: {list(ctx.state.get("question_banks", {}).keys())}
    Weak Topics: {ctx.state.get("weak_topics", [])}
    Exam Date: {ctx.state.get("exam_date")}
    """

    prompt = f"""
    You are a helpful study companion router and assistant.
    Analyze the student's message: "{user_text}"
    
    Current Session State:
    {state_desc}
    
    First, decide if the student is asking a general informational question about their state (e.g., what subjects are saved, what topics are there, list the weak topics, when is the exam, etc.).
    If it is a general informational question, reply with the prefix "QUERY: " followed by a direct, friendly answer to their question using the Session State.
    
    Otherwise, classify their intent into exactly one of these categories:
    - 'ingest': Student is pasting study notes, syllabus list, or wants to edit/adjust topics. Do NOT classify requests to generate questions or quizzes here.
    - 'generate': Student wants to generate questions, a quiz, or a question bank (e.g., "generate a question bank", "create questions", "generate a question bank for Physics").
    - 'quiz': Student wants to start a quiz, practice questions, or take a test (e.g., "quiz me", "yes", "let's start", "start quiz").
    - 'plan': Student wants a revision schedule, study plan, or mentions an exam date (e.g., "create a study plan", "schedule my revision").
    - 'remedial': Student wants to generate or review a remedial study guide, help with weak topics, or get a study guide (e.g., "remedial guide", "study guide for weak topics").
    - 'pomodoro': Student wants to start a pomodoro study block, active study session, focus timer, or study session (e.g., "start study session", "pomodoro", "study timer").
    
    If it is one of these intents, reply with exactly one word: 'ingest', 'generate', 'quiz', 'plan', 'remedial', or 'pomodoro'.
    """

    try:
        response = generate_content_with_retry(
            client,
            model=MODEL_NAME,
            contents=prompt,
        )
        result = response.text.strip()
    except Exception:
        result = "ingest"

    if result.startswith("QUERY:"):
        answer = result[len("QUERY:") :].strip()
        content = types.Content(role="model", parts=[types.Part.from_text(text=answer)])
        return Event(
            output=user_text, actions=EventActions(route="answer"), content=content
        )

    intent = result.lower()
    if intent not in ["ingest", "generate", "quiz", "plan", "remedial", "pomodoro"]:
        intent = "ingest"

    return Event(output=user_text, actions=EventActions(route=intent))


@node
def ingest_node(ctx: Context, node_input: Any) -> Event:
    """Processes study notes or syllabus input, extracts topics, classifies input type, and updates state."""
    user_text = ""
    if hasattr(node_input, "parts") and node_input.parts:
        user_text = "".join(
            [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
        )
    elif isinstance(node_input, str):
        user_text = node_input

    user_text = user_text.strip()
    user_text_lower = user_text.lower()

    # Option (b) Adjust topic list first choice
    if user_text_lower in ["b", "(b)", "b)", "adjust", "adjust topic", "adjust the topic list first", "adjust the topic list"]:
        ctx.state["pending_topic_adjustment"] = True
        return Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="🔧 Sure! Please type the updated topics you want to keep, add, or remove (e.g., 'remove CPU Scheduling, add Virtual Memory' or paste the complete list of topics)."
                    )
                ],
            ),
            actions=EventActions(state_delta={"pending_topic_adjustment": True}),
        )

    # Option (c) Do something else choice
    if user_text_lower in ["c", "(c)", "c)", "do something else", "do something else?"]:
        return Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="No problem! What would you like to do instead?\n- Type **'generate question bank'** to build the QB anyway.\n- Type **'quiz me'** to start a quiz.\n- Type **'make a plan'** to build a revision schedule.\n- Or paste new notes to start over."
                    )
                ],
            ),
            actions=EventActions(),
        )

    # Pending topic adjustment execution
    pending_adjustment = ctx.state.get("pending_topic_adjustment", False)
    if pending_adjustment:
        ctx.state["pending_topic_adjustment"] = False
        current_subject = ctx.state.get("current_subject")
        subjects = ctx.state.get("subjects", {})
        if not current_subject and subjects:
            current_subject = list(subjects.keys())[0]

        if current_subject and current_subject in subjects:
            subj_data = subjects[current_subject]
            existing_topics = []
            if isinstance(subj_data, dict):
                existing_topics = subj_data.get("topics", [])
            
            client = genai.Client()
            adjust_prompt = f"""
            The student wants to adjust the topic list for subject "{current_subject}".
            Existing topic list: {existing_topics}
            Student's adjustment request: "{user_text}"
            
            Based on the student's request, produce the new adjusted list of topics.
            Provide your output in JSON format matching this schema:
            {{
                "selected": ["topic1", "topic2", ...]
            }}
            """
            try:
                res = generate_content_with_retry(
                    client,
                    model=MODEL_NAME,
                    contents=adjust_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=get_clean_response_schema(
                            SubjectSelectionOutput
                        ),
                    ),
                )
                data = json.loads(res.text)
                new_topics = data.get("selected", []) or data.get("topics", [])
            except Exception:
                new_topics = [t.strip() for t in user_text.split(",") if t.strip()]

            if not new_topics:
                new_topics = existing_topics

            subjects[current_subject]["topics"] = new_topics
            ctx.state["subjects"] = subjects

            message = (
                f"✅ **Topic List Adjusted for {current_subject}!**\n\n"
                + "**New Topics/Concepts Found:**\n"
                + "\n".join(f"- {t}" for t in new_topics)
                + "\n\nWould you like me to:\n"
                + "(a) Generate a question bank\n"
                + "(b) Adjust the topic list first\n"
                + "(c) Do something else?"
            )
            content = types.Content(role="model", parts=[types.Part.from_text(text=message)])
            return Event(
                output=current_subject,
                actions=EventActions(state_delta={"subjects": subjects, "pending_topic_adjustment": False}),
                content=content,
            )

    if not user_text:
        return Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Please paste your study notes or syllabus list to start!"
                    )
                ],
            ),
            actions=EventActions(),
        )

    # Bug 3 Validation: prevent conversational/short inputs from being ingested as study topics
    words = user_text.split()
    is_valid = True
    if len(words) < 10:
        saved_subjects = list(ctx.state.get("subjects", {}).keys())
        indicators = [
            "syllabus",
            "notes",
            "module",
            "topic",
            "chapter",
            "exam",
            "course",
            "subject",
        ]
        for s in saved_subjects:
            indicators.append(s.lower())

        user_text_lower = user_text.lower()
        has_indicator = any(ind in user_text_lower for ind in indicators)
        if not has_indicator:
            is_valid = False

    if not is_valid:
        return Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="Please paste actual study notes or a syllabus list to start!"
                    )
                ],
            ),
            actions=EventActions(),
        )

    client = genai.Client()
    prompt = f"""
    Analyze the following study notes or syllabus list.
    Classify the input as either:
    - "topics_only": just a list of topic/module names with little to no actual explanation.
    - "detailed_notes": actual content with explanations, definitions, or worked examples, not just topic names.
    
    Extract the main Subject name, a clean list of topics/concepts covered, and the classified input type.
    
    Input:
    {user_text}
    
    Provide your output in JSON matching this schema:
    {{
        "subject": "Name of the subject",
        "topics": ["topic 1", "topic 2", ...],
        "input_type": "topics_only" or "detailed_notes"
    }}
    """

    try:
        response = generate_content_with_retry(
            client,
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=get_clean_response_schema(IngestOutput),
            ),
        )
        data = json.loads(response.text)
        subject = data.get("subject", "General Subject").strip()
        topics = data.get("topics", [])
        input_type = data.get("input_type", "topics_only")
        if input_type not in ["topics_only", "detailed_notes"]:
            input_type = "topics_only"
    except Exception as e:
        import traceback

        print("INGEST_NODE ERROR DETECTED:", repr(e))
        traceback.print_exc()
        subject = "General Subject"
        topics = ["Topic 1"]
        input_type = "topics_only"

    subjects = ctx.state.get("subjects", {})
    subjects[subject] = {
        "topics": topics,
        "input_type": input_type,
        "raw_content": user_text,
    }

    new_state = {
        "subjects": subjects,
        "current_subject": subject,
        "current_question_index": 0,
        "qb_confirmed": False,
    }

    message = (
        f"📚 **Subject Extracted:** {subject} ({'Detailed Notes' if input_type == 'detailed_notes' else 'Topics Only'})\n\n**Topics/Concepts Found:**\n"
        + "\n".join(f"- {t}" for t in topics)
        + "\n\nWould you like me to:\n"
        + "(a) Generate a question bank\n"
        + "(b) Adjust the topic list first\n"
        + "(c) Do something else?"
    )
    content = types.Content(role="model", parts=[types.Part.from_text(text=message)])

    return Event(
        output=subject, actions=EventActions(state_delta=new_state), content=content
    )


def format_question_bank(qb_dict: dict) -> str:
    lines = []
    lines.append("**Question Bank**")
    for topic, bank in qb_dict.items():
        if isinstance(bank, dict):
            explanation = bank.get("explanation", "")
            mcqs = bank.get("mcqs", [])
            qa_pairs = bank.get("qa_pairs", [])
        else:
            explanation = bank.explanation
            mcqs = bank.mcqs
            qa_pairs = bank.qa_pairs

        lines.append(f"\n### Topic: {topic}")
        lines.append(f"> **Concept Explanation:** {explanation}\n")

        if mcqs:
            lines.append("#### Multiple Choice Questions (MCQs):")
            for idx, mcq in enumerate(mcqs):
                if isinstance(mcq, dict):
                    q = mcq.get("question", "")
                    opts = mcq.get("options", {})
                    correct = mcq.get("correct_option", "")
                    exp = mcq.get("explanation", "")
                else:
                    q = mcq.question
                    opts = mcq.options
                    correct = mcq.correct_option
                    exp = mcq.explanation
                lines.append(f"**MCQ {idx + 1}. {q}**")
                opts_dict = {}
                if hasattr(opts, "model_dump"):
                    opts_dict = opts.model_dump()
                elif isinstance(opts, dict):
                    opts_dict = opts
                for key in sorted(opts_dict.keys()):
                    lines.append(f"  - **{key}:** {opts_dict[key]}")
                lines.append(f"  *Correct Option: {correct}*")
                lines.append(f"  *Explanation:* {exp}\n")

        if qa_pairs:
            lines.append("#### Q&A Pairs:")
            for idx, qa in enumerate(qa_pairs):
                if isinstance(qa, dict):
                    q = qa.get("question", "")
                    a = qa.get("answer", "")
                else:
                    q = qa.question
                    a = qa.answer
                lines.append(f"**Q{idx + 1}. {q}**")
                lines.append(f"**A{idx + 1}.** {a}\n")
    return "\n".join(lines)


def format_persistent_question_bank(subject: str, qb_dict: dict) -> str:
    lines = []
    lines.append(f"YOUR QUESTION BANK — {subject.upper()}\n")

    for topic, bank in qb_dict.items():
        if isinstance(bank, dict):
            explanation = bank.get("explanation", "")
            mcqs = bank.get("mcqs", [])
            qa_pairs = bank.get("qa_pairs", [])
        else:
            explanation = bank.explanation
            mcqs = bank.mcqs
            qa_pairs = bank.qa_pairs

        lines.append(f"TOPIC: {topic}")
        lines.append(f"Concept: {explanation}\n")

        if mcqs:
            lines.append("MCQs:")
            for idx, mcq in enumerate(mcqs):
                if isinstance(mcq, dict):
                    q = mcq.get("question", "")
                    opts = mcq.get("options", {})
                    correct = mcq.get("correct_option", "")
                else:
                    q = mcq.question
                    opts = mcq.options
                    correct = mcq.correct_option

                lines.append(f"{idx + 1}. {q}")
                opt_lines = []
                opts_dict = {}
                if hasattr(opts, "model_dump"):
                    opts_dict = opts.model_dump()
                elif isinstance(opts, dict):
                    opts_dict = opts
                elif isinstance(opts, list):
                    for i, val in enumerate(opts):
                        key = chr(65 + i)
                        opt_lines.append(f"   {key}) {val}")

                if opts_dict:
                    for key in ["A", "B", "C", "D"]:
                        val = opts_dict.get(key) or opts_dict.get(key.lower(), "")
                        if val:
                            opt_lines.append(f"   {key}) {val}")
                    if not opt_lines:
                        for k, v in sorted(opts_dict.items()):
                            opt_lines.append(f"   {k}) {v}")

                lines.extend(opt_lines)
                lines.append(f"   Answer: {correct}\n")

        if qa_pairs:
            lines.append("Q&A:")
            for idx, qa in enumerate(qa_pairs):
                if isinstance(qa, dict):
                    q = qa.get("question", "")
                    a = qa.get("answer", "")
                else:
                    q = qa.question
                    a = qa.answer
                lines.append(f"{idx + 1}. Q: {q}")
                lines.append(f"   A: {a}\n")

    return "\n".join(lines)


@node(rerun_on_resume=True)
async def question_generator_node(ctx: Context, node_input: Any):
    """Generates a genuine Question Bank (explanations & Q&A pairs) with feedback loop."""
    ctx.state["pending_interrupt"] = None
    subject = ctx.state.get("current_subject")
    if not subject:
        if isinstance(node_input, str) and node_input:
            subject = node_input
        else:
            subjects = ctx.state.get("subjects", {})
            if subjects:
                subject = list(subjects.keys())[0]
            else:
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text="Please provide study notes or a syllabus first to start!"
                            )
                        ],
                    ),
                    actions=EventActions(),
                )
                return

    client = genai.Client()
    interrupt_id = f"qb_feedback_{subject}"

    # 1. Process feedback if resuming or if we have node_input and QB is already present
    q_banks = ctx.state.get("question_banks", {})
    has_qb = subject in q_banks and q_banks[subject]

    feedback_text = None
    if ctx.resume_inputs and interrupt_id in ctx.resume_inputs:
        feedback_text = str(ctx.resume_inputs[interrupt_id]).strip()
    elif has_qb and node_input:
        input_str = ""
        if isinstance(node_input, str):
            input_str = node_input.strip()
        elif hasattr(node_input, "parts") and node_input.parts:
            input_str = "".join(
                [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
            ).strip()

        if input_str and input_str.lower() not in ["a", "generate", "generate question bank", "generate a question bank"]:
            feedback_text = input_str

    if feedback_text:

        # Classify feedback
        classify_prompt = f"""
        The student has provided feedback on the generated Question Bank.
        Feedback: "{feedback_text}"
        
        Determine if the student has confirmed the Question Bank (e.g. "looks good", "yes", "proceed", "no changes", "looks great", "yes start", etc.) OR if they are requesting revisions/additions/fixes (e.g. "change X", "add Y", "explain Z differently").
        
        Provide your output in JSON format matching this schema:
        {{
            "confirmed": true/false,
            "revisions_requested": "Description of the revision request, or empty if confirmed"
        }}
        """
        try:
            class_res = generate_content_with_retry(
                client,
                model=MODEL_NAME,
                contents=classify_prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=get_clean_response_schema(FeedbackClassification),
                ),
            )
            class_data = json.loads(class_res.text)
            confirmed = class_data.get("confirmed", False)
            revisions = class_data.get("revisions_requested", "")
        except Exception:
            # Fallback to simple keyword check
            confirmed = any(
                k in feedback_text.lower()
                for k in [
                    "looks good",
                    "yes",
                    "proceed",
                    "looks great",
                    "confirm",
                    "ok",
                    "fine",
                    "no changes",
                ]
            )
            revisions = feedback_text if not confirmed else ""

        if confirmed:
            ctx.state["qb_confirmed"] = True
            qb_dict = ctx.state.get("question_banks", {}).get(subject, {})
            qb_persistent = format_persistent_question_bank(subject, qb_dict)
            msg = (
                f"{qb_persistent}\n\n"
                "**Question Bank confirmed!**\n\n"
                "What would you like to do now:\n"
                "- Start a quiz on this subject (type 'start quiz')\n"
                "- Add another subject (paste new notes/syllabus)\n"
                "- Go straight to building a revision plan (type 'make a plan')"
            )
            yield Event(
                output=subject,
                actions=EventActions(
                    state_delta={"qb_confirmed": True, "pending_interrupt": None}
                ),
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text=msg)]
                ),
            )
            return
        else:
            # Revise Question Bank based on student requests
            current_qb = ctx.state.get("question_banks", {}).get(subject, {})
            current_qb_serialized = {}
            for k, v in current_qb.items():
                if hasattr(v, "model_dump"):
                    current_qb_serialized[k] = v.model_dump()
                else:
                    current_qb_serialized[k] = v

            revise_prompt = f"""
            You are an expert study companion. You have a Question Bank for the subject "{subject}".
            The student has requested revisions/changes: "{revisions}"
            
            Here is the current Question Bank:
            {json.dumps(current_qb_serialized)}
            
            CRITICAL: You must preserve and include ALL existing topics from the current Question Bank. Do not omit any topics. Only modify the specific topics or concepts that the student requested to add, change, or explain in more detail. The output JSON must contain the complete set of all topics.
            
            CRITICAL FORMATTING REQUIREMENT: Do NOT include any emojis, decorative separator lines (like '━━━━━━━━━━━━━━━━━━━━━━━━' or similar), or box-drawing characters in the explanations, questions, options, answers, or any other generated text. Keep the generated text as clean, plain markdown text.
            
            Revise the Question Bank according to the student's request. Keep the same structure:
            For EACH topic:
            - "explanation": concept explanation (2-4 sentences, second-year B.Tech level)
            - "mcqs": list of 2 MCQs (each containing "question", "options" dict with keys "A", "B", "C", "D" and option texts, "correct_option" which must be "A", "B", "C", or "D", and "explanation" explaining why that option is correct)
            - "qa_pairs": list of 2 Q&A pairs (each containing "question" and "answer")
            
            Provide your output in JSON format matching this schema:
            {{
                "topic_banks": [
                    {{
                        "topic": "topic name",
                        "explanation": "concept explanation",
                        "mcqs": [
                            {{
                                "question": "question text",
                                "options": {{"A": "option A text", "B": "option B text", "C": "option C text", "D": "option D text"}},
                                "correct_option": "correct option key (A, B, C, or D)",
                                "explanation": "explanation of why it is correct"
                            }},
                            ...
                        ],
                        "qa_pairs": [
                            {{
                                "question": "question text",
                                "answer": "answer text"
                            }},
                            ...
                        ]
                    }},
                    ...
                ]
            }}
            """
            try:
                rev_res = generate_content_with_retry(
                    client,
                    model=MODEL_NAME,
                    contents=revise_prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=get_clean_response_schema(
                            SubjectQuestionBankOutput
                        ),
                    ),
                )
                data = json.loads(rev_res.text)
                new_qb = {}
                for tb in data.get("topic_banks", []):
                    topic = tb.get("topic")
                    new_qb[topic] = {
                        "explanation": tb.get("explanation"),
                        "mcqs": tb.get("mcqs", []),
                        "qa_pairs": tb.get("qa_pairs", []),
                    }
                # Save to state
                ctx.state["question_banks"][subject] = new_qb
            except Exception as e:
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text=f"Failed to revise Question Bank: {e!s}. Let's try again."
                            )
                        ],
                    ),
                    actions=EventActions(),
                )
                pass

    # 2. Build initial QB if not already present
    q_banks = ctx.state.get("question_banks", {})
    if subject not in q_banks or not q_banks[subject]:
        sub_detail = ctx.state.get("subjects", {}).get(subject)
        if isinstance(sub_detail, dict):
            topics = sub_detail.get("topics", [])
            input_type = sub_detail.get("input_type", "topics_only")
            raw_content = sub_detail.get("raw_content", "")
        elif sub_detail:
            topics = sub_detail.topics
            input_type = sub_detail.input_type
            raw_content = sub_detail.raw_content
        else:
            topics = ["General Concepts"]
            input_type = "topics_only"
            raw_content = ""

        prompt = f"""
        You are an expert B.Tech study companion and examiner.
        Build a comprehensive, high-quality Question Bank document for the subject "{subject}" covering these topics:
        {", ".join(topics)}
        
        The input type for this syllabus was "{input_type}".
        """
        if input_type == "detailed_notes":
            prompt += f"""
            Here is the original detailed note content:
            {raw_content}
            
            CRITICAL: Ground all concept explanations, MCQs, and Q&A pairs in this actual pasted note content. Do not invent details not present in the notes.
            """
        else:
            prompt += """
            Generate the concept explanations, MCQs, and Q&A pairs using general, accurate subject knowledge. Keep explanations simple, accurate, and easy to understand.
            """

        prompt += """
        CRITICAL FORMATTING REQUIREMENT: Do NOT include any emojis, decorative separator lines (like '━━━━━━━━━━━━━━━━━━━━━━━━' or similar), or box-drawing characters in the explanations, questions, options, answers, or any other generated text. Keep the generated text as clean, plain markdown text.

        For EACH topic:
        1. Generate a clear, simple, exam-friendly CONCEPT EXPLANATION (2-4 sentences, written so a second-year B.Tech student can actually understand it — not just a dictionary definition).
        2. Generate 2 Multiple Choice Questions (MCQs):
           - "question": the MCQ question text
           - "options": a dictionary with keys "A", "B", "C", "D" and their respective option texts
           - "correct_option": the correct option key ("A", "B", "C", or "D")
           - "explanation": a brief explanation of WHY that option is correct
        3. Generate 2 Q&A pairs:
           - "question": the Q&A question text
           - "answer": the complete, well-explained model answer (explain the reasoning clearly, like a good exam answer would)
        
        Provide your output in JSON format matching this schema:
        {
            "topic_banks": [
                {
                    "topic": "topic name",
                    "explanation": "2-4 sentence concept explanation",
                    "mcqs": [
                        {
                            "question": "MCQ question text",
                            "options": {"A": "option A text", "B": "option B text", "C": "option C text", "D": "option D text"},
                            "correct_option": "correct option key (A, B, C, or D)",
                            "explanation": "why correct"
                        },
                        ...
                    ],
                    "qa_pairs": [
                        {
                            "question": "question text",
                            "answer": "complete, well-explained model answer"
                        },
                        ...
                    ]
                },
                ...
            ]
        }
        """
        try:
            response = generate_content_with_retry(
                client,
                model=MODEL_NAME,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=get_clean_response_schema(
                        SubjectQuestionBankOutput
                    ),
                ),
            )
            data = json.loads(response.text)
            qb_data = {}
            for tb in data.get("topic_banks", []):
                topic = tb.get("topic")
                qb_data[topic] = {
                    "explanation": tb.get("explanation"),
                    "mcqs": tb.get("mcqs", []),
                    "qa_pairs": tb.get("qa_pairs", []),
                }
            q_banks[subject] = qb_data
            ctx.state["question_banks"] = q_banks
        except Exception as e:
            print("GENERATE ERROR:", repr(e))
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text="Failed to generate Question Bank. Please retry."
                        )
                    ],
                ),
                actions=EventActions(),
            )
            return

    # 3. Present current QB and ask for feedback
    qb_dict = ctx.state["question_banks"][subject]
    qb_formatted = format_question_bank(qb_dict)
    message = (
        f"**Question Bank Generated!**\n\n{qb_formatted}\n\n"
        "**Does this Question Bank look complete/accurate?** (Let me know if anything needs to be added/fixed, or type 'confirm'):"
    )
    ctx.state["pending_interrupt"] = "generate"
    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=message)],
        ),
        actions=EventActions(),
    )
    yield RequestInput(
        interrupt_id=interrupt_id,
        message="",
    )


def check_authenticity(question: str, answer: str) -> str:
    """Checks if a student's answer is genuine or AI-generated using Gemini."""
    client = genai.Client()
    prompt = f"""
    You are checking if a student's answer to a study quiz question looks
    like it was genuinely written by the student themselves or copy-pasted
    from an AI assistant. Signs of AI-generated text include: very long
    perfectly structured paragraphs, formal academic tone inconsistent with
    a casual quiz answer, presence of phrases like 'In conclusion',
    'Furthermore', 'It is worth noting', 'To summarize', suspiciously
    complete coverage of every possible angle, or answers that are
    significantly longer and more polished than the question warrants.

    Question: {question}
    Student's answer: {answer}

    Reply with ONLY one word: AUTHENTIC or AI_GENERATED.
    """
    try:
        response = generate_content_with_retry(
            client,
            model=MODEL_NAME,
            contents=prompt,
        )
        res = response.text.strip().upper()
        res = re.sub(r"[^A-Z_]", "", res)
        if "AI_GENERATED" in res:
            return "AI_GENERATED"
        return "AUTHENTIC"
    except Exception:
        return "AUTHENTIC"


@node(rerun_on_resume=True)
async def quiz_node(ctx: Context, node_input: Any):
    """Administers a quiz one question at a time using questions from the confirmed Question Bank(s)."""
    was_quiz_active = ctx.state.get("quiz_active", False)
    # Check if we are resuming from post_quiz_action
    if ctx.resume_inputs and "post_quiz_action" in ctx.resume_inputs:
        post_action = str(ctx.resume_inputs["post_quiz_action"]).strip()
        post_action_lower = post_action.lower()
        if any(k in post_action_lower for k in ["plan", "schedule", "make a plan", "make plan"]):
            ctx.state["selected_subjects"] = []
            yield Event(output=post_action, actions=EventActions(route="done"))
            return
        elif any(k in post_action_lower for k in ["quiz again", "quiz me again", "retake", "quiz"]):
            ctx.state["selected_subjects"] = []
            ctx.state["current_question_index"] = 0
            ctx.state["quiz_active"] = False
            was_quiz_active = False
        else:
            yield Event(output=post_action, actions=EventActions(route="route_to_router"))
            return

    ctx.state["pending_interrupt"] = None
    subjects = ctx.state.get("subjects", {})
    if not was_quiz_active:
        ctx.state["selected_subjects"] = []
        ctx.state["current_question_index"] = 0
    if not subjects:
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="No active subjects. Please paste study notes or syllabus first."
                    )
                ],
            ),
            actions=EventActions(),
        )
        return

    # Check and handle selected_subjects selection
    selected = ctx.state.get("selected_subjects", [])
    if not selected:
        if len(subjects) == 1:
            selected = list(subjects.keys())
            ctx.state["selected_subjects"] = selected
            ctx.state["current_subject"] = selected[0]
        else:
            select_id = "select_quiz_subjects"
            is_resuming = ctx.resume_inputs and select_id in ctx.resume_inputs
            sel_input = None
            if is_resuming:
                sel_input = str(ctx.resume_inputs[select_id]).strip()
            elif node_input:
                input_str = ""
                if isinstance(node_input, str):
                    input_str = node_input.strip()
                elif hasattr(node_input, "parts") and node_input.parts:
                    input_str = "".join(
                        [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
                    ).strip()
                
                input_lower = input_str.lower()
                contains_subject = any(s.lower() in input_lower for s in subjects.keys())
                contains_all = any(k in input_lower for k in ["both", "combined", "all"])
                if contains_subject or contains_all:
                    sel_input = input_str

            if sel_input:
                client = genai.Client()
                parse_prompt = f"""
                Analyze the student's choice of subjects to study.
                Available subjects in state: {list(subjects.keys())}
                Student's choice: "{sel_input}"
                
                Identify which of the available subjects they want to include. If they chose "both", "combined", "all", or named multiple, include all matching ones.
                Provide your output in JSON format matching this schema:
                {{
                    "selected": ["subject1", "subject2", ...]
                }}
                """
                try:
                    res = generate_content_with_retry(
                        client,
                        model=MODEL_NAME,
                        contents=parse_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=get_clean_response_schema(
                                SubjectSelectionOutput
                            ),
                        ),
                    )
                    data = json.loads(res.text)
                    selected = data.get("selected", [])
                except Exception:
                    selected = []
                    lower_input = sel_input.lower()
                    if any(k in lower_input for k in ["both", "combined", "all"]):
                        selected = list(subjects.keys())
                    else:
                        for s in subjects.keys():
                            if s.lower() in lower_input:
                                selected.append(s)

                if not selected:
                    selected = list(subjects.keys())

                ctx.state["selected_subjects"] = selected
                ctx.state["current_subject"] = selected[0]
            else:
                subj_list_str = (
                    " and ".join(subjects.keys())
                    if len(subjects) == 2
                    else ", ".join(subjects.keys())
                )
                ctx.state["pending_interrupt"] = "quiz"
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text=f"📚 You've got {subj_list_str} loaded — quiz you on which one, or both combined?"
                            )
                        ],
                    ),
                    actions=EventActions(),
                )
                yield RequestInput(
                    interrupt_id=select_id,
                    message="",
                )
                return

    # Safety check: all selected subjects must have confirmed QBs
    q_banks = ctx.state.get("question_banks", {})
    for s in selected:
        if s not in q_banks or not q_banks[s]:
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text=f"Please generate and confirm the Question Bank for {s} before starting the quiz!"
                        )
                    ],
                ),
                actions=EventActions(),
            )
            ctx.state["selected_subjects"] = []
            return

    active_qs = ctx.state.get("active_quiz_questions", [])
    if not active_qs:
        ctx.state["quiz_history"] = []
        mcqs_pool = []
        qa_pool = []
        client = genai.Client()

        # Check if we should filter to weak topics only
        filter_weak = False
        user_input_str = ""
        if isinstance(node_input, str):
            user_input_str = node_input.lower()
        elif hasattr(node_input, "parts") and node_input.parts:
            user_input_str = "".join(
                [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
            ).lower()
        elif hasattr(node_input, "text") and node_input.text:
            user_input_str = node_input.text.lower()

        if "weak" in user_input_str:
            filter_weak = True

        weak_topics_list = ctx.state.get("weak_topics", [])

        # Serialize confirmed QBs for selected subjects to pass as context
        qb_context = {}
        for s in selected:
            s_qb = q_banks.get(s, {})
            s_qb_serialized = {}
            for topic, bank in s_qb.items():
                if filter_weak and weak_topics_list and topic not in weak_topics_list:
                    continue
                if hasattr(bank, "model_dump"):
                    s_qb_serialized[topic] = bank.model_dump()
                else:
                    s_qb_serialized[topic] = bank
            qb_context[s] = s_qb_serialized

        for s in selected:
            s_qb_data = qb_context.get(s, {})
            if not s_qb_data:
                continue

            prompt = f"""
            You are an expert tutor and examiner. 
            You are generating a set of FRESH practice quiz questions for the subject "{s}" based on the confirmed Study Question Bank.
            
            Here is the confirmed Study Question Bank:
            {json.dumps(s_qb_data)}
            
            CRITICAL REQUIREMENTS:
            1. The questions you generate now must be DIFFERENT from the questions in the Study Question Bank. Do not repeat the same question phrasing, options, or angles.
            2. For each topic:
               - Generate 2 FRESH MCQs. They must test a different angle, detail, or concept comparison (e.g. comparing Star vs Snowflake instead of just asking what is a fact table).
               - Generate 2 FRESH Q&A pairs. They must be application-level or scenario-based questions (e.g. giving a table structure and asking to normalize it, rather than just asking "Explain Normalization").
            3. The ideal answers/explanations should be aligned with the concept explanation in the QB.
            
            For each topic, output the fresh questions in JSON matching this schema:
            {{
                "topic_quiz_questions": [
                    {{
                        "topic": "topic name",
                        "mcqs": [
                            {{
                                "question": "fresh MCQ question text",
                                "options": {{"A": "option A", "B": "option B", "C": "option C", "D": "option D"}},
                                "correct_option": "A, B, C, or D",
                                "explanation": "explanation of why it is correct"
                            }},
                            ...
                        ],
                        "qa_pairs": [
                            {{
                                "question": "fresh application-level Q&A question text",
                                "answer": "model answer/explanation"
                            }},
                            ...
                        ]
                    }},
                    ...
                ]
            }}
            """

            try:
                response = generate_content_with_retry(
                    client,
                    model=MODEL_NAME,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=get_clean_response_schema(
                            SubjectQuizQuestionsOutput
                        ),
                    ),
                )
                data = json.loads(response.text)

                for tq in data.get("topic_quiz_questions", []):
                    topic = tq.get("topic")

                    orig_bank = s_qb_data.get(topic, {})
                    explanation = (
                        orig_bank.get("explanation", "")
                        if isinstance(orig_bank, dict)
                        else getattr(orig_bank, "explanation", "")
                    )

                    for mcq in tq.get("mcqs", []):
                        mcqs_pool.append(
                            {
                                "type": "mcq",
                                "question": mcq.get("question", ""),
                                "options": mcq.get("options", {}),
                                "correct_option": mcq.get("correct_option", ""),
                                "explanation": mcq.get("explanation", ""),
                                "topic": topic,
                                "subject": s,
                            }
                        )
                    for qa in tq.get("qa_pairs", []):
                        qa_pool.append(
                            {
                                "type": "qa",
                                "question": qa.get("question", ""),
                                "answer": qa.get("answer", ""),
                                "explanation": explanation,  # QB explanation remains the grading reference
                                "topic": topic,
                                "subject": s,
                            }
                        )
            except Exception as e:
                print(
                    "Failed to generate fresh quiz questions via LLM, falling back to QB questions:",
                    e,
                )
                # Fallback to the original questions in the QB if LLM generation fails
                for topic, bank in s_qb_data.items():
                    explanation = (
                        bank.get("explanation", "")
                        if isinstance(bank, dict)
                        else getattr(bank, "explanation", "")
                    )
                    mcqs = (
                        bank.get("mcqs", [])
                        if isinstance(bank, dict)
                        else getattr(bank, "mcqs", [])
                    )
                    qa_pairs = (
                        bank.get("qa_pairs", [])
                        if isinstance(bank, dict)
                        else getattr(bank, "qa_pairs", [])
                    )

                    for mcq in mcqs:
                        if isinstance(mcq, dict):
                            q = mcq.get("question", "")
                            options = mcq.get("options", {})
                            correct_option = mcq.get("correct_option", "")
                            exp = mcq.get("explanation", "")
                        else:
                            q = mcq.question
                            options = mcq.options
                            correct_option = mcq.correct_option
                            exp = mcq.explanation
                        mcqs_pool.append(
                            {
                                "type": "mcq",
                                "question": q,
                                "options": options,
                                "correct_option": correct_option,
                                "explanation": exp,
                                "topic": topic,
                                "subject": s,
                            }
                        )
                    for qa in qa_pairs:
                        if isinstance(qa, dict):
                            q = qa.get("question", "")
                            a = qa.get("answer", "")
                        else:
                            q = qa.question
                            a = qa.answer
                        qa_pool.append(
                            {
                                "type": "qa",
                                "question": q,
                                "answer": a,
                                "explanation": explanation,
                                "topic": topic,
                                "subject": s,
                            }
                        )

        import random

        random.shuffle(mcqs_pool)
        random.shuffle(qa_pool)

        # Alternate: MCQ -> QA -> MCQ -> QA...
        active_qs = []
        i = 0
        j = 0
        while i < len(mcqs_pool) or j < len(qa_pool):
            if i < len(mcqs_pool):
                active_qs.append(mcqs_pool[i])
                i += 1
            if j < len(qa_pool):
                active_qs.append(qa_pool[j])
                j += 1

        ctx.state["active_quiz_questions"] = active_qs

    if active_qs:
        ctx.state["quiz_active"] = True

    idx = ctx.state.get("current_question_index", 0)
    interrupt_id = f"quiz_ans_{idx}"

    # 1. Process reply if we are resuming from a question interrupt or have general input during active quiz
    student_answer = None
    if ctx.resume_inputs and interrupt_id in ctx.resume_inputs:
        student_answer = str(ctx.resume_inputs[interrupt_id]).strip()
    elif was_quiz_active and node_input:
        if isinstance(node_input, str):
            student_answer = node_input.strip()
        elif hasattr(node_input, "parts") and node_input.parts:
            student_answer = "".join(
                [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
            ).strip()
        elif hasattr(node_input, "text") and node_input.text:
            student_answer = node_input.text.strip()

    if student_answer is not None:
        # Stop check
        if student_answer.lower() in ["stop", "exit", "quit", "end"]:
            # Cleanup state
            ctx.state["selected_subjects"] = []
            ctx.state["active_quiz_questions"] = []
            ctx.state["ai_flagged_count"] = 0
            ctx.state["quiz_active"] = False

            summary_text = compute_quiz_results_summary(
                ctx, stopped_early=True, total_questions=len(active_qs)
            )

            # Wire MCPToolset and get study tips for each weak topic
            _ = mcp_toolset
            weak_topics = ctx.state.get("weak_topics", [])
            topic_stats = ctx.state.get("topic_stats", {})
            study_tips = []
            for wt in weak_topics:
                stat = topic_stats.get(wt, {})
                accuracy = 0.0
                if isinstance(stat, dict):
                    accuracy = stat.get("accuracy", 0.0)
                elif stat:
                    accuracy = getattr(stat, "accuracy", 0.0)
                try:
                    tip = await call_mcp_tool(
                        "get_study_tip",
                        {"topic": wt, "accuracy_pct": accuracy * 100.0}
                    )
                    study_tips.append(f"- **{wt}**: {tip}")
                except Exception:
                    pass
            if study_tips:
                summary_text += "\n\n💡 **Study Tips for Weak Topics:**\n" + "\n".join(study_tips)

            ctx.state["pending_interrupt"] = None
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=summary_text)],
                ),
                actions=EventActions(),
            )
            yield RequestInput(
                interrupt_id="post_quiz_action",
                message="",
            )
            return

        current_q = active_qs[idx]
        q_type = current_q.get("type", "qa")

        is_correct = False
        feedback_explanation = ""
        was_flagged_ai_this_time = False
        flagged_count = 0

        if q_type == "mcq":
            student_ans_clean = student_answer.strip().upper()
            correct_opt = current_q["correct_option"].upper()

            if student_ans_clean in ["A", "B", "C", "D"]:
                is_correct = student_ans_clean == correct_opt
            else:
                opts = current_q.get("options", {})
                correct_text = opts.get(correct_opt, "").strip().lower()
                student_ans_lower = student_answer.strip().lower()
                is_correct = student_ans_lower == correct_text

            feedback_explanation = current_q.get("explanation", "")
        else:
            flagged_count = ctx.state.get("ai_flagged_count", 0)
            if flagged_count == 0:
                auth_res = check_authenticity(current_q["question"], student_answer)
                if auth_res == "AI_GENERATED":
                    ctx.state["ai_flagged_count"] = 1
                    msg = (
                        "Looks like that answer might be AI-generated!\n\n"
                        "That's okay — but this quiz is meant to help YOU learn, not your AI\n"
                        "assistant.\n\n"
                        "Your own words — even if rough, incomplete, or in Hinglish — are 100%\n"
                        "valid and actually help you remember better than a perfect AI answer.\n\n"
                        "Please retype your answer in your own words below:"
                    )
                    ctx.state["pending_interrupt"] = "quiz"
                    yield Event(
                        content=types.Content(
                            role="model",
                            parts=[types.Part.from_text(text=msg)],
                        ),
                        actions=EventActions(),
                    )
                    yield RequestInput(
                        interrupt_id=interrupt_id,
                        message="",
                    )
                    return
            else:
                # Retry attempt: grade normally, bypassing check.
                pass

            grading = grade_answer_impl(current_q, student_answer)
            is_correct = grading.get("correct", False)
            feedback_explanation = grading.get("explanation", "")

        # Update topic stats
        topic_stats = ctx.state.get("topic_stats", {})
        topic = current_q.get("topic", "General")

        stat = topic_stats.get(topic)
        if not stat:
            stat = {
                "topic": topic,
                "attempted": 0,
                "correct": 0,
                "accuracy": 0.0,
                "flagged_ai": False,
            }
        elif isinstance(stat, TopicStat):
            stat = stat.model_dump()

        stat["attempted"] += 1
        if is_correct:
            stat["correct"] += 1
        stat["accuracy"] = stat["correct"] / stat["attempted"]
        if flagged_count >= 1:
            stat["flagged_ai"] = True

        topic_stats[topic] = stat

        # Store in quiz history
        history_item = {
            "type": q_type,
            "topic": topic,
            "question": current_q["question"],
            "student_answer": student_answer,
            "is_correct": is_correct,
            "explanation": feedback_explanation,
            "subject": current_q["subject"],
        }
        if q_type == "mcq":
            correct_opt = current_q["correct_option"].upper()
            opts = current_q.get("options") or {}
            opts_dict = {}
            if hasattr(opts, "model_dump"):
                opts_dict = opts.model_dump()
            elif isinstance(opts, dict):
                opts_dict = opts
            correct_text = opts_dict.get(correct_opt) or opts_dict.get(
                correct_opt.lower(), ""
            )

            history_item["correct_letter"] = correct_opt
            history_item["correct_option_text"] = correct_text
            history_item["options"] = opts_dict
        else:
            history_item["correct_answer"] = current_q["answer"]
            history_item["concept_explanation"] = current_q["explanation"]

        history = list(ctx.state.get("quiz_history", []))
        history.append(history_item)
        ctx.state["quiz_history"] = history

        # Output feedback
        feedback = f"### 📝 Question {idx + 1} Feedback\n"
        feedback += f"**Your Answer:** {student_answer}\n"
        feedback += f"**Result:** {'✅ Correct' if is_correct else '❌ Incorrect'}\n"
        if flagged_count >= 1:
            if is_correct:
                feedback += "🌟 *Great job rephrasing in your own words! Answering in your own words is harder, but it's much more valuable for your actual learning than copy-pasting. Keep it up!*\n\n"
            else:
                feedback += "*(Note: This was a re-entered answer in your own words, graded normally)*\n\n"
        feedback += f"**Explanation:** {feedback_explanation}\n\n"

        yield Event(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=feedback)]
            ),
            actions=EventActions(),
        )

        # Move index forward and update state
        idx += 1
        ctx.state["topic_stats"] = topic_stats
        ctx.state["current_question_index"] = idx
        ctx.state["ai_flagged_count"] = 0

    # 2. Check if the quiz is finished
    if idx >= len(active_qs):
        # Cleanup state
        ctx.state["selected_subjects"] = []
        ctx.state["active_quiz_questions"] = []
        ctx.state["ai_flagged_count"] = 0
        ctx.state["quiz_active"] = False

        summary_text = compute_quiz_results_summary(
            ctx, stopped_early=False, total_questions=len(active_qs)
        )

        # Wire MCPToolset and get study tips for each weak topic
        _ = mcp_toolset
        weak_topics = ctx.state.get("weak_topics", [])
        topic_stats = ctx.state.get("topic_stats", {})
        study_tips = []
        for wt in weak_topics:
            stat = topic_stats.get(wt, {})
            accuracy = 0.0
            if isinstance(stat, dict):
                accuracy = stat.get("accuracy", 0.0)
            elif stat:
                accuracy = getattr(stat, "accuracy", 0.0)
            try:
                tip = await call_mcp_tool(
                    "get_study_tip",
                    {"topic": wt, "accuracy_pct": accuracy * 100.0}
                )
                study_tips.append(f"- **{wt}**: {tip}")
            except Exception:
                pass
        if study_tips:
            summary_text += "\n\n💡 **Study Tips for Weak Topics:**\n" + "\n".join(study_tips)

        ctx.state["pending_interrupt"] = None
        yield Event(
            content=types.Content(
                role="model",
                parts=[types.Part.from_text(text=summary_text)],
            ),
            actions=EventActions(),
        )
        yield RequestInput(
            interrupt_id="post_quiz_action",
            message="",
        )
        return

    # 3. Present the next question
    next_q = active_qs[idx]
    next_interrupt_id = f"quiz_ans_{idx}"
    q_type = next_q.get("type", "qa")

    subj_prefix = f"[{next_q['subject']}] " if len(selected) > 1 else ""

    if q_type == "mcq":
        presented = next_q["question"]
        opts = next_q.get("options") or {}

        question_text = f"❓ **Question {idx + 1} of {len(active_qs)}** (Topic: *{subj_prefix}{next_q.get('topic', 'General')}* - MCQ)\n\n"
        question_text += f"{presented}\n\n"

        opts_dict = {}
        if hasattr(opts, "model_dump"):
            opts_dict = opts.model_dump()
        elif isinstance(opts, dict):
            opts_dict = opts

        if isinstance(opts, list):
            for i, val in enumerate(opts):
                key = chr(65 + i)
                question_text += f"{key}) {val}\n"
        elif opts_dict:
            for key in ["A", "B", "C", "D"]:
                val = opts_dict.get(key) or opts_dict.get(key.lower(), "")
                if val:
                    question_text += f"{key}) {val}\n"
            # Fallback if keys are not A/B/C/D
            if not any(f"{k})" in question_text for k in ["A", "B", "C", "D"]):
                for k, v in sorted(opts_dict.items()):
                    question_text += f"{k}) {v}\n"
        question_text += "\n(Type A, B, C, or D — or type 'stop' to finish)"
    else:
        presented = next_q.get("presented_question")
        if not presented:
            import random

            # 50/50 chance to rephrase for variety
            use_variant = random.choice([True, False])
            if use_variant:
                client = genai.Client()
                prompt = f"""
                You are a study tutor. Rephrase this practice question slightly to test the student's understanding in a different way, but keep the core meaning and required answer identical.
                
                Original Question: {next_q["question"]}
                
                Provide only the rephrased question, no other text or explanation.
                """
                try:
                    response = generate_content_with_retry(
                        client,
                        model=MODEL_NAME,
                        contents=prompt,
                    )
                    presented = response.text.strip()
                except Exception:
                    presented = next_q["question"]
            else:
                presented = next_q["question"]

            next_q["presented_question"] = presented
            ctx.state["active_quiz_questions"][idx] = next_q

        question_text = f"❓ **Question {idx + 1} of {len(active_qs)}** (Topic: *{subj_prefix}{next_q.get('topic', 'General')}*)\n\n"
        question_text += f"{presented}\n"
        question_text += "\n*(Answer in your own words, or type 'stop' to finish and see your summary)*"

    ctx.state["pending_interrupt"] = "quiz"
    yield Event(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=question_text)],
        ),
        actions=EventActions(),
    )
    if q_type == "mcq":
        yield RequestInput(
            interrupt_id=next_interrupt_id,
            message="",
        )
    else:
        yield RequestInput(
            interrupt_id=next_interrupt_id,
            message="",
        )


def grade_answer_impl(question_dict: dict, student_answer: str) -> dict:
    """Grades descriptive questions using Gemini based on stored QB answer and explanation."""
    client = genai.Client()
    q_text = question_dict.get("presented_question") or question_dict.get("question")
    ideal_ans = question_dict.get("answer")
    explanation = question_dict.get("explanation")

    prompt = f"""
    You are an expert tutor. Grade the student's answer for the following question.
    
    Question Asked: {q_text}
    Ideal Model Answer: {ideal_ans}
    Concept Explanation: {explanation}
    
    Student's Answer: {student_answer}
    
    Evaluate if the student's answer is correct or shows sufficient understanding of the concept.
    Reference or paraphrase the Concept Explanation in your explanation of why they are correct or incorrect. Do not invent a fresh explanation from scratch.
    
    Provide output in JSON matching this schema:
    {{
        "correct": true/false,
        "explanation": "Paraphrase of the Concept Explanation and constructive feedback."
    }}
    """
    try:
        response = generate_content_with_retry(
            client,
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=get_clean_response_schema(GradingResult),
            ),
        )
        return json.loads(response.text)
    except Exception as e:
        return {"correct": False, "explanation": f"Grading failed: {str(e)}"}


def compute_quiz_results_summary(
    ctx: Context, stopped_early: bool, total_questions: int
) -> str:
    history = ctx.state.get("quiz_history", [])

    # Calculate score
    correct_count = sum(1 for q in history if q.get("is_correct", False))
    total = len(history)
    pct = round((correct_count / total * 100), 1) if total > 0 else 0.0

    subject_name = ctx.state.get("current_subject", "Unknown Subject")

    lines = []
    lines.append("📊 QUIZ COMPLETE — YOUR RESULTS")
    lines.append(f"Subject: {subject_name}")
    if stopped_early:
        lines.append(
            f"Quiz stopped early — {total} of {total_questions} questions attempted"
        )
    lines.append(f"Score: {correct_count} / {total} ({pct}%)")
    lines.append("")

    # Details for every question attempted
    for idx, item in enumerate(history):
        q_num = idx + 1
        q_type = item.get("type", "qa").upper()
        topic = item.get("topic", "General")
        q_text = item.get("question", "")

        lines.append(f"Q{q_num}. {topic} — {q_type}")
        lines.append(f"❓ {q_text}\n")

        if q_type == "MCQ":
            opts = item.get("options") or {}
            # Format options A) ... B) ... C) ... D) ...
            opt_strings = []
            for key in ["A", "B", "C", "D"]:
                val = opts.get(key) or opts.get(key.lower(), "")
                if val:
                    opt_strings.append(f"{key}) {val}")

            lines.append("   " + "  ".join(opt_strings))
            lines.append(f"   📝 Your answer: {item.get('student_answer')}")

            correct_letter = item.get("correct_letter", "A")
            correct_text = item.get("correct_option_text", "")
            lines.append(f"   ✅ Correct answer: {correct_letter}) {correct_text}")

            status = "✅ Correct!" if item.get("is_correct", False) else "❌ Incorrect"
            lines.append(f"   [{status}]")
            lines.append(f"   💡 Explanation: {item.get('explanation')}\n")
        else:
            lines.append(f"   📝 Your answer: {item.get('student_answer')}")
            lines.append(f"   ✅ Model answer: {item.get('correct_answer')}")

            status = "✅ Correct!" if item.get("is_correct", False) else "❌ Incorrect"
            lines.append(f"   [{status}]")
            lines.append(f"   💡 Key concept: {item.get('concept_explanation')}\n")

    lines.append("")
    lines.append("📈 TOPIC-WISE ACCURACY")

    # Use topic_stats directly for accuracy data to avoid double-counting
    current_quiz_topics = []
    seen = set()
    for item in history:
        t = item.get("topic", "General")
        if t not in seen:
            seen.add(t)
            current_quiz_topics.append(t)

    topic_stats = ctx.state.get("topic_stats", {})
    weak_topics = []
    for t in current_quiz_topics:
        stat = topic_stats.get(t, {})
        if isinstance(stat, dict):
            correct = stat.get("correct", 0)
            attempted_topic = stat.get("attempted", 0)
            accuracy = stat.get("accuracy", 0.0)
        else:
            correct = getattr(stat, "correct", 0)
            attempted_topic = getattr(stat, "attempted", 0)
            accuracy = getattr(stat, "accuracy", 0.0)

        pct_str = f"{accuracy * 100:.1f}%"

        if accuracy < 0.60:
            indicator = "🔴 Weak"
            weak_topics.append((t, pct_str))
        elif accuracy <= 0.80:
            indicator = "🟡 Average"
        else:
            indicator = "🟢 Strong"

        lines.append(
            f"{t}: {correct}/{attempted_topic} correct ({pct_str})  [{indicator}]"
        )

    # Save weak topics to state
    weak_topics_names = [t[0] for t in weak_topics]
    ctx.state["weak_topics"] = weak_topics_names

    lines.append("")
    lines.append("⚠️ TOPICS NEEDING REVISION:")
    if weak_topics:
        for t, p in weak_topics:
            lines.append(f"- {t} ({p})")
    else:
        lines.append("🎉 None! All topics are in Strong/Average shape.")

    lines.append("\nWhat would you like to do next?")
    lines.append("- Type 'make a plan' for a revision schedule")
    lines.append("- Type 'quiz me again' to retake on weak topics only")
    lines.append("- Type 'new subject' to add another subject")

    return "\n".join(lines)


@node(rerun_on_resume=True)
async def planner_node(ctx: Context, node_input: Any):
    """Plans a study revision schedule prioritizing weak topics using weights and groupings."""
    ctx.state["pending_interrupt"] = None
    subjects = ctx.state.get("subjects", {})
    if not subjects:
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="No active subjects. Please paste study notes or syllabus first."
                    )
                ],
            ),
            actions=EventActions(),
        )
        return

    # 1. Subject selection check
    selected = ctx.state.get("selected_subjects", [])
    if not selected:
        if len(subjects) == 1:
            selected = list(subjects.keys())
            ctx.state["selected_subjects"] = selected
        else:
            select_id = "select_planner_subjects"
            is_resuming = ctx.resume_inputs and select_id in ctx.resume_inputs
            sel_input = None
            if is_resuming:
                sel_input = str(ctx.resume_inputs[select_id]).strip()
            elif node_input:
                input_str = ""
                if isinstance(node_input, str):
                    input_str = node_input.strip()
                elif hasattr(node_input, "parts") and node_input.parts:
                    input_str = "".join(
                        [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
                    ).strip()
                
                input_lower = input_str.lower()
                contains_subject = any(s.lower() in input_lower for s in subjects.keys())
                contains_all = any(k in input_lower for k in ["both", "combined", "all"])
                if contains_subject or contains_all:
                    sel_input = input_str

            if sel_input:
                client = genai.Client()
                parse_prompt = f"""
                Analyze the student's choice of subjects to study.
                Available subjects in state: {list(subjects.keys())}
                Student's choice: "{sel_input}"
                
                Identify which of the available subjects they want to include. If they chose "both", "combined", "all", or named multiple, include all matching ones.
                Provide your output in JSON format matching this schema:
                {{
                    "selected": ["subject1", "subject2", ...]
                }}
                """
                try:
                    res = generate_content_with_retry(
                        client,
                        model=MODEL_NAME,
                        contents=parse_prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                            response_schema=get_clean_response_schema(
                                SubjectSelectionOutput
                            ),
                        ),
                    )
                    data = json.loads(res.text)
                    selected = data.get("selected", [])
                except Exception:
                    selected = []
                    lower_input = sel_input.lower()
                    if any(k in lower_input for k in ["both", "combined", "all"]):
                        selected = list(subjects.keys())
                    else:
                        for s in subjects.keys():
                            if s.lower() in lower_input:
                                selected.append(s)

                if not selected:
                    selected = list(subjects.keys())

                ctx.state["selected_subjects"] = selected
            else:
                subj_list_str = (
                    " and ".join(subjects.keys())
                    if len(subjects) == 2
                    else ", ".join(subjects.keys())
                )
                ctx.state["pending_interrupt"] = "plan"
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text=f"📚 You've got {subj_list_str} loaded — make a revision plan for which one, or both combined?"
                            )
                        ],
                    ),
                    actions=EventActions(),
                )
                yield RequestInput(
                    interrupt_id=select_id,
                    message="",
                )
                return

    # 2. minutes_per_day check
    minutes = ctx.state.get("minutes_per_day")
    if minutes is None:
        min_id = "ask_minutes_per_day"
        if ctx.resume_inputs and min_id in ctx.resume_inputs:
            min_input = str(ctx.resume_inputs[min_id]).strip()
            match = re.search(r"\d+", min_input)
            if match:
                minutes = int(match.group(0))
                ctx.state["minutes_per_day"] = minutes
            else:
                ctx.state["pending_interrupt"] = "plan"
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[
                            types.Part.from_text(
                                text="⚠️ Invalid input. Please enter a number for minutes per day (e.g. 60):"
                            )
                        ],
                    ),
                    actions=EventActions(),
                )
                yield RequestInput(
                    interrupt_id=min_id,
                    message="",
                )
                return
        else:
            ctx.state["pending_interrupt"] = "plan"
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text="⏱️ How many minutes per day do you plan to study (e.g., 60)?"
                        )
                    ],
                ),
                actions=EventActions(),
            )
            yield RequestInput(
                interrupt_id=min_id,
                message="",
            )
            return

    # 3. exam_date check
    exam_date_str = ctx.state.get("exam_date")
    user_text = ""
    if hasattr(node_input, "parts") and node_input.parts:
        user_text = "".join(
            [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
        )
    elif isinstance(node_input, str):
        user_text = node_input

    if ctx.resume_inputs and "ask_exam_date" in ctx.resume_inputs:
        exam_date_str = str(ctx.resume_inputs["ask_exam_date"]).strip()
    elif not exam_date_str and user_text:
        match = re.search(r"\d{4}-\d{2}-\d{2}", user_text)
        if match:
            exam_date_str = match.group(0)

    if not exam_date_str:
        ctx.state["pending_interrupt"] = "plan"
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="📅 Please enter your exam date (in YYYY-MM-DD format, e.g. 2026-07-01):"
                    )
                ],
            ),
            actions=EventActions(),
        )
        yield RequestInput(
            interrupt_id="ask_exam_date",
            message="",
        )
        return

    try:
        exam_date = datetime.datetime.strptime(exam_date_str, "%Y-%m-%d").date()
    except ValueError:
        ctx.state["pending_interrupt"] = "plan"
        yield Event(
            content=types.Content(
                role="model",
                parts=[
                    types.Part.from_text(
                        text="⚠️ Invalid date format. Please enter the exam date in YYYY-MM-DD format (e.g. 2026-07-01):"
                    )
                ],
            ),
            actions=EventActions(),
        )
        yield RequestInput(
            interrupt_id="ask_exam_date",
            message="",
        )
        return

    today = datetime.date.today()
    days_remaining = (exam_date - today).days

    if days_remaining <= 0:
        schedule_text = f"📅 **Exam Date:** {exam_date_str}\n⚠️ Your exam is today or has already passed!"
        ctx.state["selected_subjects"] = []
        yield Event(
            output=schedule_text,
            actions=EventActions(state_delta={"exam_date": exam_date_str}),
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=schedule_text)]
            ),
        )
        return

    # 4. Weighted Scheduling logic
    topic_stats = ctx.state.get("topic_stats", {})
    topic_stats_standard = {}
    for k, v in topic_stats.items():
        if isinstance(v, TopicStat):
            topic_stats_standard[k] = v.model_dump()
        else:
            topic_stats_standard[k] = v

    topics_with_weights = []
    for s in selected:
        s_detail = subjects.get(s)
        if isinstance(s_detail, dict):
            s_topics = s_detail.get("topics", [])
        elif s_detail:
            s_topics = s_detail.topics
        else:
            s_topics = []

        for t in s_topics:
            stat = topic_stats_standard.get(t, {})
            accuracy = stat.get("accuracy", 0.0)
            attempted = stat.get("attempted", 0)
            weight = 100.0 - (accuracy * 100.0)
            topics_with_weights.append(
                {
                    "subject": s,
                    "topic": t,
                    "accuracy_pct": round(accuracy * 100.0, 1),
                    "attempted": attempted,
                    "weight": round(weight, 1),
                    "flagged_ai": stat.get("flagged_ai", False),
                }
            )

    client = genai.Client()
    plan_prompt = f"""
    You are an expert study planning coordinator.
    Design a highly personalized, smart, day-wise revision study schedule for the student.
    
    Inputs:
    - Selected Subjects: {selected}
    - Topics with calculated study weights (weight = 100 - accuracy%; higher weight means weaker area; 100 weight means never practiced):
      {json.dumps(topics_with_weights)}
    - Available Days: {days_remaining}
    - Student Availability: {minutes} minutes per day
    
    Requirements:
    1. Distribute the study days proportionally based on the weights. Weaker topics (higher weight) must get more sessions/depth/time.
    2. Group multiple light/related topics into a single day's session if days are limited or if their weights are low/similar.
    3. Use the {minutes} minutes per day to indicate how deep they should go on each day (e.g. adjust task lists/depth to fit {minutes} mins).
    4. Each daily entry MUST explicitly label the subject (e.g., "[DBMS] Normalization") and explain WHY it's prioritized (e.g. "[DBMS] Normalization (Focus: weakest area, 20% accuracy)" or "[OS] CPU Scheduling (Standard review, 80% accuracy)").
    5. Reserve the last 1-2 days before the exam date (out of {days_remaining} days) specifically for mixed practice, mock exams, and final review across all topics.
    
    Output Format:
    Return a beautiful, structured Markdown schedule. Start with a brief summary of the plan structure, followed by the daily entries.
    """

    try:
        response = generate_content_with_retry(
            client,
            model=MODEL_NAME,
            contents=plan_prompt,
        )
        schedule_text = response.text.strip()
    except Exception as e:
        schedule_text = f"📅 **Exam Date:** {exam_date_str}\nFailed to generate personalized schedule: {e!s}"

    ctx.state["selected_subjects"] = []

    # Wire MCPToolset and get study tips & export revision schedule
    _ = mcp_toolset

    study_tips = []
    for tw in topics_with_weights:
        topic_name = tw["topic"]
        accuracy_pct = tw["accuracy_pct"]
        try:
            tip = await call_mcp_tool(
                "get_study_tip",
                {"topic": topic_name, "accuracy_pct": accuracy_pct}
            )
            study_tips.append(f"- **{topic_name}**: {tip}")
        except Exception:
            pass

    if study_tips:
        schedule_text += "\n\n💡 **Targeted Study Tips:**\n" + "\n".join(study_tips)

    # Export revision schedule
    try:
        subj_name = ", ".join(selected) if isinstance(selected, list) else str(selected)
        schedule_dict = {"Revision Plan": schedule_text}
        exported_path = await call_mcp_tool(
            "export_revision_schedule",
            {
                "subject": subj_name,
                "schedule": schedule_dict,
                "exam_date": exam_date_str,
            }
        )
        schedule_text += f"\n\n📂 *Revision schedule exported to:* `{exported_path}`"
    except Exception as e:
        schedule_text += f"\n\n⚠️ Failed to export revision schedule: {e!s}"

    content = types.Content(
        role="model", parts=[types.Part.from_text(text=schedule_text)]
    )

    yield Event(
        output=schedule_text,
        actions=EventActions(
            state_delta={"exam_date": exam_date_str, "pending_interrupt": None}
        ),
        content=content,
    )



@node
async def remedial_guide_node(ctx: Context, node_input: Any):
    """Generates and exports a deep-dive Remedial Study Guide for weak topics (accuracy < 60%)."""
    ctx.state["pending_interrupt"] = None
    weak_topics = ctx.state.get("weak_topics", [])
    if not weak_topics:
        msg = (
            "You don't have any weak topics yet! "
            "Please take a diagnostic quiz first to identify areas needing improvement."
        )
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=msg)]),
            actions=EventActions(),
        )
        return

    subject = ctx.state.get("current_subject") or "General"
    q_banks = ctx.state.get("question_banks", {})

    client = genai.Client()
    sections = []
    sections.append(f"# REMEDIAL STUDY GUIDE - {subject.upper()}\n")
    sections.append("This study guide targets specific concepts where you had lower accuracy in diagnostic quizzes. Review these concept breakdowns, pitfalls, and worked examples.\n")

    for topic in weak_topics:
        # Check if we have Question Bank data for this topic
        topic_qb = None
        for s, qb in q_banks.items():
            if topic in qb:
                topic_qb = qb[topic]
                break

        explanation = ""
        qa_pairs_serialized = []
        if topic_qb:
            if isinstance(topic_qb, dict):
                explanation = topic_qb.get("explanation", "")
                qa_list = topic_qb.get("qa_pairs", [])
            else:
                explanation = getattr(topic_qb, "explanation", "")
                qa_list = getattr(topic_qb, "qa_pairs", [])

            for qa in qa_list:
                if isinstance(qa, dict):
                    qa_pairs_serialized.append({"Q": qa.get("question"), "A": qa.get("answer")})
                else:
                    qa_pairs_serialized.append({"Q": getattr(qa, "question"), "A": getattr(qa, "answer")})

        prompt = f"""
        You are an expert tutor. Create a deep-dive remedial study breakdown for the topic: "{topic}".
        Subject: {subject}
        Base Explanation: {explanation}
        Sample Questions and Answers: {json.dumps(qa_pairs_serialized)}

        CRITICAL FORMATTING REQUIREMENT: Do NOT include any emojis, decorative separator lines (like '━━━━━━━━━━━━━━━━━━━━━━━━' or similar), or box-drawing characters in your response. Keep the output as clean, plain markdown text.

        Generate a breakdown containing exactly these sections:
        1. Core Analogy (an intuitive, real-world comparison to explain the concept simply)
        2. Common Exam Pitfalls & Misconceptions (what students typically get wrong, forget, or confuse under exam pressure)
        3. Step-by-Step Resolved Scenario (a concrete example or problem showing how the concept is applied, solved, or structured)
        4. Focus Study Tip (one actionable study tip for this topic)
        """
        try:
            res = generate_content_with_retry(
                client,
                model=MODEL_NAME,
                contents=prompt,
            )
            breakdown = res.text.strip()
        except Exception as e:
            breakdown = f"Failed to generate remedial breakdown: {e!s}"

        sections.append(f"## Topic: {topic}")
        sections.append(f"Concept Explanation: {explanation}\n")
        sections.append(breakdown)
        sections.append("\n---\n")

    guide_content = "\n".join(sections)

    # Export to exports/
    ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    EXPORTS_DIR = os.path.join(ROOT_DIR, "exports")
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    filename = f"remedial_guide_{subject.lower().replace(' ', '_')}.md"
    filepath = os.path.join(EXPORTS_DIR, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(guide_content)
        export_msg = f"\n\n📂 *Remedial study guide exported to:* `{filepath}`"
    except Exception as e:
        export_msg = f"\n\n⚠️ Failed to export study guide: {e!s}"

    full_message = guide_content + export_msg
    yield Event(
        output=full_message,
        actions=EventActions(),
        content=types.Content(role="model", parts=[types.Part.from_text(text=full_message)]),
    )


@node(rerun_on_resume=True)
async def pomodoro_node(ctx: Context, node_input: Any):
    """Administers an active Pomodoro study session and a 2-question checkpoint quiz."""
    ctx.state["pending_interrupt"] = None
    subjects = ctx.state.get("subjects", {})
    if not subjects:
        yield Event(
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="No active subjects. Please paste study notes or syllabus first.")]
            ),
            actions=EventActions(),
        )
        return

    active = ctx.state.get("study_timer_active", False)
    checkpoint_qs = ctx.state.get("pomodoro_checkpoint_questions", [])

    # CASE 1: Checkpoint Quiz is Active
    if active and checkpoint_qs:
        idx = ctx.state.get("pomodoro_checkpoint_index", 0)
        interrupt_id = f"pomodoro_checkpoint_{idx}"

        student_answer = None
        if ctx.resume_inputs and interrupt_id in ctx.resume_inputs:
            student_answer = str(ctx.resume_inputs[interrupt_id]).strip()
        elif node_input:
            if isinstance(node_input, str):
                student_answer = node_input.strip()
            elif hasattr(node_input, "parts") and node_input.parts:
                student_answer = "".join(
                    [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
                ).strip()

        if student_answer is not None:
            # Check for stop/exit command
            if student_answer.lower() in ["stop", "exit", "quit", "cancel"]:
                ctx.state["study_timer_active"] = False
                ctx.state["pomodoro_checkpoint_questions"] = []
                ctx.state["pomodoro_checkpoint_index"] = 0
                ctx.state["study_session_topic"] = None
                stop_msg = "⏱️ Checkpoint Quiz stopped."
                yield Event(
                    output=stop_msg,
                    actions=EventActions(
                        state_delta={
                            "study_timer_active": False,
                            "pomodoro_checkpoint_questions": [],
                            "pomodoro_checkpoint_index": 0,
                            "study_session_topic": None,
                        }
                    ),
                    content=types.Content(role="model", parts=[types.Part.from_text(text=stop_msg)]),
                )
                return

            current_q = checkpoint_qs[idx]
            q_type = current_q.get("type", "mcq")

            is_correct = False
            feedback_explanation = ""

            if q_type == "mcq":
                student_ans_clean = student_answer.strip().upper()
                correct_opt = current_q["correct_option"].upper()
                if student_ans_clean in ["A", "B", "C", "D"]:
                    is_correct = student_ans_clean == correct_opt
                else:
                    opts = current_q.get("options", {})
                    correct_text = opts.get(correct_opt, "").strip().lower()
                    is_correct = student_answer.strip().lower() == correct_text
                feedback_explanation = current_q.get("explanation", "")
            else:
                grading = grade_answer_impl(current_q, student_answer)
                is_correct = grading.get("correct", False)
                feedback_explanation = grading.get("explanation", "")

            # Output feedback
            feedback = f"📝 **Question {idx + 1} Feedback:**\n"
            feedback += f"Result: {'Correct' if is_correct else 'Incorrect'}\n"
            feedback += f"Explanation: {feedback_explanation}\n\n"
            yield Event(
                content=types.Content(role="model", parts=[types.Part.from_text(text=feedback)]),
                actions=EventActions(),
            )

            idx += 1
            ctx.state["pomodoro_checkpoint_index"] = idx

        # Present next question
        if idx < len(checkpoint_qs):
            next_q = checkpoint_qs[idx]
            next_interrupt_id = f"pomodoro_checkpoint_{idx}"
            q_type = next_q.get("type", "mcq")

            if q_type == "mcq":
                opts = next_q.get("options") or {}
                question_text = f"❓ **Checkpoint MCQ:**\n\n{next_q['question']}\n\n"
                for key in ["A", "B", "C", "D"]:
                    val = opts.get(key) or opts.get(key.lower(), "")
                    if val:
                        question_text += f"{key}) {val}\n"
                question_text += "\n(Type A, B, C, or D)"
            else:
                question_text = f"❓ **Checkpoint Question:**\n\n{next_q['question']}\n\n(Answer in your own words)"

            ctx.state["pending_interrupt"] = "pomodoro"
            yield Event(
                content=types.Content(role="model", parts=[types.Part.from_text(text=question_text)]),
                actions=EventActions(),
            )
            yield RequestInput(interrupt_id=next_interrupt_id, message="")
            return
        else:
            completion_msg = "🎉 **Checkpoint Quiz Complete!** Keep up the great work. Focus session finished successfully."
            ctx.state["study_timer_active"] = False
            ctx.state["pomodoro_checkpoint_questions"] = []
            ctx.state["pomodoro_checkpoint_index"] = 0
            ctx.state["study_session_topic"] = None
            yield Event(
                output=completion_msg,
                actions=EventActions(
                    state_delta={
                        "study_timer_active": False,
                        "pomodoro_checkpoint_questions": [],
                        "pomodoro_checkpoint_index": 0,
                        "study_session_topic": None,
                    }
                ),
                content=types.Content(role="model", parts=[types.Part.from_text(text=completion_msg)]),
            )
            return

    # CASE 2: Focus Session Active (waiting for done/cancel)
    if active:
        session_id = "pomodoro_session"
        user_msg = None
        if ctx.resume_inputs and session_id in ctx.resume_inputs:
            user_msg = str(ctx.resume_inputs[session_id]).strip().lower()
        elif node_input:
            if isinstance(node_input, str):
                user_msg = node_input.strip().lower()
            elif hasattr(node_input, "parts") and node_input.parts:
                user_msg = "".join(
                    [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
                ).strip().lower()

        if user_msg == "cancel":
            ctx.state["study_timer_active"] = False
            ctx.state["study_session_topic"] = None
            cancel_msg = "⏱️ Focus session cancelled."
            yield Event(
                output=cancel_msg,
                actions=EventActions(
                    state_delta={"study_timer_active": False, "study_session_topic": None}
                ),
                content=types.Content(role="model", parts=[types.Part.from_text(text=cancel_msg)]),
            )
            return
        elif user_msg == "done":
            start_time = ctx.state.get("study_session_start_time") or time.time()
            elapsed_mins = round((time.time() - start_time) / 60.0, 1)

            topic = ctx.state.get("study_session_topic")
            # Generate 2 fresh checkpoint questions
            q_banks = ctx.state.get("question_banks", {})
            topic_qb = None
            for s, qb in q_banks.items():
                if topic in qb:
                    topic_qb = qb[topic]
                    break

            checkpoint_qs = []
            if topic_qb:
                if isinstance(topic_qb, dict):
                    explanation = topic_qb.get("explanation", "")
                else:
                    explanation = getattr(topic_qb, "explanation", "")

                client = genai.Client()
                prompt = f"""
                You are a study tutor. Based on the concept explanation below, generate exactly 2 practice questions:
                1. One Multiple Choice Question (MCQ) testing a conceptual detail.
                2. One descriptive question (Q&A) testing application-level understanding.

                Concept: {explanation}

                CRITICAL FORMATTING REQUIREMENT: Do NOT include any emojis, decorative separator lines (like '━━━━━━━━━━━━━━━━━━━━━━━━' or similar), or box-drawing characters in the explanations, questions, options, answers, or any other generated text. Keep the generated text as clean, plain markdown text.

                Provide your output in JSON format matching this schema:
                {{
                    "mcq": {{
                        "question": "question text",
                        "options": {{"A": "option A", "B": "option B", "C": "option C", "D": "option D"}},
                        "correct_option": "A, B, C, or D",
                        "explanation": "explanation of why it is correct"
                    }},
                    "qa": {{
                        "question": "descriptive question text",
                        "answer": "ideal answer"
                    }}
                }}
                """
                try:
                    res = generate_content_with_retry(
                        client,
                        model=MODEL_NAME,
                        contents=prompt,
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json",
                        )
                    )
                    data = json.loads(res.text)
                    mcq_data = data.get("mcq", {})
                    qa_data = data.get("qa", {})

                    checkpoint_qs.append({
                        "type": "mcq",
                        "question": mcq_data.get("question"),
                        "options": mcq_data.get("options"),
                        "correct_option": mcq_data.get("correct_option"),
                        "explanation": mcq_data.get("explanation"),
                        "topic": topic
                    })
                    checkpoint_qs.append({
                        "type": "qa",
                        "question": qa_data.get("question"),
                        "answer": qa_data.get("answer"),
                        "explanation": explanation,
                        "topic": topic
                    })
                except Exception as e:
                    print("Checkpoint generation failed, fallback:", e)

            if not checkpoint_qs:
                checkpoint_qs = [
                    {
                        "type": "mcq",
                        "question": f"What is the core definition of {topic}?",
                        "options": {"A": "Standard mechanism", "B": "An incorrect definition", "C": "A basic process", "D": "None of the above"},
                        "correct_option": "A",
                        "explanation": "Option A is correct.",
                        "topic": topic
                    },
                    {
                        "type": "qa",
                        "question": f"Explain the main benefit of {topic}.",
                        "answer": "It improves system efficiency and organization.",
                        "explanation": "Efficiency improvement.",
                        "topic": topic
                    }
                ]

            ctx.state["pomodoro_checkpoint_questions"] = checkpoint_qs
            ctx.state["pomodoro_checkpoint_index"] = 0

            done_msg = f"🎉 **Focus Session Complete!** You studied for **{elapsed_mins}** minutes.\n\nLet's do a quick **Checkpoint Quiz** (2 questions) to test your recall!"
            yield Event(
                content=types.Content(role="model", parts=[types.Part.from_text(text=done_msg)]),
                actions=EventActions(
                    state_delta={
                        "pomodoro_checkpoint_questions": checkpoint_qs,
                        "pomodoro_checkpoint_index": 0
                    }
                ),
            )

            first_q = checkpoint_qs[0]
            opts = first_q.get("options") or {}
            question_text = f"❓ **Checkpoint MCQ:**\n\n{first_q['question']}\n\n"
            for key in ["A", "B", "C", "D"]:
                val = opts.get(key) or opts.get(key.lower(), "")
                if val:
                    question_text += f"{key}) {val}\n"
            question_text += "\n(Type A, B, C, or D)"

            ctx.state["pending_interrupt"] = "pomodoro"
            yield Event(
                content=types.Content(role="model", parts=[types.Part.from_text(text=question_text)]),
                actions=EventActions(),
            )
            yield RequestInput(interrupt_id="pomodoro_checkpoint_0", message="")
            return
        else:
            ctx.state["pending_interrupt"] = "pomodoro"
            yield Event(
                content=types.Content(role="model", parts=[types.Part.from_text(text="Please type **'done'** when you complete your study block, or **'cancel'** to stop.")]),
                actions=EventActions(),
            )
            yield RequestInput(interrupt_id=session_id, message="")
            return

    # CASE 3: Start a new Focus Session
    selected_topic = None

    user_input_str = ""
    if isinstance(node_input, str):
        user_input_str = node_input.strip()
    elif hasattr(node_input, "parts") and node_input.parts:
        user_input_str = "".join(
            [p.text for p in node_input.parts if hasattr(p, "text") and p.text]
        ).strip()

    all_topics = []
    for s, details in subjects.items():
        if isinstance(details, dict):
            all_topics.extend(details.get("topics", []))
        else:
            all_topics.extend(getattr(details, "topics", []))

    for t in all_topics:
        if t.lower() in user_input_str.lower():
            selected_topic = t
            break

    if not selected_topic:
        topic_select_id = "select_pomodoro_topic"
        if ctx.resume_inputs and topic_select_id in ctx.resume_inputs:
            chosen = str(ctx.resume_inputs[topic_select_id]).strip()
            for t in all_topics:
                if t.lower() in chosen.lower():
                    selected_topic = t
                    break
            if not selected_topic and chosen.isdigit():
                idx = int(chosen) - 1
                if 0 <= idx < len(all_topics):
                    selected_topic = all_topics[idx]

            if not selected_topic:
                ctx.state["pending_interrupt"] = "pomodoro"
                yield Event(
                    content=types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=f"⚠️ Invalid choice. Please choose a topic by name or number:\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(all_topics)))]
                    ),
                    actions=EventActions(),
                )
                yield RequestInput(interrupt_id=topic_select_id, message="")
                return
        else:
            ctx.state["pending_interrupt"] = "pomodoro"
            yield Event(
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="⏱️ **Which topic would you like to start a study focus block on?** Choose from:\n" + "\n".join(f"{i+1}. {t}" for i, t in enumerate(all_topics)))]
                ),
                actions=EventActions(),
            )
            yield RequestInput(interrupt_id=topic_select_id, message="")
            return

    ctx.state["study_timer_active"] = True
    ctx.state["study_session_topic"] = selected_topic
    ctx.state["study_session_start_time"] = time.time()
    ctx.state["pending_interrupt"] = "pomodoro"

    start_msg = (
        f"⏱️ **Focus Block Started!**\n"
        f"Topic: **{selected_topic}**\n\n"
        "Go ahead and study this topic. I am waiting here to keep you focused.\n"
        "Type **'done'** when you complete your study block, or **'cancel'** to stop."
    )
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(text=start_msg)]),
        actions=EventActions(
            state_delta={
                "study_timer_active": True,
                "study_session_topic": selected_topic,
                "study_session_start_time": time.time(),
            }
        ),
    )
    yield RequestInput(interrupt_id="pomodoro_session", message="")


# =====================================================================
# Graph Construction & Edges Definition
# =====================================================================

edges = [
    # START routes to security_checkpoint
    Edge(from_node=START, to_node=security_checkpoint),
    # Conditional routing from security_checkpoint
    Edge(
        from_node=security_checkpoint,
        to_node=router_node,
        route=["pass", "scrubbed"],
    ),
    Edge(
        from_node=security_checkpoint,
        to_node=security_blocked_node,
        route="SECURITY_EVENT",
    ),
    # Conditional routing from router_node
    Edge(from_node=router_node, to_node=ingest_node, route="ingest"),
    Edge(from_node=router_node, to_node=question_generator_node, route="generate"),
    Edge(from_node=router_node, to_node=quiz_node, route="quiz"),
    Edge(from_node=router_node, to_node=planner_node, route="plan"),
    Edge(from_node=router_node, to_node=remedial_guide_node, route="remedial"),
    Edge(from_node=router_node, to_node=pomodoro_node, route="pomodoro"),
    # Quiz node routes to planner_node on quiz completion / stop
    Edge(from_node=quiz_node, to_node=planner_node, route="done"),
    Edge(from_node=quiz_node, to_node=router_node, route="route_to_router"),
]


# =====================================================================
# Workflow Root Agent Initialization
# =====================================================================

root_agent = Workflow(
    name="campus_buddy",
    edges=edges,
    state_schema=CampusBuddyState,
    description="A smart B.Tech study companion workflow that extracts topics, generates quizzes, diagnoses weak topics, and builds custom revision schedules.",
)

app = App(
    root_agent=root_agent,
    name="app",
)
