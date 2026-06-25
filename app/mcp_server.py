import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("campus_buddy")

# Ensure exports directory exists in the project root
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPORTS_DIR = os.path.join(ROOT_DIR, "exports")
os.makedirs(EXPORTS_DIR, exist_ok=True)


@mcp.tool()
def export_revision_schedule(subject: str, schedule: dict, exam_date: str) -> str:
    """Formats the planner_node's output into a clean readable text file and saves it to an exports/ folder.

    Args:
        subject: The name of the subject.
        schedule: Dictionary representing the schedule content.
        exam_date: The exam date string.

    Returns:
        The saved file path.
    """
    lines = []
    lines.append("=========================================")
    lines.append(f"REVISION SCHEDULE FOR {subject.upper()}")
    lines.append(f"Exam Date: {exam_date}")
    lines.append("=========================================\n")

    if isinstance(schedule, dict):
        for key, value in schedule.items():
            lines.append(f"{key}:")
            if isinstance(value, dict):
                for k, v in value.items():
                    lines.append(f"  - {k}: {v}")
            elif isinstance(value, list):
                for item in value:
                    lines.append(f"  - {item}")
            else:
                lines.append(f"  {value}")
            lines.append("")
    else:
        lines.append(str(schedule))

    content = "\n".join(lines)
    filename = f"revision_schedule_{subject.lower().replace(' ', '_')}.txt"
    filepath = os.path.join(EXPORTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


@mcp.tool()
def export_question_bank(subject: str, question_bank: dict) -> str:
    """Formats the full QB (concept explanations + MCQs + Q&A pairs) into a structured text file in exports/ folder.

    Args:
        subject: The name of the subject.
        question_bank: Dictionary representing the question bank.

    Returns:
        The saved file path.
    """
    lines = []
    lines.append("=========================================")
    lines.append(f"QUESTION BANK FOR {subject.upper()}")
    lines.append("=========================================\n")

    if isinstance(question_bank, dict):
        for topic, content in question_bank.items():
            lines.append(f"Topic: {topic}")
            lines.append("-" * len(f"Topic: {topic}"))

            explanation = ""
            mcqs = []
            qa_pairs = []
            if isinstance(content, dict):
                explanation = content.get("explanation", "")
                mcqs = content.get("mcqs", [])
                qa_pairs = content.get("qa_pairs", [])
            else:
                explanation = getattr(content, "explanation", "")
                mcqs = getattr(content, "mcqs", [])
                qa_pairs = getattr(content, "qa_pairs", [])

            if explanation:
                lines.append(f"Concept Explanation:\n{explanation}\n")

            if mcqs:
                lines.append("Multiple Choice Questions (MCQs):")
                for idx, mcq in enumerate(mcqs):
                    q_text = ""
                    options = {}
                    correct = ""
                    exp = ""
                    if isinstance(mcq, dict):
                        q_text = mcq.get("question", "")
                        options = mcq.get("options", {})
                        correct = mcq.get("correct_option", "")
                        exp = mcq.get("explanation", "")
                    else:
                        q_text = getattr(mcq, "question", "")
                        options = getattr(mcq, "options", {})
                        correct = getattr(mcq, "correct_option", "")
                        exp = getattr(mcq, "explanation", "")

                    lines.append(f"  {idx + 1}. {q_text}")
                    if isinstance(options, dict):
                        for k, v in options.items():
                            lines.append(f"     {k}) {v}")
                    lines.append(f"     Correct Option: {correct}")
                    if exp:
                        lines.append(f"     Explanation: {exp}")
                    lines.append("")

            if qa_pairs:
                lines.append("Q&A Pairs:")
                for _, qa in enumerate(qa_pairs):
                    q_text = ""
                    a_text = ""
                    if isinstance(qa, dict):
                        q_text = qa.get("question", "")
                        a_text = qa.get("answer", "")
                    else:
                        q_text = getattr(qa, "question", "")
                        a_text = getattr(qa, "answer", "")
                    lines.append(f"  Q: {q_text}")
                    lines.append(f"  A: {a_text}")
                    lines.append("")
            lines.append("=" * 40 + "\n")
    else:
        lines.append(str(question_bank))

    content = "\n".join(lines)
    filename = f"question_bank_{subject.lower().replace(' ', '_')}.txt"
    filepath = os.path.join(EXPORTS_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath


@mcp.tool()
def get_study_tip(topic: str, accuracy_pct: float) -> str:
    """Given a topic name and the student's accuracy on it, returns a short targeted study tip.

    Args:
        topic: The topic name.
        accuracy_pct: Student's accuracy percentage on this topic.

    Returns:
        A study tip.
    """
    val = accuracy_pct
    if val > 1.0:
        val = val / 100.0

    if val < 0.40:
        return "Focus here first. Re-read the concept explanation in your QB, then try explaining it aloud."
    elif val <= 0.60:
        return "You're getting there. Try the Q&A questions again without looking at the answers first."
    elif val <= 0.80:
        return (
            "Good grasp. Test yourself on application-level questions for this topic."
        )
    else:
        return "Strong! Move on but revisit briefly before exam."


@mcp.tool()
def check_syllabus_coverage(subjects_in_state: list, topics_per_subject: dict) -> dict:
    """Given the subjects and topics already ingested, returns a summary of coverage.

    Args:
        subjects_in_state: List of subjects in state.
        topics_per_subject: Dictionary of topics per subject.

    Returns:
        Dictionary summarizing coverage.
    """
    num_subjects = len(subjects_in_state)
    total_topics = 0
    incomplete_subjects = []

    for subject, topics in topics_per_subject.items():
        if subject in subjects_in_state:
            num_topics = len(topics)
            total_topics += num_topics
            if num_topics < 3:
                incomplete_subjects.append(subject)

    return {
        "subjects_loaded": num_subjects,
        "total_topics": total_topics,
        "incomplete_subjects": incomplete_subjects,
        "status": ("possibly incomplete" if incomplete_subjects else "complete"),
    }


if __name__ == "__main__":
    mcp.run()
