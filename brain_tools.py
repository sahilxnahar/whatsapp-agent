"""Claude tools: definitions (strict JSON schemas) and their execution against flows / slots."""
from datetime import date as date_cls

import config
import flows
import slots


def _strict(name, description, properties, required=None):
    return {
        "name": name, "description": description, "strict": True,
        "input_schema": {"type": "object", "properties": properties,
                         "required": required if required is not None else list(properties),
                         "additionalProperties": False},
    }


DATE = {"type": "string", "description": "Date as YYYY-MM-DD, or empty string for the next available days."}
PART = {"type": "string", "enum": ["any", "morning", "afternoon", "evening"]}


def build_tools():
    ct_ids = [c["id"] for c in slots.call_types()]
    tools = [
        _strict("offer_call_slots",
                "Send the customer a tappable WhatsApp list of free call slots, filtered by call type, date and part "
                "of day. Use for any new booking or availability request.",
                {"call_type": {"type": "string", "enum": ct_ids, "description": "Which call type to book."},
                 "date": DATE, "part_of_day": PART}),
        _strict("handover_to_human",
                "Flag this chat for a human team member, who will reply personally. Pauses the AI for this customer.",
                {"reason": {"type": "string", "description": "Short reason, e.g. 'refund request'."}}),
    ]
    file_ids = [f["id"] for f in config.BUSINESS.get("files", [])]
    if file_ids:
        tools.append(_strict("send_file", "Send the customer one of the business's files (PDF guide, catalogue, menu).",
                             {"file_id": {"type": "string", "enum": file_ids}}))
    return tools


def _parse_day(value):
    """Returns (date or None, error or None)."""
    if not value:
        return None, None
    try:
        day = date_cls.fromisoformat(value)
    except ValueError:
        return None, "date must be YYYY-MM-DD or an empty string"
    if day < config.now().date():
        return None, f"{day} is in the past; today is {config.now().date()}"
    return day, None


def run_tool(ctx, name, args, used):
    """Execute a tool call. Returns (result_text, is_error). `used` collects side effects: ui / handover."""
    if name == "offer_call_slots":
        day, err = _parse_day(args.get("date"))
        if err:
            return err, True
        summary = flows.offer_slots(ctx, args.get("call_type"), date=day, part=args.get("part_of_day", "any"))
        if summary is None:
            return f"unknown call_type; use one of {[c['id'] for c in slots.call_types()]}", True
        used.add("ui")
        return summary + " Reply with one short line; the customer taps a slot to continue.", False
    if name == "send_file":
        ok = flows.send_file(ctx, args.get("file_id"))
        used.add("ui")
        return ("File sent." if ok else "Sending failed; tell the customer the team will share it shortly."), not ok
    if name == "handover_to_human":
        flows.handover(ctx, args.get("reason", "requested by AI"), quiet=True)
        used.add("handover")
        return "Chat flagged for a human; AI replies are now paused for this customer.", False
    return f"unknown tool {name}", True
