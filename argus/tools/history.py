"""Render message-history search hits as plain text for the model."""


def format_history(rows: list[dict]) -> str:
    """Format ``Store.search_messages`` rows, one ``[<ts>] <sender>: <content>`` line each."""
    if not rows:
        return "No matching messages."
    return "\n".join(
        f"[{r['ts']}] {r['sender']}: {r['content'].replace(chr(10), ' / ')}" for r in rows
    )
