"""The Léa example: a support agent may refund up to 50 €; above that, a human approves."""

from tool_guard import APPROVAL, FileApprovals, FileJournal, FileStops, Guard, limit

guard = Guard(
    approvals=FileApprovals("var/toolguard/approvals"),
    journal=FileJournal("var/toolguard/journal.jsonl"),
    stops=FileStops("var/toolguard/stops.json"),
)


@guard.tool()
@limit("amount", max=50, above=APPROVAL, min=0)
def refund(order: str, amount: float) -> str:
    """Refund an order. The amount is in euros."""
    return f"Refund of {amount:.2f} € done on order {order}."


if __name__ == "__main__":
    print(refund("A-12", 30))
    print(refund("A-13", 180))
